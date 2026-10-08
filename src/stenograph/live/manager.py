"""Live session lifecycle: capture → streaming decode → persistence/events.

A session is stored as a job (kind="live"), so the same SSE endpoint and the
job page work for it as for file jobs. One session at a time; it owns the GPU
while running. Per-track audio is written to ``data/live/<job id>/<track>.wav``
so a quality offline pass can be run over the recording later.
"""

from __future__ import annotations

import logging
import threading
import time
import wave
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
    """Owns the single active live session."""

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
        self._session: _LiveSession | None = None
        self._lock = threading.Lock()

    def status(self) -> dict:
        """Current session state for the UI."""
        with self._lock:
            session = self._session
        supported = capture_module.capture_supported()
        if session is None:
            return {"active": False, "supported": supported}
        return {"active": True, "supported": supported, "job": session.job.model_dump()}

    def start(self, tracks: list[str] | None = None, language: str | None = None) -> Job:
        """Start a live session; raises RuntimeError when one is already running."""
        with self._lock:
            if self._session is not None:
                raise RuntimeError("live-сессия уже идёт")
            selected = tuple(track for track in (tracks or DEFAULT_TRACKS) if track)
            invalid = [track for track in selected if track not in capture_module.TRACKS]
            if invalid or not selected:
                raise RuntimeError(f"недопустимые источники: {', '.join(invalid) or 'пусто'}")
            job = Job(
                kind="live",
                source_name="Live-сессия",
                status=JobStatus.RUNNING,
                started_at=time.time(),
                language=language or self._settings.language_or_none(),
            )
            job.meta["engine"] = f"whisper:{self._settings.live_model}"
            job.meta["tracks"] = list(selected)
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
                capture_factory=self._capture_factory,
                tracks=selected,
            )
            self._session = session
        try:
            session.start()
        except Exception as exc:
            with self._lock:
                self._session = None
            session.abort(str(exc))
            raise
        log.info("live-сессия %s запущена (%s)", job.id, ", ".join(selected))
        return job

    def stop(self) -> Job | None:
        """Stop the active session and finalize it; None when idle."""
        with self._lock:
            session = self._session
        if session is None:
            return None
        session.stop()
        with self._lock:
            if self._session is session:
                self._session = None
        if self._auto_reprocess and self._reprocess is not None:
            try:
                self._reprocess(session.job)
            except Exception:  # noqa: BLE001 — chaining must never break stopping
                log.exception("не удалось запустить улучшение записи %s", session.job.id)
        log.info("live-сессия %s остановлена", session.job.id)
        return session.job


class _LiveSession:
    """One running capture + decode session."""

    def __init__(
        self,
        *,
        job: Job,
        settings: Settings,
        repo: JobRepository,
        bus: EventBus,
        transcriber_factory: TranscriberFactory,
        capture_factory: CaptureFactory,
        tracks: tuple[str, ...],
    ) -> None:
        self.job = job
        self._settings = settings
        self._repo = repo
        self._bus = bus
        self._transcriber_factory = transcriber_factory
        self._capture_factory = capture_factory
        self.tracks = tracks

        self._stop_event = threading.Event()
        self._lock = threading.Lock()  # guards pending chunks, levels, segments
        self._pending: dict[str, list[np.ndarray]] = {track: [] for track in tracks}
        self._levels: dict[str, float] = {track: 0.0 for track in tracks}
        self._segments: list[Segment] = []
        self._trackers: dict[str, StreamTracker] = {}
        self._sources: list[Any] = []
        self._writers: dict[str, wave.Wave_write] = {}
        self._started_monotonic = time.monotonic()
        self._levels_emitted_at = 0.0
        self._worker: threading.Thread | None = None

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        """Open writers and sources, then start the decode pump."""
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
        try:
            for track in self.tracks:
                source = self._capture_factory(
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
        self._worker = threading.Thread(
            target=self._pump_loop, name=f"stenograph-live-{self.job.id}", daemon=True
        )
        self._worker.start()

    def stop(self) -> None:
        """Signal capture to stop, flush the trackers and finalize."""
        self._stop_event.set()
        for source in self._sources:
            source.stop()
        worker = self._worker
        if worker is not None:
            worker.join(timeout=60.0)
        for source in self._sources:
            source.join(timeout=5.0)
        if worker is not None and worker.is_alive():  # pragma: no cover
            log.warning("live-воркер %s не завершился за 60 с", self.job.id)

    def abort(self, reason: str) -> None:
        """Mark the session as failed (e.g. a device could not be opened)."""
        self._stop_event.set()
        self._close_writers()
        self.job.status = JobStatus.ERROR
        self.job.error = f"не удалось запустить захват: {reason}"
        self.job.finished_at = time.time()
        self._repo.save(self.job)
        self._bus.publish(self.job.id, {"type": "error", "message": self.job.error})

    # -- capture callbacks (capture threads) --------------------------------

    def _on_chunk(self, track: str, audio: np.ndarray) -> None:
        """Buffer captured audio and append it to the track's WAV file."""
        level = float(np.sqrt(np.mean(np.square(audio.astype(np.float64))))) if audio.size else 0.0
        with self._lock:
            self._pending[track].append(audio)
            self._levels[track] = max(level, self._levels[track] * 0.8)
        writer = self._writers.get(track)
        if writer is not None:
            frames = (np.clip(audio, -1.0, 1.0) * 32767.0).astype("<i2")
            try:
                writer.writeframes(frames.tobytes())
            except Exception:  # noqa: BLE001 — a broken writer must not kill capture
                log.exception("не удалось записать аудио трека «%s»", track)

    # -- decode pump (worker thread) ----------------------------------------

    def _pump_loop(self) -> None:
        try:
            while not self._stop_event.is_set():
                self._pump_tracks()
                self._emit_levels()
                time.sleep(0.15)
            self._pump_tracks()
            for tracker in self._trackers.values():
                tracker.flush()
            self._finish()
        except Exception as exc:  # noqa: BLE001 — report and keep the app alive
            log.exception("live-сессия %s упала", self.job.id)
            self._fail(str(exc))

    def _pump_tracks(self) -> None:
        for track, tracker in self._trackers.items():
            with self._lock:
                chunks = self._pending[track]
                self._pending[track] = []
            for chunk in chunks:
                tracker.feed(chunk)
            if chunks:
                tracker.tick()

    def _emit_levels(self) -> None:
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

    # -- finalization ---------------------------------------------------------

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

    def _fail(self, reason: str) -> None:
        self._close_writers()
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
