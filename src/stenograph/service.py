"""Application service: job intake, a single-worker queue, cancellation.

All GPU work happens on one worker thread (Whisper is not GPU-concurrency
friendly); the queue is FIFO. Live sessions will get priority over files in a
later milestone.
"""

from __future__ import annotations

import logging
import queue
import threading
from collections.abc import Callable
from pathlib import Path

from .config import Settings, get_settings
from .domain.models import Job, JobStatus
from .engines import get_asr
from .engines.base import AsrEngine, TranscribeOptions
from .events import EventBus
from .pipeline import run_file_job
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
    ) -> None:
        self.settings = settings
        self.repo = repo
        self.bus = bus
        self.engine_name = engine_name or settings.engine
        self._engine_factory = engine_factory
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

            request = job.meta.get("request") or {}
            engine = self._engine_for(request.get("engine") or self.engine_name)
            language = request.get("language") or self.settings.language_or_none()
            options = TranscribeOptions(language=language)

            run_file_job(
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


def build_default_service(settings: Settings | None = None) -> TranscriptionService:
    """Wire the default service: whisper engine, SQLite repository, event bus."""
    settings = settings or get_settings()
    settings.ensure_dirs()
    return TranscriptionService(settings, JobRepository(settings.db_path), EventBus())
