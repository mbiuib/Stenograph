"""One Jigasi meeting connection: per-participant streaming decode.

Audio frames arrive from the websocket handler; every participant gets its own
`StreamTracker` (LocalAgreement-2 on whisper-turbo). Committed text is
persisted as a job (kind="jitsi") and streamed to the event bus (web UI);
caption messages (partial/final JSON) are queued for the websocket sender.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
import wave
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from ..config import Settings
from ..domain.models import Job, JobStatus, Segment
from ..events import EventBus
from ..live.streamer import SAMPLE_RATE, StreamTracker
from ..naming import jitsi_name, room_label
from ..storage import JobRepository
from .protocol import normalize_language, result_message

log = logging.getLogger(__name__)

DONE = ""  # dequeue sentinel: session closed and all messages drained

MIN_PAUSE_SEC = 0.05  # короче — сетевой джиттер, кадры пишутся встык
SILENCE_BLOCK_SEC = 30.0  # тишина пишется блоками, чтобы не держать большие массивы


def _silence_blocks(seconds: float) -> list[np.ndarray]:
    """Zero-filled float32 blocks covering ``seconds`` of digital silence."""
    blocks: list[np.ndarray] = []
    remaining = seconds
    while remaining > 1e-3:
        size = int(min(remaining, SILENCE_BLOCK_SEC) * SAMPLE_RATE)
        if size <= 0:
            break
        blocks.append(np.zeros(size, dtype=np.float32))
        remaining -= size / SAMPLE_RATE
    return blocks


def _safe_name(participant_id: str) -> str:
    """Filesystem-safe file name for a participant's audio."""
    cleaned = "".join(ch for ch in participant_id if ch.isalnum() or ch in "-_")
    return cleaned[:64] or "participant"


def _written_seconds(writer: Any) -> float:
    """Audio seconds already written to a participant's WAV (0 when closed)."""
    try:
        frames = writer.tell()
    except Exception:  # noqa: BLE001 — a closed writer must not break the snapshot
        return 0.0
    return max(0, int(frames)) / SAMPLE_RATE


@dataclass(slots=True)
class _Participant:
    """Runtime state of one conference participant."""

    participant_id: str
    label: str
    writer: Any  # wave.Wave_write
    path: Path
    tracker: StreamTracker | None = None  # None in record-only meetings
    language: str | None = None
    stream: Any | None = None  # pool adapter when served by the shared decoder
    last_frame: float = 0.0  # monotonic time of the last fed frame
    next_offset: float = 0.0  # session second the next written sample goes to
    tracker_fed: float = 0.0  # audio seconds already handed to the tracker


class _ParticipantStream:
    """One participant exposed to the shared live decode pool.

    The pool (LiveManager) calls this surface from its own decode thread; the
    meeting's pump then only buffers incoming frames and writes WAV files.
    Mirrors a live session's serving interface: ``job``/``finished``/
    ``needs_turn``/``begin_batch_window``/``end_batch_window``.
    """

    def __init__(
        self, session: MeetingSession, participant_id: str, language: str | None
    ) -> None:
        self._session = session
        self._participant_id = participant_id
        self.language = language

    @property
    def job(self) -> Job:
        return self._session.job

    @property
    def finished(self) -> bool:
        return False

    @property
    def batch_cap_sec(self) -> float:
        return self._session._bridge_cap

    def needs_turn(self) -> bool:
        return self._session._participant_needs_turn(self._participant_id)

    def begin_batch_window(
        self, *, cap_sec: float | None = None
    ) -> tuple[str, np.ndarray] | None:
        self._session._drain_pending(self._participant_id)
        participant = self._session._participants.get(self._participant_id)
        if participant is None or participant.tracker is None:
            return None
        audio = participant.tracker.pending_window(cap_sec=cap_sec)
        if audio is None:
            return None
        return ("", audio)

    def end_batch_window(self, track: str | None, words: list[Any] | None) -> bool:
        if words is not None:
            participant = self._session._participants.get(self._participant_id)
            if participant is not None and participant.tracker is not None:
                participant.tracker.apply_window(
                    list(words), hold_sec=self._session._bridge_hold
                )
        return self.needs_turn()


class MeetingSession:
    """A live transcription session for one Jitsi room (one websocket)."""

    def __init__(
        self,
        meeting_id: str,
        settings: Settings,
        repo: JobRepository,
        bus: EventBus,
        transcriber_factory: Any,
        pool: Any | None = None,
        reprocess: Callable[[Job], Job | None] | None = None,
        auto_reprocess: bool = False,
        transcribe: bool = True,
    ) -> None:
        self.meeting_id = meeting_id
        # Record-only meetings (MEETSCRIBE_REALTIME_TRANSCRIBE=false) save the
        # audio and leave decoding to the later quality pass; a running meeting
        # can be switched either way from the Jitsi page (``set_transcribe``).
        self.transcribe = transcribe
        # Meeting language: forced by MEETSCRIBE_BRIDGE_LANGUAGE when set — the
        # live pass and the improvement both honour it; None (default) keeps
        # per-frame Jigasi languages and lets the improvement auto-detect.
        self._language = normalize_language(settings.bridge_language or "")
        self.job = Job(
            kind="jitsi",
            source_name=jitsi_name(room=meeting_id),
            status=JobStatus.RUNNING,
            started_at=time.time(),
            language=self._language,
        )
        self.job.meta["engine"] = f"whisper:{settings.live_model}"
        self.job.meta["meeting_id"] = meeting_id
        self.job.meta["room"] = room_label(meeting_id)
        self.job.meta["transcribe"] = transcribe
        # Дорожки пишутся по часам встречи (паузы между репликами — тишиной):
        # файлы участников выровнены и микшируются в одну запись «как вживую».
        self.job.meta["audio_timeline"] = "realtime"
        repo.save(self.job)

        self._settings = settings
        self._repo = repo
        self._bus = bus
        self._factory = transcriber_factory
        self._pool = pool  # shared live decode pool (None = tick in this thread)
        self._reprocess = reprocess  # quality re-pass chain after the meeting ends
        self._auto_reprocess = auto_reprocess
        self._bridge_cap = settings.bridge_batch_window_sec
        self._bridge_hold = settings.bridge_batch_hold_sec
        self._lock = threading.Lock()  # guards participants/pending/segments
        self._participants: dict[str, _Participant] = {}
        self._pending: dict[str, list[np.ndarray]] = {}
        self._pending_samples: dict[str, int] = {}  # queued audio samples per participant
        self._dropped_samples: dict[str, int] = {}  # dropped backlog, for throttled warnings
        self._drop_log_at: dict[str, float] = {}  # last drop warning per participant
        self._segments: list[Segment] = []
        self._out: queue.Queue[str] = queue.Queue()
        self._closed = threading.Event()
        self._stop_event = threading.Event()
        self._started_monotonic = time.monotonic()
        self._idle_stop_sec = settings.jitsi_idle_stop_sec  # 0 = watchdog off
        self._last_frame_monotonic = self._started_monotonic
        self._stop_reason: str | None = None  # "manual" | "idle" | None
        self._audio_dir = settings.data_dir / "jitsi" / self.job.id
        self._audio_dir.mkdir(parents=True, exist_ok=True)
        self._worker = threading.Thread(
            target=self._pump_loop,
            name=f"stenograph-jitsi-{meeting_id[:8]}",
            daemon=True,
        )
        self._worker.start()
        log.info("jitsi bridge: сессия %s запущена (задача %s)", meeting_id, self.job.id)

    def status(self) -> dict:
        """Snapshot for the Jitsi page: participants, counters, durations."""
        now = time.monotonic()
        with self._lock:
            participants = []
            for participant in self._participants.values():
                last_frame = participant.last_frame
                participants.append(
                    {
                        "id": participant.participant_id,
                        "label": participant.label,
                        "language": participant.language,
                        "segments": sum(
                            1
                            for segment in self._segments
                            if segment.speaker == participant.label
                        ),
                        "audio_sec": round(_written_seconds(participant.writer), 1),
                        "last_frame_sec": round(now - last_frame, 1) if last_frame else None,
                    }
                )
            return {
                "meeting_id": self.meeting_id,
                "room": room_label(self.meeting_id),
                "source_name": self.job.source_name,
                "job_id": self.job.id,
                "job_status": self.job.status.value,
                "started_at": self.job.started_at,
                "duration_sec": round(now - self._started_monotonic, 1),
                "language": self.job.language,
                "pooled": self._pool is not None,
                "transcribe": self.transcribe,
                "segments": len(self._segments),
                "participants": participants,
                "silence_sec": round(now - self._last_frame_monotonic, 1),
                "idle_stop_sec": self._idle_stop_sec,
                "stopping": self._stop_event.is_set(),
            }

    # -- early stops (API thread / pump thread) --------------------------------

    @property
    def stop_requested(self) -> bool:
        """True once a stop (manual or idle watchdog) has been requested."""
        return self._stop_event.is_set()

    @property
    def finished(self) -> bool:
        """True once the worker has finalized the session (all drained)."""
        return self._closed.is_set()

    @property
    def stop_reason(self) -> str | None:
        """Why the session stopped early ("manual"/"idle"); None on a normal end."""
        with self._lock:
            return self._stop_reason

    def request_stop(self, reason: str) -> bool:
        """Ask the session to finalize early; True when the flag was fresh.

        Used by the manual stop API and the idle watchdog: the worker exits its
        loop, flushes the trackers and finalizes the job; the websocket watcher
        then closes the Jigasi socket so the captions stop too. First reason
        wins (an idle auto-stop and a manual click can race).
        """
        with self._lock:
            self._stop_reason = self._stop_reason or reason
            fresh = not self._stop_event.is_set()
        if fresh:
            self._stop_event.set()
            log.info("jitsi bridge: остановка встречи %s (%s)", self.meeting_id, reason)
        return fresh

    def wait_finished(self, timeout: float = 30.0) -> bool:
        """Block until the worker finalized the session; False on timeout."""
        return self._closed.wait(timeout)

    # -- intake (event loop thread) -------------------------------------------

    def feed(self, participant_id: str, language: str | None, audio: np.ndarray) -> None:
        """Buffer one participant's audio; creates the tracker on first frames.

        Jigasi sends speech bursts, not a continuous stream, so each frame is
        placed at its real position on the meeting timeline
        (``arrival − duration``) and the gap since the previous frame is
        filled with silence. File, transcript times and the mixed playback all
        share that timeline — «как будто ты прямо там».
        """
        if audio.size == 0:
            return
        duration = audio.size / SAMPLE_RATE
        arrival = time.monotonic() - self._started_monotonic
        created = False
        silence_sec = 0.0
        with self._lock:
            participant = self._participants.get(participant_id)
            if participant is None:
                participant = self._create_participant(
                    participant_id, self._language or language
                )
                created = True
            start = max(participant.next_offset, arrival - duration)
            if start - participant.next_offset >= MIN_PAUSE_SEC:
                silence_sec = start - participant.next_offset
            else:
                start = participant.next_offset  # джиттер сети: пишем встык
            participant.next_offset = start + duration
            if self.transcribe:
                if silence_sec:
                    blocks = _silence_blocks(silence_sec)
                    self._pending[participant_id].extend(blocks)
                    self._pending_samples[participant_id] = (
                        self._pending_samples.get(participant_id, 0)
                        + sum(block.size for block in blocks)
                    )
                self._pending[participant_id].append(audio)
                self._pending_samples[participant_id] = (
                    self._pending_samples.get(participant_id, 0) + audio.size
                )
                self._trim_pending(participant_id)
                participant.tracker_fed = participant.next_offset
            participant.last_frame = time.monotonic()
            self._last_frame_monotonic = participant.last_frame
        if created and self.transcribe:
            # Never register with the pool while holding the session lock: the
            # decode pool takes this lock while serving and holds its own serve
            # guard there — adopting under the lock deadlocks the event loop
            # (S→G vs G→S). The pool may only serve the participant after this.
            self._adopt_participant(participant)
        if silence_sec:
            self._write_silence(participant, silence_sec)
        self._write_audio(participant, audio)

    @staticmethod
    def _write_audio(participant: _Participant, audio: np.ndarray) -> None:
        """Append captured audio to the participant's WAV (best effort)."""
        writer = participant.writer
        if writer is None:
            return
        try:
            frames = (np.clip(audio, -1.0, 1.0) * 32767.0).astype("<i2")
            writer.writeframes(frames.tobytes())
        except Exception:  # noqa: BLE001 — a broken writer must not kill the session
            log.exception(
                "jitsi bridge: не удалось записать звук участника %s",
                participant.participant_id,
            )

    def _write_silence(self, participant: _Participant, seconds: float) -> None:
        """Append ``seconds`` of digital silence to the participant's WAV."""
        for block in _silence_blocks(seconds):
            self._write_audio(participant, block)

    def _make_tracker(self, participant_id: str, language: str | None) -> StreamTracker:
        """Build the per-participant streaming tracker (decoding paths only)."""
        return StreamTracker(
            self._factory(language),
            on_final=self._on_final_factory(participant_id),
            on_partial=self._on_partial_factory(participant_id),
            step_sec=self._settings.live_step_sec,
            max_window_sec=self._settings.live_max_window_sec,
        )

    def _create_participant(self, participant_id: str, language: str | None) -> _Participant:
        """Create the audio file (+ tracker when decoding) for a participant.

        Called under the session lock. Record-only meetings never build the
        tracker: no decoder touches the participant until the meeting is
        switched to realtime or the quality pass runs over the files later.
        """
        label = f"Спикер {len(self._participants) + 1}"
        path = self._audio_dir / f"{_safe_name(participant_id)}.wav"
        writer = wave.open(str(path), "wb")  # noqa: SIM115 — kept open for the session
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(SAMPLE_RATE)
        tracker = self._make_tracker(participant_id, language) if self.transcribe else None
        participant = _Participant(
            participant_id=participant_id,
            label=label,
            writer=writer,
            path=path,
            tracker=tracker,
            language=language,
        )
        self._participants[participant_id] = participant
        self._pending[participant_id] = []
        self._pending_samples[participant_id] = 0
        self.job.meta.setdefault("participants", {})[participant_id] = label
        self.job.meta.setdefault("audio", {})[label] = str(path)
        log.info(
            "jitsi bridge: участник %s → «%s»%s",
            participant_id,
            label,
            "" if self.transcribe else " (без распознавания)",
        )
        return participant

    def _adopt_participant(self, participant: _Participant) -> None:
        """Register a participant with the shared decode pool.

        Must be called WITHOUT the session lock held (see ``feed``): ``adopt``
        is quick, but any lock inversion here would wedge the event loop.
        """
        pool = self._pool
        if pool is None:
            return
        with self._lock:
            if not self.transcribe or participant.stream is not None:
                return
        stream = _ParticipantStream(self, participant.participant_id, participant.language)
        if pool.adopt(self._stream_key(participant.participant_id), stream):
            with self._lock:
                if self.transcribe:
                    participant.stream = stream  # served by the shared batched pool
                    return
            # Toggled off while registering: detach immediately.
            try:
                pool.unadopt(self._stream_key(participant.participant_id))
            except Exception:  # noqa: BLE001 — a failed detach must not crash feed
                log.exception(
                    "jitsi bridge: не удалось отцепить участника %s от пула",
                    participant.participant_id,
                )

    def set_transcribe(self, enabled: bool) -> None:
        """Switch realtime decoding of this meeting on/off; recording continues.

        Called from the API thread (the Jitsi page toggle). Enabling mid-meeting
        first feeds the tracker the missed time as silence, so replay times
        stay on the meeting clock. Pooled participants are released without
        holding the session lock: ``unadopt`` waits for an in-flight serve
        round, and holding the lock across it would deadlock the event loop
        (the serve guard ↔ session lock ordering rule).
        """
        with self._lock:
            if enabled == self.transcribe:
                return
            self.transcribe = enabled
            self.job.meta["transcribe"] = enabled
            participants = list(self._participants.values())
            for participant in participants:
                if enabled:
                    if participant.tracker is None:
                        participant.tracker = self._make_tracker(
                            participant.participant_id, participant.language
                        )
                    catch_up = participant.next_offset - participant.tracker_fed
                    if catch_up >= MIN_PAUSE_SEC:
                        blocks = _silence_blocks(catch_up)
                        self._pending[participant.participant_id].extend(blocks)
                        self._pending_samples[participant.participant_id] = (
                            self._pending_samples.get(participant.participant_id, 0)
                            + sum(block.size for block in blocks)
                        )
                    participant.tracker_fed = participant.next_offset
                else:
                    # Всё неразобранное — в мусор: часы догоним при включении.
                    self._pending[participant.participant_id] = []
                    self._pending_samples[participant.participant_id] = 0
        self._repo.save(self.job)
        self._bus.publish(self.job.id, {"type": "meta", "meta": self.job.meta})
        if enabled:
            for participant in participants:
                self._adopt_participant(participant)
            log.info("jitsi bridge: распознавание включено (%s)", self.meeting_id)
        else:
            pool = self._pool
            for participant in participants:
                if participant.stream is None or pool is None:
                    continue
                try:
                    pool.unadopt(self._stream_key(participant.participant_id))
                except Exception:  # noqa: BLE001 — the flush path still finalizes
                    log.exception(
                        "jitsi bridge: не удалось отцепить участника %s от пула",
                        participant.participant_id,
                    )
                participant.stream = None
            log.info("jitsi bridge: распознавание выключено (%s)", self.meeting_id)

    # -- shared-pool helping (decode pool thread) ------------------------------

    def _stream_key(self, participant_id: str) -> str:
        """Pool registry key of one participant (unique across meetings)."""
        return f"{self.job.id}:{participant_id}"

    def _drain_pending(self, participant_id: str, *, unlimited: bool = False) -> None:
        """Feed buffered frames into the participant's tracker (pool thread).

        Frames are merged into one array before feeding: ``StreamTracker.feed``
        is a plain append, so N per-frame calls copied the whole window N times
        — the drain cost grew with the backlog (a collapsed round once spent
        28 s collecting vs 2 s of inference). One merged append is a single
        pass. At most ``bridge_drain_max_sec`` of audio is taken per call so an
        overloaded round stays bounded; the remainder waits in the queue.
        """
        with self._lock:
            queue = self._pending.get(participant_id, [])
            limit = 0 if unlimited else int(self._settings.bridge_drain_max_sec * SAMPLE_RATE)
            if limit > 0:
                take: list[np.ndarray] = []
                acc = 0
                split = 0
                for index, chunk in enumerate(queue):
                    take.append(chunk)
                    acc += chunk.size
                    split = index + 1
                    if acc >= limit:
                        break
                rest = queue[split:]
            else:
                take, rest = queue, []
                acc = sum(chunk.size for chunk in queue)
            self._pending[participant_id] = list(rest)
            self._pending_samples[participant_id] = max(
                0, self._pending_samples.get(participant_id, 0) - acc
            )
        participant = self._participants.get(participant_id)
        if participant is None or participant.tracker is None or not take:
            return
        merged = take[0] if len(take) == 1 else np.concatenate(take)
        participant.tracker.feed(merged)

    def _trim_pending(self, participant_id: str) -> None:
        """Drop the oldest queued frames when the backlog exceeds its cap.

        The queue absorbs jitter; under sustained overload it would grow until
        RAM runs out while the decoder falls ever further behind anyway.
        Dropping the oldest audio bounds both — the recordings stay intact on
        disk and the quality pass re-reads the WAVs after the meeting.
        """
        cap_sec = self._settings.bridge_pending_max_sec
        if cap_sec <= 0:
            return
        queue = self._pending[participant_id]
        total = self._pending_samples.get(participant_id, 0)
        drop_target = total - int(cap_sec * SAMPLE_RATE)
        if drop_target <= 0:
            return
        dropped = 0
        split = 0
        for index, chunk in enumerate(queue):
            if dropped >= drop_target:
                break
            dropped += chunk.size
            split = index + 1
        del queue[:split]
        self._pending_samples[participant_id] = total - dropped
        self._dropped_samples[participant_id] = (
            self._dropped_samples.get(participant_id, 0) + dropped
        )
        now = time.monotonic()
        if now - self._drop_log_at.get(participant_id, 0.0) >= 10.0:
            log.warning(
                "jitsi bridge: очередь участника %s переполнена — пропущено %.0f с аудио "
                "(декодер не успевает; запись не тронута)",
                participant_id,
                self._dropped_samples[participant_id] / SAMPLE_RATE,
            )
            self._dropped_samples[participant_id] = 0
            self._drop_log_at[participant_id] = now

    def _participant_needs_turn(self, participant_id: str) -> bool:
        """True while the pool still has work for this participant."""
        with self._lock:
            has_pending = bool(self._pending.get(participant_id))
        if has_pending:
            return True
        participant = self._participants.get(participant_id)
        return (
            participant is not None
            and participant.tracker is not None
            and participant.tracker.ready()
        )

    # -- caption messages (decode worker thread) --------------------------------

    def _on_final_factory(self, participant_id: str) -> Callable[[float, float, str], None]:
        def on_final(start: float, end: float, text: str) -> None:
            text = text.strip()
            if not text:
                return
            label = self._participants[participant_id].label
            segment = Segment(
                index=len(self._segments),
                start=round(max(0.0, start), 2),
                end=round(max(start, end), 2),
                text=text,
                speaker=label,
            )
            with self._lock:
                self._segments.append(segment)
            self._bus.publish(self.job.id, {"type": "segment", "segment": segment.model_dump()})
            self._persist()
            self._out.put(result_message("final", participant_id, text))

        return on_final

    def _on_partial_factory(self, participant_id: str) -> Callable[[str], None]:
        def on_partial(text: str) -> None:
            text = text.strip()
            if text:
                self._out.put(result_message("partial", participant_id, text))

        return on_partial

    def dequeue(self, timeout: float) -> str | None:
        """Next caption message; None on timeout; DONE when drained and closed."""
        try:
            return self._out.get(timeout=timeout)
        except queue.Empty:
            return DONE if self._closed.is_set() else None

    # -- decode worker ------------------------------------------------------------

    def _pump_loop(self) -> None:
        try:
            while not self._stop_event.is_set():
                self._pump_participants()
                self._check_idle()
                time.sleep(0.15)
            self._detach_streams()
            self._pump_participants()
            for participant in list(self._participants.values()):
                if participant.tracker is not None:
                    participant.tracker.flush()
            self._finish()
        except Exception as exc:  # noqa: BLE001 — report and keep the app alive
            log.exception("jitsi bridge: сессия %s упала", self.meeting_id)
            self._fail(str(exc))
        finally:
            self._closed.set()

    def _detach_streams(self) -> None:
        """Release pooled participants back to this thread for the final flush.

        ``unadopt`` waits for any in-flight serve round, so after it returns
        the tracker belongs exclusively to this thread again.
        """
        pool = self._pool
        if pool is None:
            return
        for participant in list(self._participants.values()):
            if participant.stream is None:
                continue
            try:
                pool.unadopt(self._stream_key(participant.participant_id))
            except Exception:  # noqa: BLE001 — finalization must not be blocked
                log.exception(
                    "jitsi bridge: не удалось отцепить участника %s от пула",
                    participant.participant_id,
                )
            participant.stream = None
            self._drain_pending(participant.participant_id, unlimited=True)

    def _pump_participants(self) -> None:
        for participant_id, participant in list(self._participants.items()):
            if participant.stream is not None:
                continue  # the shared decode pool feeds and serves this one
            if participant.tracker is None:
                continue  # record-only participant: only the WAV is written
            with self._lock:
                chunks = self._pending.get(participant_id, [])
                self._pending[participant_id] = []
                self._pending_samples[participant_id] = 0
            if chunks:
                participant.tracker.feed(chunks[0] if len(chunks) == 1 else np.concatenate(chunks))
                participant.tracker.tick()

    def _check_idle(self) -> None:
        """Auto-stop the meeting when nobody has spoken for the idle timeout.

        Jigasi keeps the session while any client stays in the room: a
        forgotten tab with a muted microphone holds an "empty" meeting alive
        forever. Speech frames reset the timer; without a single frame it
        counts from the session start. ``MEETSCRIBE_JITSI_IDLE_STOP_SEC`` = 0
        disables the watchdog.
        """
        if self._idle_stop_sec <= 0 or self._stop_event.is_set():
            return
        idle_sec = time.monotonic() - self._last_frame_monotonic
        if idle_sec >= self._idle_stop_sec:
            log.info(
                "jitsi bridge: встреча %s молчит %.0f с (порог %.0f) — авто-стоп",
                self.meeting_id,
                idle_sec,
                self._idle_stop_sec,
            )
            self.request_stop("idle")

    def _persist(self) -> None:
        with self._lock:
            ordered = sorted(self._segments, key=lambda item: item.start)
            for position, segment in enumerate(ordered):
                segment.index = position
            self.job.segments = list(ordered)
        self._repo.save(self.job)

    def _finish(self) -> None:
        self._close_writers()
        duration = time.monotonic() - self._started_monotonic
        self._persist()
        with self._lock:
            ordered = list(self.job.segments)
        self.job.text = "\n".join(f"{item.speaker}: {item.text}" for item in ordered)
        self.job.status = JobStatus.DONE
        self.job.progress = 100
        self.job.message = self._final_message()
        reason = self.stop_reason
        if reason:
            self.job.meta["stop_reason"] = reason
        self.job.finished_at = time.time()
        self.job.meta["duration"] = round(duration, 2)
        self.job.meta["processing_seconds"] = round(duration, 2)
        self._repo.save(self.job)
        self._bus.publish(
            self.job.id, {"type": "done", "text": self.job.text, "meta": self.job.meta}
        )
        self._chain_reprocess()
        log.info(
            "jitsi bridge: сессия %s завершена — %d сегментов за %.1f с",
            self.meeting_id,
            len(ordered),
            duration,
        )

    def _final_message(self) -> str:
        """Human message for the finished job; early stops name their reason."""
        reason = self.stop_reason
        if reason == "manual":
            return "Запись остановлена вручную из Стенографа"
        if reason == "idle":
            minutes, seconds = divmod(int(self._idle_stop_sec), 60)
            return f"Остановлено автоматически: {minutes:02d}:{seconds:02d} без речи"
        return (
            "Транскрибация завершена"
            if self.transcribe
            else "Запись завершена (без распознавания)"
        )

    def _chain_reprocess(self) -> None:
        """Hand the finished meeting to the quality re-pass (best effort)."""
        if not (self._auto_reprocess and self._reprocess is not None):
            return
        if not self._participants:
            return
        try:
            self._reprocess(self.job)
        except ValueError as exc:  # nothing recorded / nothing to improve
            log.info("jitsi bridge: улучшение записи пропущено (%s)", exc)
        except Exception:  # noqa: BLE001 — chaining must never break the session
            log.exception("jitsi bridge: не удалось запустить улучшение записи %s", self.job.id)

    def _fail(self, reason: str) -> None:
        self._close_writers()
        self.job.status = JobStatus.ERROR
        self.job.error = reason
        self.job.finished_at = time.time()
        self._repo.save(self.job)
        self._bus.publish(self.job.id, {"type": "error", "message": reason})

    def stop(self) -> None:
        """Signal the worker to flush everything and finalize (idempotent)."""
        self._stop_event.set()
        if self._worker.is_alive():
            self._worker.join(timeout=60.0)
        if self._worker.is_alive():  # pragma: no cover
            log.warning("jitsi bridge: воркер сессии %s не завершился за 60 с", self.meeting_id)

    def _close_writers(self) -> None:
        for participant in self._participants.values():
            writer = participant.writer
            if writer is None:
                continue
            try:
                writer.close()
            except Exception:  # noqa: BLE001
                log.debug("jitsi bridge: не удалось закрыть WAV участника", exc_info=True)
            participant.writer = None
