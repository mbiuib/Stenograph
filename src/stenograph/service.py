"""Application service: job intake, a single-worker queue, cancellation.

All GPU work happens on one worker thread (Whisper is not GPU-concurrency
friendly). While the air is live the worker holds heavy ASR jobs off the GPU
(realtime gate); otherwise queued work runs by class — user files first, then
analyses, then manual improvements, auto-chained improvements last.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections.abc import Callable
from pathlib import Path

from .config import Settings, clean_language, get_settings
from .domain.models import Job, JobStatus
from .engines import available_asr, get_asr
from .engines.base import AsrEngine, TranscribeOptions
from .events import EventBus
from .llm.analyzer import transcript_text
from .llm.client import LlmClient
from .llm.prompts import ANALYSIS_TYPES
from .pipeline import run_analysis_job, run_file_job, run_reprocess_job
from .storage import JobRepository

log = logging.getLogger(__name__)

EngineFactory = Callable[[str, Settings], AsrEngine]

# Queue classes: lower runs first. User-facing work outranks background
# quality re-passes, and an auto-chained improvement (created when a live
# session stops) must never delay a freshly uploaded file or a manual action.
PRIORITY_FILE = 10
PRIORITY_ANALYSIS = 15
PRIORITY_REPROCESS = 20
PRIORITY_REPROCESS_AUTO = 30

_WAITING_MESSAGE = "Ждёт: идёт живая запись"


def queue_priority(job: Job) -> int:
    """Execution class of a queued job (lower number runs earlier)."""
    if job.kind == "file":
        return PRIORITY_FILE
    if job.kind == "analysis":
        return PRIORITY_ANALYSIS
    if job.kind == "reprocess":
        return PRIORITY_REPROCESS_AUTO if job.meta.get("auto") else PRIORITY_REPROCESS
    return PRIORITY_REPROCESS


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
        # Set by create_app: does a live/bridge stream run right now? Heavy ASR
        # jobs then hold back until the air is free (realtime prioritisation).
        self.realtime_provider: Callable[[], bool] | None = None
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

    def reprocess_job(
        self,
        recording: Job,
        *,
        engine: str | None = None,
        language: str | None = None,
        auto: bool = False,
    ) -> Job:
        """Queue an offline re-transcription of a live/jitsi recording.

        One child job per session: every recorded track is re-transcribed from
        scratch (the default engine — moss — unless overridden) and merged into
        a single transcript (live: «Вы» / diarized labels; jitsi: per-speaker
        files keep their participant labels). ``language`` overrides the track
        language ("auto"/"" mean auto-detect); None keeps the recording's own
        language, falling back to the settings default. Idempotent while a run
        is queued/running: returns the already existing child job. ``auto``
        marks the child as chained after a session stop — the lowest queue
        class.
        """
        audio, tracks = self._recording_sources(recording)

        existing_id = recording.meta.get("reprocess_job")
        if existing_id:
            existing = self.repo.get(str(existing_id))
            if existing and existing.status in (JobStatus.QUEUED, JobStatus.RUNNING):
                return existing

        job = Job(kind="reprocess", source_name=f"Улучшение записи — {recording.source_name}")
        job.meta["parent"] = recording.id
        job.meta["source_kind"] = recording.kind
        job.meta["tracks"] = tracks
        job.meta["audio"] = {track: audio[track] for track in tracks}
        if recording.meta.get("audio_timeline"):
            # ребёнок играет как запись: единая дорожка, если она возможна
            job.meta["audio_timeline"] = recording.meta["audio_timeline"]
        if language is not None:
            effective = clean_language(language)
        else:
            effective = clean_language(recording.language) or self.settings.language_or_none()
        job.meta["request"] = {
            "engine": engine,
            "language": effective,
        }
        if auto:
            job.meta["auto"] = True  # queue class: awaits the gap, runs last
        self._enqueue(job)

        recording.meta["reprocess_job"] = job.id
        self.repo.save(recording)
        self.bus.publish(recording.id, {"type": "meta", "meta": recording.meta})
        return job

    @staticmethod
    def _recording_sources(job: Job) -> tuple[dict[str, str], list[str]]:
        """Track map + playable tracks of a recording; ValueError when none.

        Live sessions record "system"/"mic" tracks; jitsi meetings record one
        file per participant, keyed by the speaker label («Спикер N»).
        """
        if job.kind not in ("live", "jitsi"):
            raise ValueError("улучшение доступно для записей Live и Jitsi")
        audio = {str(key): str(value) for key, value in (job.meta.get("audio") or {}).items()}
        if job.kind == "live":
            tracks = [
                track
                for track in ("system", "mic")
                if audio.get(track) and Path(audio[track]).is_file()
            ]
        else:
            tracks = [label for label, path in audio.items() if label and Path(path).is_file()]
        if not tracks:
            raise ValueError("у записи нет сохранённых дорожек")
        return audio, tracks

    def chain_reprocess(self, live_job: Job) -> Job:
        """Auto-chained improvement after a session stop (lowest queue class).

        The engine comes from MEETSCRIBE_REPROCESS_ENGINE when set (applies to
        live and jitsi alike); an unknown name falls back to the default.
        """
        engine = self.settings.reprocess_engine
        if engine is not None and engine not in available_asr():
            log.warning("reprocess_engine=%r неизвестен — беру движок по умолчанию", engine)
            engine = None
        return self.reprocess_job(live_job, engine=engine, auto=True)

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
        if language is not None:
            new_language = clean_language(language)
        else:
            new_language = clean_language(request.get("language"))
        new_job.meta["request"] = {
            "language": new_language,
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
        waiting = sorted(
            (job for job in (self.repo.get(item) for item in waiting_ids) if job),
            key=queue_priority,  # display order == execution order
        )
        return {"active": active, "waiting": waiting}

    # -- worker -------------------------------------------------------------

    def _realtime_busy(self) -> bool:
        """True while any live/bridge stream is running (the background yields)."""
        provider = self.realtime_provider
        if provider is None:
            return False
        try:
            return bool(provider())
        except Exception:  # noqa: BLE001 — the gate must never kill the worker
            log.debug("realtime provider failed", exc_info=True)
            return False

    def _wait_for_realtime_gap(self, job_id: str) -> bool:
        """One polling step of the realtime gate for ``job_id``.

        True while a heavy ASR job must yield to an active live/bridge
        stream (the caller returns it to the waiting pool and re-picks a
        second later, so a freshly queued file can take the lead); False
        when the job may run.
        """
        job = self.repo.get(job_id)
        if job is None or job.kind not in ("file", "reprocess"):
            return False
        if not self._realtime_busy():
            return False
        if job.message != _WAITING_MESSAGE:
            log.info("job %s (%s) waits: a live/bridge stream is active", job_id, job.kind)
            job.message = _WAITING_MESSAGE
            self.repo.save(job)
            self.bus.publish(
                job.id,
                {
                    "type": "status",
                    "status": str(job.status),
                    "message": job.message,
                    "progress": job.progress,
                },
            )
        return True

    def _pause_gate(self, is_cancelled: Callable[[], bool]) -> Callable[[], None] | None:
        """Cooperative yield handed to engine chunk loops (None = no gate)."""
        if self.realtime_provider is None:
            return None

        def gate() -> None:
            while self._realtime_busy() and not is_cancelled():
                time.sleep(0.5)

        return gate

    def _work_loop(self) -> None:
        while True:
            self._queue.get()  # wake-up: at least one job is waiting
            try:
                self._serve_waiting()
            except Exception:  # the worker must survive anything a job throws
                log.exception("worker failed while serving the queue")
            finally:
                self._queue.task_done()

    def _serve_waiting(self) -> None:
        """Run the best-class waiting job; heavy ASR yields to the air."""
        while True:
            job_id = self._pick_queued()
            if job_id is None:
                return
            if self._wait_for_realtime_gap(job_id):
                # Held back by the air: return to the pool and re-pick a
                # second later — a newly queued file takes the lead meanwhile.
                with self._lock:
                    self._waiting.insert(0, job_id)
                time.sleep(1.0)
                continue
            self._run_job(job_id)
            return

    def _pick_queued(self) -> str | None:
        """Take the highest-class waiting job (insertion order breaks ties)."""
        with self._lock:
            candidates = list(self._waiting)
        best_id: str | None = None
        best_rank = 0
        for job_id in candidates:
            job = self.repo.get(job_id)
            if job is None or job.status != JobStatus.QUEUED:
                with self._lock:
                    if job_id in self._waiting:
                        self._waiting.remove(job_id)
                continue
            rank = queue_priority(job)
            if best_id is None or rank < best_rank:
                best_id, best_rank = job_id, rank
        if best_id is None:
            return None
        with self._lock:
            if best_id in self._waiting:
                self._waiting.remove(best_id)
        return best_id

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
            language = clean_language(request.get("language")) or self.settings.language_or_none()
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
                pause_gate=self._pause_gate(cancel_event.is_set),
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
