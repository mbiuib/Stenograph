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
    ProgressCallback,
    SegmentCallback,
    TranscribeOptions,
    TranscribeProgress,
)
from .events import EventBus
from .media import AUDIO_EXTS, extract_audio, probe
from .storage import JobRepository

log = logging.getLogger(__name__)

PROBE_UNTIL = 2  # progress % reserved for metadata probing
EXTRACT_UNTIL = 5  # ... and for audio extraction
ASR_UNTIL = 99  # ASR fills everything up to this
REPROCESS_UNTIL = 99  # reprocess spreads track progress up to this


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
            extract_audio(source, audio_path, settings)

        transition(JobStatus.RUNNING, "Транскрибация…", EXTRACT_UNTIL)

        def on_progress(tick: TranscribeProgress) -> None:
            job.progress = EXTRACT_UNTIL + int(tick.fraction * (ASR_UNTIL - EXTRACT_UNTIL))
            job.message = tick.message
            emit({"type": "progress", "value": job.progress, "message": tick.message})

        streamed = 0

        def on_segment(segment: Segment) -> None:
            nonlocal streamed
            streamed += 1
            emit({"type": "segment", "segment": segment.model_dump()})

        result = engine.transcribe(
            audio_path,
            options,
            on_progress=on_progress,
            on_segment=on_segment,
            is_cancelled=is_cancelled,
        )

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
        transition(JobStatus.DONE, "Готово", 100)
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
    Segments of all tracks are merged into one timeline.
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
        language = ""
        span = (REPROCESS_UNTIL - 1) / len(tracks)

        def make_callbacks(
            track: str, index: int, base: float
        ) -> tuple[ProgressCallback, SegmentCallback]:
            def on_progress(tick: TranscribeProgress) -> None:
                value = int(base + tick.fraction * span)
                job.progress = value
                job.message = f"Дорожка {index + 1}/{len(tracks)}: {tick.message}"
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
            result = engine.transcribe(
                path,
                options,
                on_progress=on_progress,
                on_segment=on_segment,
                is_cancelled=is_cancelled,
            )
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
        transition(JobStatus.DONE, "Готово", 100)
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
