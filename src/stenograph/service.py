"""Application service: job intake, a single-worker queue, cancellation.

All GPU work happens on one worker thread (Whisper is not GPU-concurrency
friendly); the queue is FIFO. Live sessions will get priority over files in a
later milestone.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections.abc import Callable
from pathlib import Path

from .config import Settings, get_settings
from .domain.models import Job, JobStatus
from .engines import get_asr
from .engines.base import AsrEngine, TranscribeOptions
from .events import EventBus
from .llm.analyzer import transcript_text
from .llm.client import LlmClient
from .llm.prompts import ANALYSIS_TYPES
from .pipeline import run_analysis_job, run_file_job, run_reprocess_job
from .storage import JobRepository

log = logging.getLogger(__name__)

EngineFactory = Callable[[str, Settings], AsrEngine]


class TranscriptionService:
    """Owns the job queue, the worker thread and the engine instances."""

    def __init__(
        self,
        settings: Settings,
        repo: JobRepository,
        bus: EventBus,
        engine_name: str | None = None,
        engine_factory: EngineFactory | None = None,
        llm_client: LlmClient | None = None,
    ) -> None:
        self.settings = settings
        self.repo = repo
        self.bus = bus
        self.engine_name = engine_name or settings.engine
        self._engine_factory = engine_factory
        self._llm = llm_client
        self._engines: dict[str, AsrEngine] = {}  # created lazily on the worker thread
        self._queue: queue.Queue[str] = queue.Queue()
        self._waiting: list[str] = []
        self._active_job_id: str | None = None
        self._cancel_events: dict[str, threading.Event] = {}
        self._lock = threading.Lock()
        self._worker = threading.Thread(
            target=self._work_loop, name="stenograph-worker", daemon=True
        )
        self._worker.start()

    def submit_file(
        self,
        source_path: Path,
        *,
        source_name: str | None = None,
        language: str | None = None,
        engine: str | None = None,
    ) -> Job:
        """Register a new file job and put it on the queue."""
        job = Job(
            kind="file",
            source_name=source_name or source_path.name,
            source_path=str(source_path),
        )
        job.meta["request"] = {"language": language, "engine": engine}
        return self._enqueue(job)

    def reprocess_job(self, live_job: Job, *, engine: str | None = None) -> Job:
        """Queue an offline re-transcription of a live session recording.

        One child job per session: every recorded track is re-transcribed from
        scratch (the default engine — moss — unless overridden) and merged into
        a single diarized transcript. Idempotent while a run is queued/running:
        returns the already existing child job.
        """
        audio = {
            str(track): str(path) for track, path in (live_job.meta.get("audio") or {}).items()
        }
        tracks = [
            track
            for track in ("system", "mic")
            if audio.get(track) and Path(audio[track]).is_file()
        ]
        if not tracks:
            raise ValueError("у записи нет сохранённых дорожек")

        existing_id = live_job.meta.get("reprocess_job")
        if existing_id:
            existing = self.repo.get(str(existing_id))
            if existing and existing.status in (JobStatus.QUEUED, JobStatus.RUNNING):
                return existing

        job = Job(kind="reprocess", source_name=f"Улучшение записи — {live_job.source_name}")
        job.meta["parent"] = live_job.id
        job.meta["tracks"] = tracks
        job.meta["audio"] = {track: audio[track] for track in tracks}
        job.meta["request"] = {
            "engine": engine,
            "language": live_job.language or self.settings.language_or_none(),
        }
        self._enqueue(job)

        live_job.meta["reprocess_job"] = job.id
        self.repo.save(live_job)
        self.bus.publish(live_job.id, {"type": "meta", "meta": live_job.meta})
        return job

    def retry_file_job(
        self, job: Job, *, engine: str | None = None, language: str | None = None
    ) -> Job:
        """Queue the same uploaded file for a fresh transcription run.

        Any finished file job (done, error, cancelled) can be retried; the
        original request options are reused unless overridden. A new job is
        created so the failed attempt stays visible in the history.
        """
        if job.kind != "file":
            raise ValueError("перезапустить можно только файловую задачу")
        if job.status in (JobStatus.QUEUED, JobStatus.RUNNING):
            raise ValueError("задача ещё выполняется — дождитесь её завершения")
        source = Path(job.source_path or "")
        if not source.is_file():
            raise ValueError("исходный файл больше недоступен в хранилище")

        request = job.meta.get("request") or {}
        new_job = Job(
            kind="file",
            source_name=job.source_name,
            source_path=str(source),
        )
        new_job.meta["request"] = {
            "language": language if language is not None else request.get("language"),
            "engine": engine if engine is not None else request.get("engine"),
        }
        new_job.meta["retry_of"] = job.id
        return self._enqueue(new_job)

    def update_job(
        self,
        job: Job,
        *,
        source_name: str | None = None,
        speaker_names: dict[str, str] | None = None,
    ) -> Job:
        """Rename a job and/or map diarized speaker labels to human names."""
        if source_name is not None:
            cleaned = source_name.strip()
            if not cleaned:
                raise ValueError("имя задачи не может быть пустым")
            if len(cleaned) > 200:
                raise ValueError("имя задачи слишком длинное (максимум 200 символов)")
            job.source_name = cleaned
        if speaker_names is not None:
            mapping = {
                str(key).strip(): str(value).strip()
                for key, value in speaker_names.items()
                if str(key).strip() and str(value).strip()
            }
            if mapping:
                job.meta["speaker_names"] = mapping
            else:
                job.meta.pop("speaker_names", None)
        self.repo.save(job)
        self.bus.publish(job.id, {"type": "meta", "meta": job.meta})
        return job

    def request_analysis(self, job: Job, analysis_type: str) -> Job:
        """Queue protocol/summary generation for a finished job.

        One child job per analysis type; while it is queued or running the
        same child is returned (idempotent). The parent gets a back-reference
        in ``meta.analysis`` immediately and the full text once the child
        finishes.
        """
        if analysis_type not in ANALYSIS_TYPES:
            raise ValueError(f"неизвестный тип анализа: {analysis_type}")
        if job.kind == "analysis":
            raise ValueError("нельзя анализировать результат анализа")
        if not transcript_text(job):
            raise ValueError("у задачи нет транскрипта")

        existing_id = ((job.meta.get("analysis") or {}).get(analysis_type) or {}).get("job_id")
        if existing_id:
            existing = self.repo.get(str(existing_id))
            if existing and existing.status in (JobStatus.QUEUED, JobStatus.RUNNING):
                return existing

        titles = {"protocol": "Протокол", "summary": "Резюме"}
        child = Job(kind="analysis", source_name=f"{titles[analysis_type]} — {job.source_name}")
        child.meta["parent"] = job.id
        child.meta["analysis_type"] = analysis_type
        self._enqueue(child)

        analysis = job.meta.setdefault("analysis", {})
        analysis[analysis_type] = {"job_id": child.id, "created_at": time.time()}
        job.meta["analysis"] = analysis
        self.repo.save(job)
        self.bus.publish(job.id, {"type": "meta", "meta": job.meta})
        return child

    def _enqueue(self, job: Job) -> Job:
        """Persist a job and put it on the single-worker queue."""
        self.repo.save(job)
        with self._lock:
            self._cancel_events[job.id] = threading.Event()
            self._waiting.append(job.id)
        self._queue.put(job.id)
        log.info("queued job %s (%s)", job.id, job.source_name)
        return job

    def cancel(self, job_id: str) -> bool:
        """Request cooperative cancellation; False if the job is not active."""
        with self._lock:
            event = self._cancel_events.get(job_id)
        if event is None:
            return False
        event.set()
        return True

    def get(self, job_id: str) -> Job | None:
        """Fetch a job from the repository."""
        return self.repo.get(job_id)

    def list_jobs(
        self, *, status: JobStatus | None = None, limit: int = 100, offset: int = 0
    ) -> list[Job]:
        """List jobs, newest first."""
        return self.repo.list_jobs(status=status, limit=limit, offset=offset)

    def delete(self, job_id: str) -> None:
        """Delete a job from the repository."""
        self.repo.delete(job_id)

    def queue_view(self) -> dict:
        """Snapshot of the work queue: the active job and the waiting ones."""
        with self._lock:
            active_id = self._active_job_id
            waiting_ids = list(self._waiting)
        active = self.repo.get(active_id) if active_id else None
        waiting = [job for job in (self.repo.get(item) for item in waiting_ids) if job]
        return {"active": active, "waiting": waiting}

    # -- worker -------------------------------------------------------------

    def _work_loop(self) -> None:
        while True:
            job_id = self._queue.get()
            try:
                self._run_job(job_id)
            except Exception:  # the worker must survive anything a job throws
                log.exception("worker crashed on job %s", job_id)
            finally:
                self._queue.task_done()

    def _run_job(self, job_id: str) -> None:
        job = self.repo.get(job_id)
        with self._lock:
            if job_id in self._waiting:
                self._waiting.remove(job_id)
            self._active_job_id = job_id
        try:
            if job is None:
                log.warning("job %s disappeared before execution", job_id)
                return
            with self._lock:
                cancel_event = self._cancel_events.get(job_id) or threading.Event()
                self._cancel_events[job_id] = cancel_event

            if job.kind == "analysis":
                run_analysis_job(
                    job,
                    settings=self.settings,
                    repo=self.repo,
                    bus=self.bus,
                    client=self._llm_client(),
                    is_cancelled=cancel_event.is_set,
                )
                return

            request = job.meta.get("request") or {}
            engine = self._engine_for(request.get("engine") or self.engine_name)
            language = request.get("language") or self.settings.language_or_none()
            options = TranscribeOptions(language=language)

            runner = run_reprocess_job if job.kind == "reprocess" else run_file_job
            runner(
                job,
                settings=self.settings,
                repo=self.repo,
                bus=self.bus,
                engine=engine,
                options=options,
                is_cancelled=cancel_event.is_set,
            )
        finally:
            with self._lock:
                self._cancel_events.pop(job_id, None)
                self._active_job_id = None

    def _engine_for(self, name: str) -> AsrEngine:
        """Return (and cache) the engine instance for the given name."""
        engine = self._engines.get(name)
        if engine is None:
            engine = (
                self._engine_factory(name, self.settings)
                if self._engine_factory
                else get_asr(name, self.settings)
            )
            self._engines[name] = engine
        return engine

    def _llm_client(self) -> LlmClient:
        """Return (and cache) the LLM client built from settings."""
        if self._llm is None:
            self._llm = LlmClient(
                self.settings.llm_base_url,
                self.settings.llm_model,
                api_key=self.settings.llm_api_key,
                timeout=self.settings.llm_timeout_sec,
                max_tokens=self.settings.llm_max_tokens,
            )
        return self._llm


def build_default_service(settings: Settings | None = None) -> TranscriptionService:
    """Wire the default service: whisper engine, SQLite repository, event bus."""
    settings = settings or get_settings()
    settings.ensure_dirs()
    return TranscriptionService(settings, JobRepository(settings.db_path), EventBus())
