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
from ..naming import timestamped
from ..storage import JobRepository
from .protocol import result_message

log = logging.getLogger(__name__)

DONE = ""  # dequeue sentinel: session closed and all messages drained


def _safe_name(participant_id: str) -> str:
    """Filesystem-safe file name for a participant's audio."""
    cleaned = "".join(ch for ch in participant_id if ch.isalnum() or ch in "-_")
    return cleaned[:64] or "participant"


@dataclass(slots=True)
class _Participant:
    """Runtime state of one conference participant."""

    participant_id: str
    label: str
    tracker: StreamTracker
    writer: Any  # wave.Wave_write
    path: Path


class MeetingSession:
    """A live transcription session for one Jitsi room (one websocket)."""

    def __init__(
        self,
        meeting_id: str,
        settings: Settings,
        repo: JobRepository,
        bus: EventBus,
        transcriber_factory: Any,
    ) -> None:
        self.meeting_id = meeting_id
        self.job = Job(
            kind="jitsi",
            source_name=timestamped("Jitsi"),
            status=JobStatus.RUNNING,
            started_at=time.time(),
        )
        self.job.meta["engine"] = f"whisper:{settings.live_model}"
        self.job.meta["meeting_id"] = meeting_id
        repo.save(self.job)

        self._settings = settings
        self._repo = repo
        self._bus = bus
        self._factory = transcriber_factory
        self._lock = threading.Lock()  # guards participants/pending/segments
        self._participants: dict[str, _Participant] = {}
        self._pending: dict[str, list[np.ndarray]] = {}
        self._segments: list[Segment] = []
        self._out: queue.Queue[str] = queue.Queue()
        self._closed = threading.Event()
        self._stop_event = threading.Event()
        self._started_monotonic = time.monotonic()
        self._audio_dir = settings.data_dir / "jitsi" / self.job.id
        self._audio_dir.mkdir(parents=True, exist_ok=True)
        self._worker = threading.Thread(
            target=self._pump_loop,
            name=f"stenograph-jitsi-{meeting_id[:8]}",
            daemon=True,
        )
        self._worker.start()
        log.info("jitsi bridge: сессия %s запущена (задача %s)", meeting_id, self.job.id)

    # -- intake (event loop thread) -------------------------------------------

    def feed(self, participant_id: str, language: str | None, audio: np.ndarray) -> None:
        """Buffer one participant's audio; creates the tracker on first frames."""
        if audio.size == 0:
            return
        with self._lock:
            participant = self._participants.get(participant_id)
            if participant is None:
                participant = self._create_participant(participant_id, language)
            self._pending[participant_id].append(audio)
        writer = participant.writer
        if writer is not None:
            try:
                frames = (np.clip(audio, -1.0, 1.0) * 32767.0).astype("<i2")
                writer.writeframes(frames.tobytes())
            except Exception:  # noqa: BLE001 — a broken writer must not kill the session
                log.exception("jitsi bridge: не удалось записать звук участника %s", participant_id)

    def _create_participant(self, participant_id: str, language: str | None) -> _Participant:
        """Create tracker + audio file for a participant (call under lock)."""
        label = f"Спикер {len(self._participants) + 1}"
        path = self._audio_dir / f"{_safe_name(participant_id)}.wav"
        writer = wave.open(str(path), "wb")  # noqa: SIM115 — kept open for the session
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(SAMPLE_RATE)
        tracker = StreamTracker(
            self._factory(language),
            on_final=self._on_final_factory(participant_id),
            on_partial=self._on_partial_factory(participant_id),
            step_sec=self._settings.live_step_sec,
            max_window_sec=self._settings.live_max_window_sec,
        )
        participant = _Participant(
            participant_id=participant_id, label=label, tracker=tracker, writer=writer, path=path
        )
        self._participants[participant_id] = participant
        self._pending[participant_id] = []
        self.job.meta.setdefault("participants", {})[participant_id] = label
        self.job.meta.setdefault("audio", {})[label] = str(path)
        log.info("jitsi bridge: участник %s → «%s»", participant_id, label)
        return participant

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
                time.sleep(0.15)
            self._pump_participants()
            for participant in list(self._participants.values()):
                participant.tracker.flush()
            self._finish()
        except Exception as exc:  # noqa: BLE001 — report and keep the app alive
            log.exception("jitsi bridge: сессия %s упала", self.meeting_id)
            self._fail(str(exc))
        finally:
            self._closed.set()

    def _pump_participants(self) -> None:
        for participant_id, participant in list(self._participants.items()):
            with self._lock:
                chunks = self._pending.get(participant_id, [])
                self._pending[participant_id] = []
            for chunk in chunks:
                participant.tracker.feed(chunk)
            if chunks:
                participant.tracker.tick()

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
        self.job.message = "Транскрибация завершена"
        self.job.finished_at = time.time()
        self.job.meta["duration"] = round(duration, 2)
        self.job.meta["processing_seconds"] = round(duration, 2)
        self._repo.save(self.job)
        self._bus.publish(
            self.job.id, {"type": "done", "text": self.job.text, "meta": self.job.meta}
        )
        log.info(
            "jitsi bridge: сессия %s завершена — %d сегментов за %.1f с",
            self.meeting_id,
            len(ordered),
            duration,
        )

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
