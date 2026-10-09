"""Realtime priority: heavy ASR jobs yield to live/bridge streams."""

from __future__ import annotations

import threading
import time
from pathlib import Path

from fakes import FakeEngine
from stenograph.config import Settings
from stenograph.domain.models import JobStatus
from stenograph.events import EventBus
from stenograph.service import TranscriptionService
from stenograph.storage import JobRepository


def _make_service(tmp_path: Path):
    settings = Settings(data_dir=tmp_path / "data")
    settings.ensure_dirs()
    engine = FakeEngine()
    service = TranscriptionService(
        settings,
        JobRepository(settings.db_path),
        EventBus(),
        engine_factory=lambda name, prepared: engine,
    )
    return service, service.repo, engine


def _submit_file(service: TranscriptionService, tmp_path: Path):
    source = tmp_path / "clip.wav"
    source.write_bytes(b"\x00" * 32)
    return service.submit_file(source, language="ru")


def _wait_status(
    repo: JobRepository, job_id: str, wanted: set[JobStatus], timeout: float = 10.0
):
    deadline = time.monotonic() + timeout
    job = repo.get(job_id)
    assert job is not None
    while time.monotonic() < deadline and job.status not in wanted:
        time.sleep(0.05)
        job = repo.get(job_id)
        assert job is not None
    return job


def test_heavy_job_waits_while_realtime_is_active(tmp_path: Path) -> None:
    """A file job stays queued while live/bridge is active, then runs."""
    service, repo, _ = _make_service(tmp_path)
    busy = threading.Event()
    busy.set()
    service.realtime_provider = busy.is_set

    job = _submit_file(service, tmp_path)
    time.sleep(0.5)
    pending = repo.get(job.id)
    assert pending is not None and pending.status == JobStatus.QUEUED
    assert pending.message == "Ждёт: идёт живая запись"

    busy.clear()
    finished = _wait_status(repo, job.id, {JobStatus.DONE, JobStatus.ERROR})
    assert finished.status == JobStatus.DONE, finished.error


def test_file_job_runs_when_realtime_is_idle(tmp_path: Path) -> None:
    """With an idle provider the very same job runs without delay."""
    service, repo, _ = _make_service(tmp_path)
    service.realtime_provider = lambda: False

    job = _submit_file(service, tmp_path)
    finished = _wait_status(repo, job.id, {JobStatus.DONE, JobStatus.ERROR})
    assert finished.status == JobStatus.DONE, finished.error


def test_pause_gate_blocks_until_realtime_stops(tmp_path: Path) -> None:
    """The gate handed to engines sleeps while busy and returns once idle."""
    service, _, _ = _make_service(tmp_path)
    busy = threading.Event()
    busy.set()
    service.realtime_provider = busy.is_set

    gate = service._pause_gate(threading.Event().is_set)
    assert gate is not None, "a provider must produce a gate"
    returned = threading.Event()

    def run_gate() -> None:
        gate()
        returned.set()

    thread = threading.Thread(target=run_gate, daemon=True)
    thread.start()
    assert not returned.wait(0.4), "the gate must hold while the air is busy"

    busy.clear()
    assert returned.wait(5.0), "the gate must release once the air is idle"


def test_pause_gate_is_none_without_provider(tmp_path: Path) -> None:
    """Tests/CLI without a provider keep the old ungated behaviour."""
    service, _, _ = _make_service(tmp_path)
    assert service._pause_gate(threading.Event().is_set) is None


def test_pause_gate_reaches_the_engine(tmp_path: Path) -> None:
    """The worker hands the gate to engine.transcribe (wiring check)."""
    service, repo, engine = _make_service(tmp_path)
    service.realtime_provider = lambda: False

    job = _submit_file(service, tmp_path)
    _wait_status(repo, job.id, {JobStatus.DONE, JobStatus.ERROR})
    assert engine.last_pause_gate is not None
