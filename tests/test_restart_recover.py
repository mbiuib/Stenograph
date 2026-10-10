"""Restart recovery: jobs orphaned by a killed server come back at startup.

The queue lives in memory, so a hard stop leaves rows stuck in queued/running
in the repository. With restart_recover (default) waiting jobs return to the
queue as-is and interrupted ones restart from scratch in the SAME job —
progress resets, no duplicate history. Recordings (live/jitsi) cannot resume:
they are only marked interrupted, keeping the transcript recorded so far.
With the flag off every orphan is merely marked, so retry/improve unblock.
"""

from __future__ import annotations

import time
from pathlib import Path

from fastapi.testclient import TestClient

from fakes import FakeEngine
from stenograph.api.app import create_app
from stenograph.config import Settings
from stenograph.domain.models import Job, JobStatus, Segment
from stenograph.events import EventBus
from stenograph.service import TranscriptionService
from stenograph.storage import JobRepository

INTERRUPTED_MESSAGE = "Прервано остановкой сервера"


def _make_service(tmp_path: Path, *, recover: bool = True):
    settings = Settings(data_dir=tmp_path / "data")
    settings.ensure_dirs()
    settings.restart_recover = recover
    repo = JobRepository(settings.db_path)
    engine = FakeEngine()
    service = TranscriptionService(settings, repo, EventBus(), engine_factory=lambda n, s: engine)
    return service, repo, engine


def _seed_file_job(
    tmp_path: Path, name: str = "clip.wav", *, status: JobStatus = JobStatus.QUEUED,
    progress: int = 0,
) -> Job:
    source = tmp_path / name
    source.write_bytes(b"\x00" * 32)
    job = Job(kind="file", source_name=name, source_path=str(source), status=status)
    job.progress = progress
    if status == JobStatus.RUNNING:
        job.started_at = time.time() - 60
    job.meta["request"] = {"language": "ru", "engine": None}
    return job


def _wait_finished(repo: JobRepository, job_id: str, timeout: float = 10.0) -> Job:
    deadline = time.monotonic() + timeout
    job = repo.get(job_id)
    assert job is not None
    while time.monotonic() < deadline and job.status in (JobStatus.QUEUED, JobStatus.RUNNING):
        time.sleep(0.05)
        job = repo.get(job_id)
        assert job is not None
    return job


def test_recover_puts_waiting_jobs_back(tmp_path: Path) -> None:
    """A job that only waited in the queue returns to it as the SAME job."""
    service, repo, _ = _make_service(tmp_path)
    job = _seed_file_job(tmp_path)
    repo.save(job)
    view = service.queue_view()
    assert view["active"] is None and view["active_jobs"] == [] and view["waiting"] == []

    stats = service.recover_after_restart()

    assert stats == {"requeued": 1, "restarted": 0, "marked": 0}
    finished = _wait_finished(repo, job.id)
    assert finished.status == JobStatus.DONE, finished.error
    assert finished.text == "раз\nдва"
    # Same job recycled — nothing new in the history.
    assert len(repo.list_jobs()) == 1


def test_recover_restarts_interrupted_job_in_place(tmp_path: Path) -> None:
    """A job interrupted mid-run restarts from scratch in the SAME job row."""
    service, repo, _ = _make_service(tmp_path)
    job = _seed_file_job(tmp_path, status=JobStatus.RUNNING, progress=46)
    repo.save(job)

    stats = service.recover_after_restart()

    assert stats == {"requeued": 0, "restarted": 1, "marked": 0}
    finished = _wait_finished(repo, job.id)
    assert finished.status == JobStatus.DONE, finished.error
    assert finished.progress == 100
    assert finished.text == "раз\nдва"
    # The very same job id was revived: no retry child, no duplicates.
    assert [item.id for item in repo.list_jobs()] == [job.id]


def test_recover_marks_interrupted_recording(tmp_path: Path) -> None:
    """A live/jitsi recording cannot resume: it is marked, transcript kept."""
    service, repo, _ = _make_service(tmp_path)
    job = Job(kind="live", source_name="Live — тест", status=JobStatus.RUNNING)
    job.segments = [Segment(index=0, start=0.0, end=1.0, text="привет")]
    job.meta["audio"] = {"system": str(tmp_path / "sys.wav")}
    repo.save(job)

    stats = service.recover_after_restart()

    assert stats == {"requeued": 0, "restarted": 0, "marked": 1}
    stored = repo.get(job.id)
    assert stored is not None
    assert stored.status == JobStatus.ERROR
    assert stored.message == INTERRUPTED_MESSAGE
    assert [segment.text for segment in stored.segments] == ["привет"]
    view = service.queue_view()
    assert view["active"] is None and view["active_jobs"] == [] and view["waiting"] == []


def test_recover_disabled_only_marks(tmp_path: Path) -> None:
    """With restart_recover=false orphans are only marked — nothing runs."""
    service, repo, engine = _make_service(tmp_path, recover=False)
    queued = _seed_file_job(tmp_path, "one.wav")
    running = _seed_file_job(tmp_path, "two.wav", status=JobStatus.RUNNING, progress=30)
    repo.save(queued)
    repo.save(running)

    stats = service.recover_after_restart()

    assert stats == {"requeued": 0, "restarted": 0, "marked": 2}
    for job in (queued, running):
        stored = repo.get(job.id)
        assert stored is not None
        assert stored.status == JobStatus.ERROR
        assert stored.message == INTERRUPTED_MESSAGE
    time.sleep(0.3)
    assert engine.calls == []  # nothing was put on the queue
    view = service.queue_view()
    assert view["active"] is None and view["active_jobs"] == [] and view["waiting"] == []


def test_app_startup_recovers_the_queue(tmp_path: Path) -> None:
    """create_app itself revives orphans — the production boot path."""
    settings = Settings(data_dir=tmp_path / "data")
    settings.ensure_dirs()
    repo = JobRepository(settings.db_path)
    service = TranscriptionService(
        settings, repo, EventBus(), engine_factory=lambda n, s: FakeEngine()
    )
    job = _seed_file_job(tmp_path)
    repo.save(job)

    TestClient(create_app(settings=settings, service=service))

    finished = _wait_finished(repo, job.id)
    assert finished.status == JobStatus.DONE, finished.error
