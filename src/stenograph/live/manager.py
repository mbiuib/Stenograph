"""Live session lifecycle: capture → queued streaming decode → persistence.

Several sessions can capture at the same time: each browser page records its
own microphone/system audio, and the server-side session captures this
machine's devices (that one stays single). Transcription is serialized
through one decode queue — a single worker thread serves sessions turn by
turn (FIFO, bounded work per turn), so whisper never runs N streams on the
GPU at once and every recording is transcribed as queue capacity allows.
A session that starts while the queue is busy just keeps recording; its text
catches up when its turn comes (its lag is reported by ``status()``).

A session is stored as a job (kind="live"), so the same SSE endpoint and the
job page work for it as for file jobs. Per-track audio is written to
``data/live/<job id>/<track>.wav`` so a quality offline pass can be run over
the recording later.
"""

from __future__ import annotations

import logging
import threading
import time
import wave
from collections import deque
from collections.abc import Callable
from typing import Any

import numpy as np

from ..config import Settings
from ..domain.models import Job, JobStatus, Segment
from ..events import EventBus
from ..storage import JobRepository
from . import capture as capture_module
from .streamer import SAMPLE_RATE, StreamTracker, WindowTranscriber, WindowWord

log = logging.getLogger(__name__)

TranscriberFactory = Callable[[str | None], WindowTranscriber]
CaptureFactory = Callable[..., capture_module.AudioSource]

DEFAULT_TRACKS: tuple[str, ...] = ("system", "mic")
FINALIZE_WAIT_SEC = 30.0  # how long stop_session waits for the queue to finalize a session
IDLE_SLEEP_SEC = 0.15  # decode loop idle poll (same cadence as the old per-session pump)


_ENGINE_FACTORIES: dict[tuple[str, str, str, str], TranscriberFactory] = {}
_ENGINE_FACTORIES_LOCK = threading.Lock()


def default_transcriber_factory(settings: Settings) -> TranscriberFactory:
    """Shared window-transcriber factory: one whisper engine per model config.

    Live sessions and the Jigasi bridge share the same engine instance; GPU
    calls are serialized with an engine lock so concurrent tracks cannot race
    the model.
    """
    key = (
        settings.live_model,
        str(settings.models_dir or ""),
        settings.device,
        settings.compute_type,
    )
    with _ENGINE_FACTORIES_LOCK:
        factory = _ENGINE_FACTORIES.get(key)
        if factory is None:
            factory = _build_transcriber_factory(settings)
            _ENGINE_FACTORIES[key] = factory
        return factory


def _build_transcriber_factory(settings: Settings) -> TranscriberFactory:
    """Create a factory bound to one lazily loaded whisper engine."""
    holder: dict[str, Any] = {}
    engine_lock = threading.Lock()

    def factory(language: str | None) -> WindowTranscriber:
        engine = holder.get("engine")
        if engine is None:
            from ..engines.whisper import FasterWhisperEngine

            engine = FasterWhisperEngine(
                model=settings.live_model,
                models_dir=settings.models_dir,
                device=settings.device,
                compute_type=settings.compute_type,
            )
            holder["engine"] = engine

        def transcribe(audio: np.ndarray) -> list[WindowWord]:
            with engine_lock:
                return engine.transcribe_window(audio, language=language, beam_size=1)

        return transcribe

    return factory


class LiveManager:
    """Owns all live sessions and the shared transcription queue."""

    def __init__(
        self,
        settings: Settings,
        repo: JobRepository,
        bus: EventBus,
        *,
        transcriber_factory: TranscriberFactory | None = None,
        capture_factory: CaptureFactory | None = None,
        reprocess: Callable[[Job], Job | None] | None = None,
        auto_reprocess: bool = False,
    ) -> None:
        self._settings = settings
        self._repo = repo
        self._bus = bus
        self._transcriber_factory = transcriber_factory or default_transcriber_factory(settings)
        self._capture_factory = capture_factory or capture_module.open_source
        self._reprocess = reprocess
        self._auto_reprocess = auto_reprocess
        self._sessions: dict[str, _LiveSession] = {}
        self._turn: deque[str] = deque()  # sessions waiting for a transcription turn (FIFO)
        self._queued: set[str] = set()  # turn-queue membership, for O(1) dedupe
        self._serving_id: str | None = None  # session the decoder works on right now
        self._server_session_id: str | None = None  # the single WASAPI capture session
        self._lock = threading.Lock()
        self._decoder = threading.Thread(
            target=self._decode_loop, name="stenograph-live-decode", daemon=True
        )
        self._decoder.start()

    # -- public API -----------------------------------------------------------

    def status(self) -> dict:
        """Live sessions and the state of the transcription queue, for the UI."""
        with self._lock:
            sessions = list(self._sessions.values())
            serving = self._serving_id
            waiting = list(self._turn)
        items = []
        for session in sessions:
            position = waiting.index(session.job.id) + 1 if session.job.id in waiting else None
            items.append(
                {
                    "job_id": session.job.id,
                    "source_name": session.job.source_name,
                    "capture": session.job.meta.get("capture") or "server",
                    "tracks": list(session.tracks),
                    "started_at": session.job.started_at,
                    "lag_sec": session.lag_seconds(),
                    "transcribing": session.job.id == serving,
                    "queue_position": position,
                }
            )
        return {
            "active": bool(items),
            "supported": capture_module.capture_supported(),
            "sessions": items,
            "serving": serving,
        }

    def start(self, tracks: list[str] | None = None, language: str | None = None) -> Job:
        """Start a server-side capture session; one at a time (this machine's devices).

        Raises RuntimeError when a server-side capture session is already running.
        """
        with self._lock:
            if self._server_session_id is not None:
                raise RuntimeError("серверная live-сессия уже идёт")
        session = self._begin(
            tracks,
            language,
            capture_factory=self._capture_factory,
            source_name="Live-сессия",
            capture_mode=None,
            server=True,
        )
        return session.job

    def start_web(
        self, tracks: list[str] | None = None, language: str | None = None
    ) -> _LiveSession:
        """Start a browser-upload session; audio arrives via ``session.feed``.

        Any number of browser sessions can run at once; the decode queue
        transcribes them turn by turn.
        """
        return self._begin(
            tracks,
            language,
            capture_factory=None,
            source_name="Live-сессия (браузер)",
            capture_mode="browser",
            server=False,
        )

    def stop(self) -> Job | None:
        """Stop the server-side session and finalize it; None when idle."""
        with self._lock:
            session_id = self._server_session_id
        if session_id is None:
            return None
        return self.stop_session(session_id)

    def stop_session(self, job_id: str) -> Job | None:
        """Stop one session (browser or server) and wait (bounded) for finalization.

        The decode queue drains the remaining audio, flushes the tail and
        completes the job; this call waits for that to finish, so the returned
        job is usually already ``done``.
        """
        with self._lock:
            session = self._sessions.get(job_id)
        if session is None:
            return None
        session.request_stop()
        if not session.wait_finished(FINALIZE_WAIT_SEC):
            log.warning(
                "live-сессия %s: финализация не успела за %.0f с — продолжится в фоне",
                job_id,
                FINALIZE_WAIT_SEC,
            )
        log.info("live-сессия %s остановлена", job_id)
        return session.job

    def _begin(
        self,
        tracks: list[str] | None,
        language: str | None,
        *,
        capture_factory: CaptureFactory | None,
        source_name: str,
        capture_mode: str | None,
        server: bool,
    ) -> _LiveSession:
        """Create and start a session (shared by server-side and browser capture)."""
        selected = tuple(track for track in (tracks or DEFAULT_TRACKS) if track)
        invalid = [track for track in selected if track not in capture_module.TRACKS]
        if invalid or not selected:
            raise RuntimeError(f"недопустимые источники: {', '.join(invalid) or 'пусто'}")
        with self._lock:
            if server and self._server_session_id is not None:
                raise RuntimeError("серверная live-сессия уже идёт")
            job = Job(
                kind="live",
                source_name=source_name,
                status=JobStatus.RUNNING,
                started_at=time.time(),
                language=language or self._settings.language_or_none(),
            )
            job.meta["engine"] = f"whisper:{self._settings.live_model}"
            job.meta["tracks"] = list(selected)
            if capture_mode is not None:
                job.meta["capture"] = capture_mode
            else:
                try:
                    job.meta["devices"] = capture_module.describe_devices()
                except Exception:  # noqa: BLE001 — device info is best-effort metadata
                    log.debug("не удалось получить имена устройств", exc_info=True)
            self._repo.save(job)
            session = _LiveSession(
                job=job,
                settings=self._settings,
                repo=self._repo,
                bus=self._bus,
                transcriber_factory=self._transcriber_factory,
                capture_factory=capture_factory,
                tracks=selected,
            )
            self._sessions[job.id] = session
            if server:
                self._server_session_id = job.id
        try:
            session.start()
        except Exception as exc:
            with self._lock:
                if self._sessions.get(job.id) is session:
                    self._sessions.pop(job.id, None)
                if self._server_session_id == job.id:
                    self._server_session_id = None
            session.abort(str(exc))
            raise
        log.info("live-сессия %s запущена (%s)", job.id, ", ".join(selected))
        return session

    # -- decode queue (dedicated worker thread) --------------------------------

    def _decode_loop(self) -> None:
        """Serve live sessions from the shared queue, one turn at a time."""
        while True:
            served = False
            try:
                served = self._serve_next()
            except Exception:  # noqa: BLE001 — the loop must survive any session bug
                log.exception("live-декодер: непредвиденная ошибка")
            try:
                self._emit_levels()
            except Exception:  # noqa: BLE001
                log.exception("live-декодер: не удалось отправить уровни ввода")
            if not served:
                time.sleep(IDLE_SLEEP_SEC)

    def _serve_next(self) -> bool:
        """Give one bounded turn to the next queued session; False when idle."""
        with self._lock:
            self._refresh_turns()
            if not self._turn:
                return False
            job_id = self._turn.popleft()
            self._queued.discard(job_id)
            session = self._sessions.get(job_id)
            self._serving_id = job_id
        try:
            still_needs = (
                session.serve(
                    max_ticks=self._settings.live_turn_ticks,
                    max_sec=self._settings.live_turn_sec,
                )
                if session is not None
                else False
            )
        except Exception as exc:  # noqa: BLE001 — report through the job, keep serving others
            log.exception("live-сессия %s упала при транскрибации", job_id)
            if session is not None:
                session.fail(str(exc))
            still_needs = False
        finished = session is not None and session.finished
        with self._lock:
            self._serving_id = None
            if finished:
                if self._sessions.get(job_id) is session:
                    self._sessions.pop(job_id, None)
                if self._server_session_id == job_id:
                    self._server_session_id = None
            elif (
                still_needs
                and self._sessions.get(job_id) is session
                and job_id not in self._queued
            ):
                self._queued.add(job_id)
                self._turn.append(job_id)
        if finished and session is not None:
            self._chain_reprocess(session)
            session.notify_finished()
        return True

    def _refresh_turns(self) -> None:
        """Queue every session with pending work (call under the lock)."""
        for job_id, session in self._sessions.items():
            if job_id == self._serving_id or job_id in self._queued:
                continue
            if session.needs_turn():
                self._queued.add(job_id)
                self._turn.append(job_id)

    def _emit_levels(self) -> None:
        """Publish input level meters for every live session (throttled per session)."""
        with self._lock:
            sessions = list(self._sessions.values())
        for session in sessions:
            session.emit_levels()

    def _chain_reprocess(self, session: _LiveSession) -> None:
        """Hand a finished recording to the quality re-pass (best effort)."""
        if not (self._auto_reprocess and self._reprocess is not None):
            return
        if not session.has_audio or session.job.status not in (JobStatus.DONE, JobStatus.ERROR):
            return
        try:
            self._reprocess(session.job)
        except ValueError as exc:  # nothing recorded / nothing to improve
            log.info("live-сессия %s: улучшение записи пропущено (%s)", session.job.id, exc)
        except Exception:  # noqa: BLE001 — chaining must never break the queue
            log.exception("не удалось запустить улучшение записи %s", session.job.id)


class _LiveSession:
    """One capture + transcription session, served by the manager's queue."""

    def __init__(
        self,
        *,
        job: Job,
        settings: Settings,
        repo: JobRepository,
        bus: EventBus,
        transcriber_factory: TranscriberFactory,
        capture_factory: CaptureFactory | None,
        tracks: tuple[str, ...],
    ) -> None:
        self.job = job
        self._settings = settings
        self._repo = repo
        self._bus = bus
        self._transcriber_factory = transcriber_factory
        self._capture_factory = capture_factory
        self.tracks = tracks

        self._stop_requested = threading.Event()
        self._finished = threading.Event()
        self._finalized = False
        self._lock = threading.Lock()  # guards pending chunks, levels, segments
        self._pending: dict[str, list[np.ndarray]] = {track: [] for track in tracks}
        self._levels: dict[str, float] = {track: 0.0 for track in tracks}
        self._segments: list[Segment] = []
        self._trackers: dict[str, StreamTracker] = {}
        self._sources: list[Any] = []
        self._writers: dict[str, wave.Wave_write] = {}
        self._started_monotonic = time.monotonic()
        self._levels_emitted_at = 0.0
        self._chunks_written = 0

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        """Open writers and trackers, then (server-side) the capture sources."""
        audio_dir = self._settings.data_dir / "live" / self.job.id
        audio_dir.mkdir(parents=True, exist_ok=True)
        for track in self.tracks:
            path = audio_dir / f"{track}.wav"
            writer = wave.open(str(path), "wb")  # noqa: SIM115 — kept open for the session
            writer.setnchannels(1)
            writer.setsampwidth(2)
            writer.setframerate(SAMPLE_RATE)
            self._writers[track] = writer
            self.job.meta.setdefault("audio", {})[track] = str(path)
            self._trackers[track] = StreamTracker(
                self._transcriber_factory(self.job.language),
                on_final=self._on_final_factory(track),
                on_partial=self._on_partial_factory(track),
                step_sec=self._settings.live_step_sec,
                max_window_sec=self._settings.live_max_window_sec,
            )
        capture_factory = self._capture_factory
        if capture_factory is not None:
            try:
                for track in self.tracks:
                    source = capture_factory(
                        track, self._on_chunk, chunk_sec=self._settings.live_chunk_sec
                    )
                    source.start()
                    self._sources.append(source)
            except Exception:
                self._close_writers()
                for source in self._sources:
                    source.stop()
                raise
        self._repo.save(self.job)
        self._bus.publish(
            self.job.id,
            {"type": "status", "status": "running", "message": "Запись запущена", "progress": 0},
        )

    def feed(self, track: str, audio: np.ndarray) -> None:
        """Accept externally captured audio (browser upload); unknown tracks are ignored."""
        if track not in self._trackers:
            return
        self._on_chunk(track, audio)

    def request_stop(self) -> None:
        """Ask the queue to drain and finalize this session (idempotent)."""
        self._stop_requested.set()
        for source in self._sources:
            source.stop()

    def wait_finished(self, timeout: float) -> bool:
        """Block until the queue finalized the session; False on timeout."""
        return self._finished.wait(timeout)

    def notify_finished(self) -> None:
        """Wake stop_session waiters once the manager has deregistered the session."""
        self._finished.set()

    # -- queue interface (served by the decode worker) -------------------------

    @property
    def finished(self) -> bool:
        """True once the session has been finalized (done or failed)."""
        return self._finalized

    @property
    def has_audio(self) -> bool:
        """True when at least one chunk was written to disk."""
        return self._chunks_written > 0

    def needs_turn(self) -> bool:
        """True while this session waits for transcription work (or finalization)."""
        if self._finalized:
            return False
        if self._stop_requested.is_set():
            return True  # still has to drain the backlog, flush and finalize
        if self._has_pending():
            return True  # captured audio not yet moved into the trackers
        return any(tracker.ready() for tracker in self._trackers.values())

    def lag_seconds(self) -> float:
        """Audio waiting for transcription beyond the normal step (UI hint)."""
        if not self._trackers:
            return 0.0
        return round(max(tracker.lag_seconds for tracker in self._trackers.values()), 1)

    def serve(self, *, max_ticks: int, max_sec: float) -> bool:
        """One bounded turn of transcription on the decode worker.

        Feeds buffered audio into the trackers and runs up to ``max_ticks``
        inferences (bounded by ``max_sec`` wall time), then yields to the rest
        of the queue. Returns True while the session still needs more turns.
        """
        if self._finalized:
            return False
        self._feed_pending()
        self._tick_budget(max_ticks=max_ticks, max_sec=max_sec)
        if self._stop_requested.is_set() and not self._has_ready():
            self._finalize()
            return False
        return self.needs_turn()

    def emit_levels(self) -> None:
        """Publish input level meters (throttled to ~3 per second)."""
        now = time.monotonic()
        if now - self._levels_emitted_at < 0.3:
            return
        self._levels_emitted_at = now
        with self._lock:
            levels = {track: round(value, 4) for track, value in self._levels.items()}
            for track in self._levels:
                self._levels[track] *= 0.5  # decay, so meters fall back in silence
        for track, rms in levels.items():
            self._bus.publish(self.job.id, {"type": "level", "track": track, "rms": rms})

    # -- capture callbacks (capture threads) -------------------------------

    def _on_chunk(self, track: str, audio: np.ndarray) -> None:
        """Buffer captured audio and append it to the track's WAV file."""
        if self._stop_requested.is_set():
            return
        level = float(np.sqrt(np.mean(np.square(audio.astype(np.float64))))) if audio.size else 0.0
        with self._lock:
            self._pending[track].append(audio)
            self._levels[track] = max(level, self._levels[track] * 0.8)
        writer = self._writers.get(track)
        if writer is not None:
            frames = (np.clip(audio, -1.0, 1.0) * 32767.0).astype("<i2")
            try:
                writer.writeframes(frames.tobytes())
                self._chunks_written += 1
            except Exception:  # noqa: BLE001 — a broken writer must not kill capture
                log.exception("не удалось записать аудио трека «%s»", track)

    # -- decode pump internals (decode worker thread) ------------------------

    def _feed_pending(self) -> None:
        """Move buffered chunks into the trackers."""
        for track, tracker in self._trackers.items():
            with self._lock:
                chunks = self._pending[track]
                self._pending[track] = []
            for chunk in chunks:
                tracker.feed(chunk)

    def _tick_budget(self, *, max_ticks: int, max_sec: float) -> None:
        """Run up to ``max_ticks`` inferences over ready tracks (wall-time bounded)."""
        deadline = time.monotonic() + max_sec
        ticks = 0
        while ticks < max_ticks and time.monotonic() < deadline:
            ran = False
            for tracker in self._trackers.values():
                if not tracker.ready():
                    continue
                tracker.tick()
                ticks += 1
                ran = True
                if ticks >= max_ticks:
                    break
            if not ran:
                break

    def _has_ready(self) -> bool:
        """True when any track has a full step of new audio awaiting inference."""
        return any(tracker.ready() for tracker in self._trackers.values())

    def _has_pending(self) -> bool:
        """True when captured chunks are waiting to be moved into the trackers."""
        with self._lock:
            return any(chunks for chunks in self._pending.values())

    def _on_final_factory(self, track: str) -> Callable[[float, float, str], None]:
        def on_final(start: float, end: float, text: str) -> None:
            text = text.strip()
            if not text:
                return
            segment = Segment(
                index=len(self._segments),
                start=round(max(0.0, start), 2),
                end=round(max(start, end), 2),
                text=text,
                speaker=capture_module.track_label(track),
            )
            with self._lock:
                self._segments.append(segment)
            self._bus.publish(self.job.id, {"type": "segment", "segment": segment.model_dump()})
            self._persist()

        return on_final

    def _on_partial_factory(self, track: str) -> Callable[[str], None]:
        def on_partial(text: str) -> None:
            self._bus.publish(
                self.job.id,
                {
                    "type": "partial",
                    "track": track,
                    "speaker": capture_module.track_label(track),
                    "text": text,
                },
            )

        return on_partial

    # -- finalization (decode worker thread) ----------------------------------

    def _persist(self) -> None:
        with self._lock:
            ordered = sorted(self._segments, key=lambda item: item.start)
            for position, segment in enumerate(ordered):
                segment.index = position
            self.job.segments = list(ordered)
        self._repo.save(self.job)

    def _finalize(self) -> None:
        """Flush the tail, close writers and complete the job (decode worker)."""
        if self._finalized:
            return
        error: str | None = None
        try:
            for tracker in self._trackers.values():
                tracker.flush()
        except Exception as exc:  # noqa: BLE001 — a broken tail must still finalize
            log.exception("live-сессия %s: финальный проход упал", self.job.id)
            error = str(exc)
        self._close_writers()
        duration = time.monotonic() - self._started_monotonic
        self._persist()
        if error is None:
            with self._lock:
                ordered = list(self.job.segments)
            self.job.text = "\n".join(f"{item.speaker}: {item.text}" for item in ordered)
            self.job.status = JobStatus.DONE
            self.job.progress = 100
            self.job.message = "Запись завершена"
            self.job.finished_at = time.time()
            self.job.meta["duration"] = round(duration, 2)
            self.job.meta["processing_seconds"] = round(duration, 2)
            self._repo.save(self.job)
            self._bus.publish(
                self.job.id, {"type": "done", "text": self.job.text, "meta": self.job.meta}
            )
            log.info(
                "live-сессия %s завершена: %d сегментов за %.1f с",
                self.job.id,
                len(ordered),
                duration,
            )
        else:
            self._fail_job(error)
        self._finalized = True

    def fail(self, reason: str) -> None:
        """Mark the session as failed after a decode error (decode worker)."""
        if self._finalized:
            return
        self._close_writers()
        self._fail_job(reason)
        self._finalized = True

    def abort(self, reason: str) -> None:
        """Mark the session as failed when capture could not start."""
        self.fail(f"не удалось запустить захват: {reason}")

    def _fail_job(self, reason: str) -> None:
        """Set the job to error and notify subscribers."""
        self.job.status = JobStatus.ERROR
        self.job.error = reason
        self.job.finished_at = time.time()
        self._repo.save(self.job)
        self._bus.publish(self.job.id, {"type": "error", "message": reason})

    def _close_writers(self) -> None:
        for writer in self._writers.values():
            try:
                writer.close()
            except Exception:  # noqa: BLE001
                log.debug("не удалось закрыть WAV-файл", exc_info=True)
        self._writers = {}
