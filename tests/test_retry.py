"""Retry tests: re-running an uploaded file job — service layer and API."""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from fakes import FakeEngine
from stenograph.api.app import create_app
from stenograph.config import Settings
from stenograph.domain.models import Job, JobStatus
from stenograph.events import EventBus
from stenograph.service import TranscriptionService
from stenograph.storage import JobRepository


def _make_service(tmp_path: Path):
    settings = Settings(data_dir=tmp_path / "data")
    settings.ensure_dirs()
    repo = JobRepository(settings.db_path)
    engine = FakeEngine()
    service = TranscriptionService(settings, repo, EventBus(), engine_factory=lambda n, s: engine)
    return service, repo, engine


def _failed_file_job(tmp_path: Path, *, language: str | None = "ru") -> Job:
    source = tmp_path / "clip.wav"
    source.write_bytes(b"\x00" * 32)
    job = Job(kind="file", source_name="clip.wav", source_path=str(source), status=JobStatus.ERROR)
    job.error = "boom"
    job.meta["request"] = {"language": language, "engine": None}
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


def test_service_retry_creates_new_job_and_runs(tmp_path: Path) -> None:
    """A failed file job is retried as a fresh job that re-reads the same file."""
    service, repo, _ = _make_service(tmp_path)
    job = _failed_file_job(tmp_path)
    repo.save(job)

    new_job = service.retry_file_job(job)

    assert new_job.id != job.id
    assert new_job.kind == "file"
    assert new_job.source_name == job.source_name
    assert new_job.source_path == job.source_path
    assert new_job.meta["retry_of"] == job.id
    assert new_job.meta["request"] == {"language": "ru", "engine": None}

    finished = _wait_finished(repo, new_job.id)
    assert finished.status == JobStatus.DONE, finished.error
    assert finished.text == "раз\nдва"
    # The failed attempt stays in the history untouched.
    original = repo.get(job.id)
    assert original is not None and original.status == JobStatus.ERROR


def test_service_retry_overrides_options(tmp_path: Path) -> None:
    """Engine and language can be overridden for the retry run."""
    service, repo, _ = _make_service(tmp_path)
    job = _failed_file_job(tmp_path)
    repo.save(job)

    new_job = service.retry_file_job(job, engine="whisper", language="en")

    assert new_job.meta["request"] == {"language": "en", "engine": "whisper"}


def test_service_retry_rejects_busy_live_and_missing(tmp_path: Path) -> None:
    """Retry refuses a busy job, a non-file job and a vanished source file."""
    service, repo, _ = _make_service(tmp_path)

    busy = _failed_file_job(tmp_path)
    busy.status = JobStatus.RUNNING
    repo.save(busy)
    with pytest.raises(ValueError, match="выполняется"):
        service.retry_file_job(busy)

    live = Job(kind="live", status=JobStatus.DONE)
    repo.save(live)
    with pytest.raises(ValueError, match="только файловую"):
        service.retry_file_job(live)

    vanished = _failed_file_job(tmp_path)
    vanished.source_path = str(tmp_path / "gone.wav")
    repo.save(vanished)
    with pytest.raises(ValueError, match="недоступен"):
        service.retry_file_job(vanished)


def test_api_retry_endpoint(tmp_path: Path) -> None:
    """POST /api/jobs/{id}/retry queues the file again (plus 404/400 cases)."""
    service, repo, _ = _make_service(tmp_path)
    client = TestClient(create_app(settings=service.settings, service=service))

    job = _failed_file_job(tmp_path)
    repo.save(job)

    response = client.post(f"/api/jobs/{job.id}/retry")
    assert response.status_code == 201
    payload = response.json()
    assert payload["kind"] == "file"
    assert payload["source_name"] == "clip.wav"
    assert payload["meta"]["retry_of"] == job.id

    assert client.post("/api/jobs/missing/retry").status_code == 404

    live = Job(kind="live", status=JobStatus.DONE)
    repo.save(live)
    assert client.post(f"/api/jobs/{live.id}/retry").status_code == 400

    new_job = _wait_finished(repo, payload["id"])
    assert new_job.status == JobStatus.DONE, new_job.error
    assert len(new_job.segments) == 2
