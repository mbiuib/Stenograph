"""File transcription pipeline: probe -> extract audio -> ASR -> persist.

The pipeline owns all state transitions for a job: bus events for every
stage, persistence after every transition, cooperative cancellation. The
reprocess pipeline additionally re-transcribes recorded live tracks and
merges them into a single diarized transcript.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .config import Settings
from .domain.errors import JobCancelled
from .domain.models import Job, JobStatus, Segment
from .engines.base import (
    AsrEngine,
    AsrResult,
    NoSpeechError,
    ProgressCallback,
    SegmentCallback,
    TranscribeOptions,
    TranscribeProgress,
)
from .events import EventBus
from .llm.analyzer import analyze
from .llm.client import LlmClient
from .media import AUDIO_EXTS, extract_audio, probe
from .storage import JobRepository

log = logging.getLogger(__name__)

PROBE_UNTIL = 2  # progress % reserved for metadata probing
EXTRACT_UNTIL = 5  # ... and for audio extraction
ASR_UNTIL = 99  # ASR fills everything up to this
REPROCESS_UNTIL = 99  # reprocess spreads track progress up to this
ANALYSIS_UNTIL = 99  # analysis spreads LLM step progress up to this
PROGRESS_SAVE_SEC = 1.0  # throttle for repository writes during progress ticks


def _progress_saver(repo: JobRepository, job: Job) -> Callable[[], None]:
    """Repository write throttled to PROGRESS_SAVE_SEC for progress ticks.

    The job list polls the REST API, which reads the repository — progress
    must be persisted, but not on every engine tick; once a second is plenty.
    """
    last = 0.0

    def save() -> None:
        nonlocal last
        now = time.time()
        if now - last >= PROGRESS_SAVE_SEC:
            last = now
            repo.save(job)

    return save


def run_file_job(
    job: Job,
    *,
    settings: Settings,
    repo: JobRepository,
    bus: EventBus,
    engine: AsrEngine,
    options: TranscribeOptions,
    is_cancelled: Callable[[], bool],
) -> None:
    """Run the whole file pipeline for one job, updating the repository and bus."""

    def emit(event: dict[str, Any]) -> None:
        bus.publish(job.id, event)

    def transition(status: JobStatus, message: str, progress: int) -> None:
        job.status = status
        job.message = message
        job.progress = progress
        repo.save(job)
        emit({"type": "status", "status": str(status), "message": message, "progress": progress})

    try:
        job.started_at = time.time()
        transition(JobStatus.RUNNING, "Чтение метаданных…", 1)

        source = Path(job.source_path or "")
        if not source.is_file():
            raise FileNotFoundError(f"файл не найден: {source}")
        meta = probe(source, settings)
        if meta:
            job.meta.update(meta)
            emit({"type": "meta", "meta": meta})
            if meta.get("has_audio") is False:
                raise ValueError("в файле нет аудиодорожки")

        audio_path = source
        if source.suffix.lower() not in AUDIO_EXTS:
            transition(JobStatus.RUNNING, "Извлечение аудиодорожки…", PROBE_UNTIL + 1)
            audio_path = settings.work_dir / f"{job.id}.wav"
            try:
                extract_audio(source, audio_path, settings)
            except ValueError as exc:
                # Name the user's file, not the uuid copy on disk.
                raise ValueError(str(exc).replace(source.name, job.source_name)) from exc

        transition(JobStatus.RUNNING, "Транскрибация…", EXTRACT_UNTIL)

        save_progress = _progress_saver(repo, job)

        def on_progress(tick: TranscribeProgress) -> None:
            job.progress = EXTRACT_UNTIL + int(tick.fraction * (ASR_UNTIL - EXTRACT_UNTIL))
            job.message = tick.message
            save_progress()
            emit({"type": "progress", "value": job.progress, "message": tick.message})

        streamed = 0

        def on_segment(segment: Segment) -> None:
            nonlocal streamed
            streamed += 1
            emit({"type": "segment", "segment": segment.model_dump()})

        try:
            result = engine.transcribe(
                audio_path,
                options,
                on_progress=on_progress,
                on_segment=on_segment,
                is_cancelled=is_cancelled,
            )
        except NoSpeechError:
            # Пустой результат на тишине — честный исход, а не сбой.
            result = AsrResult(duration=float(job.meta.get("duration") or 0.0))

        job.language = result.language or job.language
        speakers = sorted({s.speaker for s in result.segments if s.speaker})
        job.meta.update(
            {
                "language": result.language,
                "language_probability": result.language_probability,
                "duration": result.duration,
                "engine": engine.name,
                "speakers": speakers,
                "diarized": bool(speakers),
            }
        )
        if not result.segments:
            job.meta["no_speech"] = True
        if len(result.segments) != streamed:
            # Long segments were re-split: the UI must replace what it streamed.
            emit(
                {
                    "type": "segments_replaced",
                    "segments": [s.model_dump() for s in result.segments],
                }
            )
        job.segments = result.segments
        job.text = "\n".join(s.text for s in result.segments).strip()

        job.finished_at = time.time()
        job.meta["processing_seconds"] = round(
            job.finished_at - (job.started_at or job.finished_at), 2
        )
        transition(JobStatus.DONE, "Готово" if result.segments else "Речь не обнаружена", 100)
        emit({"type": "done", "text": job.text, "meta": job.meta})
        log.info(
            "job %s done: %d segments, %.1f s audio", job.id, len(job.segments), result.duration
        )

        if audio_path != source:
            audio_path.unlink(missing_ok=True)

    except JobCancelled:
        job.finished_at = time.time()
        transition(JobStatus.CANCELLED, "Отменено", job.progress)
        emit({"type": "cancelled"})

    except Exception as exc:  # any engine/media failure: report it through the job
        log.exception("job %s failed", job.id)
        job.error = f"{type(exc).__name__}: {exc}"
        job.finished_at = time.time()
        transition(JobStatus.ERROR, job.error, job.progress)
        emit({"type": "error", "message": job.error})


def run_reprocess_job(
    job: Job,
    *,
    settings: Settings,
    repo: JobRepository,
    bus: EventBus,
    engine: AsrEngine,
    options: TranscribeOptions,
    is_cancelled: Callable[[], bool],
) -> None:
    """Re-transcribe a live recording track by track and merge into one transcript.

    Offline improvement pass of a live session: every recorded track is run
    through the engine from scratch (moss by default); microphone segments are
    labelled «Вы», diarized system-track speakers keep their engine labels.
    Segments of all tracks are merged into one timeline. Tracks without speech
    are skipped; a recording with no speech at all completes with an empty
    transcript instead of failing.
    """

    def emit(event: dict[str, Any]) -> None:
        bus.publish(job.id, event)

    def transition(status: JobStatus, message: str, progress: int) -> None:
        job.status = status
        job.message = message
        job.progress = progress
        repo.save(job)
        emit({"type": "status", "status": str(status), "message": message, "progress": progress})

    try:
        job.started_at = time.time()
        audio = {str(key): str(value) for key, value in (job.meta.get("audio") or {}).items()}
        declared = job.meta.get("tracks") or ("system", "mic")
        tracks = [track for track in declared if track in audio]
        if not tracks:
            raise ValueError("у задачи нет дорожек для обработки")
        transition(JobStatus.RUNNING, "Подготовка…", 1)

        gathered: list[Segment] = []
        durations: dict[str, float] = {}
        speechless: list[str] = []
        language = ""
        span = (REPROCESS_UNTIL - 1) / len(tracks)
        save_progress = _progress_saver(repo, job)

        def make_callbacks(
            track: str, index: int, base: float
        ) -> tuple[ProgressCallback, SegmentCallback]:
            def on_progress(tick: TranscribeProgress) -> None:
                value = int(base + tick.fraction * span)
                job.progress = value
                job.message = f"Дорожка {index + 1}/{len(tracks)}: {tick.message}"
                save_progress()
                emit({"type": "progress", "value": value, "message": job.message})

            def on_segment(segment: Segment) -> None:
                if track == "mic":
                    segment = segment.model_copy(update={"speaker": "Вы"})
                emit({"type": "segment", "segment": segment.model_dump()})

            return on_progress, on_segment

        for index, track in enumerate(tracks):
            if is_cancelled():
                raise JobCancelled()
            path = Path(audio[track])
            if not path.is_file():
                raise FileNotFoundError(f"дорожка не найдена: {path}")
            base = 1 + span * index
            label = "микрофон" if track == "mic" else "системный звук"
            transition(
                JobStatus.RUNNING, f"Дорожка {index + 1}/{len(tracks)}: {label}…", int(base)
            )
            on_progress, on_segment = make_callbacks(track, index, base)
            try:
                result = engine.transcribe(
                    path,
                    options,
                    on_progress=on_progress,
                    on_segment=on_segment,
                    is_cancelled=is_cancelled,
                )
            except NoSpeechError:
                # Безречевая дорожка (тишина, музыка без речи) — не сбой.
                speechless.append(track)
                log.info("reprocess %s: track %s has no speech, skipped", job.id, track)
                continue
            durations[track] = result.duration
            language = language or result.language
            for segment in result.segments:
                if track == "mic":
                    segment = segment.model_copy(update={"speaker": "Вы"})
                gathered.append(segment)

        merged = sorted(gathered, key=lambda item: item.start)
        for position, segment in enumerate(merged):
            segment.index = position
        speakers = sorted({segment.speaker for segment in merged if segment.speaker})
        job.language = language or job.language
        job.meta.update(
            {
                "language": language,
                "duration": max(durations.values(), default=0.0),
                "track_durations": durations,
                "engine": engine.name,
                "speakers": speakers,
                "diarized": bool(speakers),
            }
        )
        if speechless:
            job.meta["speechless_tracks"] = speechless
        if not merged:
            job.meta["no_speech"] = True
        emit({"type": "segments_replaced", "segments": [item.model_dump() for item in merged]})
        job.segments = merged
        job.text = "\n".join(
            f"{segment.speaker}: {segment.text}" if segment.speaker else segment.text
            for segment in merged
        ).strip()

        job.finished_at = time.time()
        job.meta["processing_seconds"] = round(
            job.finished_at - (job.started_at or job.finished_at), 2
        )
        transition(JobStatus.DONE, "Готово" if merged else "Речь не обнаружена", 100)
        emit({"type": "done", "text": job.text, "meta": job.meta})
        log.info(
            "reprocess job %s done: %d segments from %d track(s)",
            job.id,
            len(merged),
            len(tracks),
        )

    except JobCancelled:
        job.finished_at = time.time()
        transition(JobStatus.CANCELLED, "Отменено", job.progress)
        emit({"type": "cancelled"})

    except Exception as exc:  # noqa: BLE001 — any failure is reported through the job
        log.exception("reprocess job %s failed", job.id)
        job.error = f"{type(exc).__name__}: {exc}"
        job.finished_at = time.time()
        transition(JobStatus.ERROR, job.error, job.progress)
        emit({"type": "error", "message": job.error})


def run_analysis_job(
    job: Job,
    *,
    settings: Settings,
    repo: JobRepository,
    bus: EventBus,
    client: LlmClient,
    is_cancelled: Callable[[], bool],
) -> None:
    """Generate a protocol or summary for a finished job via the local LLM.

    The child job keeps the markdown result as its ``text``; the parent job
    gets it back in ``meta.analysis`` so the meeting page can render it
    without an extra request.
    """

    def emit(event: dict[str, Any]) -> None:
        bus.publish(job.id, event)

    def transition(status: JobStatus, message: str, progress: int) -> None:
        job.status = status
        job.message = message
        job.progress = progress
        repo.save(job)
        emit({"type": "status", "status": str(status), "message": message, "progress": progress})

    try:
        job.started_at = time.time()
        transition(JobStatus.RUNNING, "Подготовка…", 1)

        parent_id = job.meta.get("parent")
        parent = repo.get(str(parent_id)) if parent_id else None
        if parent is None:
            raise ValueError("исходная задача не найдена")
        analysis_type = str(job.meta.get("analysis_type") or "protocol")

        def on_progress(step: int, total: int, message: str) -> None:
            value = 2 + int(step / max(total, 1) * (ANALYSIS_UNTIL - 2))
            job.progress = min(value, ANALYSIS_UNTIL)
            job.message = message
            repo.save(job)  # the page polls the API, so chunks must be visible
            emit({"type": "progress", "value": job.progress, "message": message})

        text, analysis_meta = analyze(
            parent,
            analysis_type,
            client,
            max_chars=settings.llm_chunk_chars,
            on_progress=on_progress,
            is_cancelled=is_cancelled,
        )

        job.text = text
        job.meta.update(analysis_meta)
        job.language = parent.language or job.language
        job.finished_at = time.time()
        job.meta["processing_seconds"] = round(
            job.finished_at - (job.started_at or job.finished_at), 2
        )
        transition(JobStatus.DONE, "Готово", 100)
        emit({"type": "done", "text": text, "meta": job.meta})
        log.info("analysis job %s done: %d chars (%s)", job.id, len(text), analysis_type)

        analysis = parent.meta.setdefault("analysis", {})
        entry = analysis.get(analysis_type) or {}
        entry.update(
            {
                "job_id": job.id,
                "text": text,
                "model": analysis_meta.get("model"),
                "finished_at": job.finished_at,
            }
        )
        analysis[analysis_type] = entry
        parent.meta["analysis"] = analysis
        repo.save(parent)
        bus.publish(parent.id, {"type": "meta", "meta": parent.meta})

    except JobCancelled:
        job.finished_at = time.time()
        transition(JobStatus.CANCELLED, "Отменено", job.progress)
        emit({"type": "cancelled"})

    except Exception as exc:  # noqa: BLE001 — any failure is reported through the job
        log.exception("analysis job %s failed", job.id)
        job.error = f"{type(exc).__name__}: {exc}"
        job.finished_at = time.time()
        transition(JobStatus.ERROR, job.error, job.progress)
        emit({"type": "error", "message": job.error})
