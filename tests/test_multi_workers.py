"""Parallel file workers: MEETSCRIBE_FILE_WORKERS > 1 runs jobs concurrently.

Each worker owns its engine instances, picks jobs under a lock (no double
runs) and holds heavy ASR back while a live/bridge stream is decoding.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

from fakes import FakeEngine
from stenograph.config import Settings
from stenograph.domain.errors import JobCancelled
from stenograph.domain.models import Job, JobStatus, Segment
from stenograph.engines.base import AsrResult, TranscribeOptions
from stenograph.events import EventBus
from stenograph.service import TranscriptionService
from stenograph.storage import JobRepository


class GatedEngine:
    """Blocking engine stand-in: records how many transcribes overlap."""

    name = "fake"

    def __init__(self, state: dict) -> None:
        self._state = state

    def transcribe(
        self,
        audio_path: Path,
        options: TranscribeOptions,
        *,
        on_progress=None,
        on_segment=None,
        is_cancelled=None,
        pause_gate=None,
    ) -> AsrResult:
        """Run until the test releases the gate; honour cancellation meanwhile."""
        self._state["runs"].append(str(audio_path))
        self._state["live"] += 1
        self._state["max_live"] = max(self._state["max_live"], self._state["live"])
        try:
            deadline = time.monotonic() + 15.0
            while not self._state["release"].is_set() and time.monotonic() < deadline:
                if is_cancelled and is_cancelled():
                    raise JobCancelled()
                time.sleep(0.02)
            if is_cancelled and is_cancelled():
                raise JobCancelled()
            return AsrResult(
                language="ru",
                language_probability=0.9,
                duration=1.0,
                segments=[Segment(index=0, start=0.0, end=1.0, text="ок")],
            )
        finally:
            self._state["live"] -= 1


def _state() -> dict:
    """Fresh shared state for GatedEngine doubles."""
    return {"runs": [], "live": 0, "max_live": 0, "release": threading.Event()}


def _job(repo: JobRepository, job_id: str) -> Job:
    """Fetch a job, asserting it exists (test shorthand)."""
    job = repo.get(job_id)
    assert job is not None
    return job


def _wait(condition, timeout: float = 8.0, pause: float = 0.02) -> bool:
    """Poll ``condition`` until true (bounded); returns its last value."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(pause)
    return condition()


def _make_service(tmp_path: Path, workers: int, factory):
    """Service with the given worker count and engine factory."""
    settings = Settings(data_dir=tmp_path / "data")
    settings.ensure_dirs()
    settings.file_workers = workers
    repo = JobRepository(settings.db_path)
    service = TranscriptionService(settings, repo, EventBus(), engine_factory=factory)
    return service, repo


def _wav(tmp_path: Path, name: str) -> Path:
    """A tiny stand-in file the fake engines never actually decode."""
    path = tmp_path / name
    path.write_bytes(b"\x00" * 32)
    return path


def test_two_workers_run_jobs_in_parallel(tmp_path: Path) -> None:
    """With two workers both jobs are RUNNING at the same time."""
    state = _state()
    service, repo = _make_service(tmp_path, 2, lambda n, s: GatedEngine(state))
    first = _wav(tmp_path, "a.wav")
    second = _wav(tmp_path, "b.wav")
    job_a = service.submit_file(first)
    job_b = service.submit_file(second)

    assert _wait(lambda: state["max_live"] >= 2), state
    view = service.queue_view()
    assert len(view["active_jobs"]) == 2
    assert view["active"] is not None and view["active"].id in (job_a.id, job_b.id)
    assert _job(repo, job_a.id).status == JobStatus.RUNNING
    assert _job(repo, job_b.id).status == JobStatus.RUNNING

    state["release"].set()
    assert _wait(
        lambda: _job(repo, job_a.id).status == JobStatus.DONE
        and _job(repo, job_b.id).status == JobStatus.DONE,
        timeout=10,
    )
    # Each job ran exactly once — no double picks.
    assert sorted(state["runs"]) == sorted([str(first), str(second)])


def test_single_worker_stays_serial(tmp_path: Path) -> None:
    """The default worker count keeps the old strictly serial behaviour."""
    state = _state()
    service, repo = _make_service(tmp_path, 1, lambda n, s: GatedEngine(state))
    job_a = service.submit_file(_wav(tmp_path, "a.wav"))
    job_b = service.submit_file(_wav(tmp_path, "b.wav"))

    assert _wait(lambda: state["max_live"] >= 1), state
    time.sleep(0.4)
    assert state["max_live"] == 1
    assert _job(repo, job_a.id).status == JobStatus.RUNNING
    assert _job(repo, job_b.id).status == JobStatus.QUEUED

    state["release"].set()
    assert _wait(
        lambda: _job(repo, job_a.id).status == JobStatus.DONE
        and _job(repo, job_b.id).status == JobStatus.DONE,
        timeout=10,
    )


def test_parallel_workers_never_run_a_job_twice(tmp_path: Path) -> None:
    """Ten quick jobs through two workers: every job runs exactly once."""
    engines: list[FakeEngine] = []

    def factory(name: str, settings: Settings) -> FakeEngine:
        engine = FakeEngine()
        engines.append(engine)
        return engine

    service, repo = _make_service(tmp_path, 2, factory)
    jobs = [service.submit_file(_wav(tmp_path, f"file-{i}.wav")) for i in range(10)]

    assert _wait(
        lambda: all(_job(repo, job.id).status == JobStatus.DONE for job in jobs),
        timeout=15,
    )
    assert sum(len(engine.calls) for engine in engines) == 10


def test_cancel_one_of_two_running_jobs(tmp_path: Path) -> None:
    """Cancelling one parallel job leaves the other one running."""
    state = _state()
    service, repo = _make_service(tmp_path, 2, lambda n, s: GatedEngine(state))
    job_a = service.submit_file(_wav(tmp_path, "a.wav"))
    job_b = service.submit_file(_wav(tmp_path, "b.wav"))
    assert _wait(lambda: state["max_live"] >= 2), state

    assert service.cancel(job_b.id) is True
    assert _wait(lambda: _job(repo, job_b.id).status == JobStatus.CANCELLED, timeout=5)
    assert _job(repo, job_a.id).status == JobStatus.RUNNING

    state["release"].set()
    assert _wait(lambda: _job(repo, job_a.id).status == JobStatus.DONE, timeout=10)


def test_realtime_gate_holds_every_worker(tmp_path: Path) -> None:
    """While a live/bridge stream decodes, no worker starts a heavy job."""
    busy = {"value": True}
    service, repo = _make_service(tmp_path, 2, lambda n, s: FakeEngine())
    service.realtime_provider = lambda: busy["value"]
    job_a = service.submit_file(_wav(tmp_path, "a.wav"))
    job_b = service.submit_file(_wav(tmp_path, "b.wav"))

    time.sleep(1.6)  # the gate re-checks once a second
    assert _job(repo, job_a.id).status == JobStatus.QUEUED
    assert _job(repo, job_b.id).status == JobStatus.QUEUED
    assert "живая запись" in _job(repo, job_a.id).message
    # The gate must not consume jobs: the whole pool stays visible, so as
    # soon as the air frees BOTH workers can pick a job in parallel.
    seen_waiting: set[int] = set()
    deadline = time.monotonic() + 1.6
    while time.monotonic() < deadline:
        seen_waiting.add(len(service.queue_view()["waiting"]))
        time.sleep(0.05)
    assert seen_waiting == {2}, seen_waiting

    busy["value"] = False
    assert _wait(
        lambda: _job(repo, job_a.id).status == JobStatus.DONE
        and _job(repo, job_b.id).status == JobStatus.DONE,
        timeout=10,
    )


def test_workers_setting_is_clamped(tmp_path: Path) -> None:
    """Absurd worker counts clamp to 1..8 instead of breaking the service."""
    settings = Settings(data_dir=tmp_path / "low")
    settings.ensure_dirs()
    settings.file_workers = 0
    repo = JobRepository(settings.db_path)
    service = TranscriptionService(settings, repo, EventBus())
    assert service.workers == 1

    settings = Settings(data_dir=tmp_path / "high")
    settings.ensure_dirs()
    settings.file_workers = 99
    repo = JobRepository(settings.db_path)
    service = TranscriptionService(settings, repo, EventBus())
    assert service.workers == 8
