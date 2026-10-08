"""Analysis API tests: protocol/summary child jobs, linking, validation."""

from __future__ import annotations

import time
from pathlib import Path

from fastapi.testclient import TestClient

from fakes import FakeEngine, FakeLlm
from stenograph.api.app import create_app
from stenograph.config import Settings
from stenograph.domain.models import Job, JobStatus, Segment
from stenograph.events import EventBus
from stenograph.service import TranscriptionService
from stenograph.storage import JobRepository


def _make_stack(tmp_path: Path, *, delay: float = 0.0):
    settings = Settings(data_dir=tmp_path)
    settings.ensure_dirs()
    repo = JobRepository(settings.db_path)
    bus = EventBus()
    llm = FakeLlm(delay=delay)
    service = TranscriptionService(
        settings, repo, bus, engine_factory=lambda n, s: FakeEngine(), llm_client=llm
    )
    client = TestClient(create_app(settings=settings, service=service))
    return client, repo, llm


def _seed_done_job(repo: JobRepository, **overrides) -> Job:
    """Persist a finished job directly, bypassing the queue."""
    fields: dict = {
        "kind": "file",
        "source_name": "встреча.mp4",
        "status": JobStatus.DONE,
        "text": "привет мир",
        "segments": [Segment(index=0, start=0.0, end=1.0, text="привет мир")],
    }
    fields.update(overrides)
    job = Job(**fields)
    repo.save(job)
    return job


def _wait_done(repo: JobRepository, job_id: str, timeout: float = 10.0) -> Job:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = repo.get(job_id)
        if job and job.status in (JobStatus.DONE, JobStatus.ERROR, JobStatus.CANCELLED):
            return job
        time.sleep(0.05)
    raise AssertionError("задача не завершилась за отведённое время")


def test_analyze_creates_child_and_links_parent(tmp_path: Path) -> None:
    """POST /analyze runs a child job and copies the result onto the parent."""
    client, repo, llm = _make_stack(tmp_path)
    parent = _seed_done_job(repo)

    response = client.post(f"/api/jobs/{parent.id}/analyze", data={"type": "protocol"})
    assert response.status_code == 201
    child_payload = response.json()
    assert child_payload["kind"] == "analysis"

    child = _wait_done(repo, child_payload["id"])
    assert child.status == JobStatus.DONE, child.error
    assert child.text.startswith("ответ #")
    assert child.meta["parent"] == parent.id
    assert child.meta["analysis_type"] == "protocol"
    assert llm.calls, "LLM не вызывалась"

    refreshed = repo.get(parent.id)
    entry = refreshed.meta["analysis"]["protocol"]
    assert entry["job_id"] == child.id
    assert entry["text"] == child.text
    assert entry["model"] == "fake-llm"
    assert refreshed.status == JobStatus.DONE  # the parent is untouched


def test_analyze_idempotent_while_running(tmp_path: Path) -> None:
    """A second request while the child runs returns the same job.

    After the child finishes, a new request starts a fresh one.
    """
    client, repo, llm = _make_stack(tmp_path, delay=0.3)
    parent = _seed_done_job(repo)

    first = client.post(f"/api/jobs/{parent.id}/analyze", data={"type": "summary"}).json()
    second = client.post(f"/api/jobs/{parent.id}/analyze", data={"type": "summary"}).json()
    assert first["id"] == second["id"]
    _wait_done(repo, first["id"])

    third = client.post(f"/api/jobs/{parent.id}/analyze", data={"type": "summary"}).json()
    assert third["id"] != first["id"]
    _wait_done(repo, third["id"])


def test_analyze_validation(tmp_path: Path) -> None:
    """Unknown jobs, unfinished jobs, empty transcripts and nested analyses.

    All of those are rejected with 400/404.
    """
    client, repo, llm = _make_stack(tmp_path)
    parent = _seed_done_job(repo)

    assert client.post("/api/jobs/нет-такой/analyze", data={"type": "protocol"}).status_code == 404
    assert (
        client.post(f"/api/jobs/{parent.id}/analyze", data={"type": "всё"}).status_code == 400
    )

    running = _seed_done_job(repo, status=JobStatus.RUNNING, source_name="идёт.mp4")
    assert (
        client.post(f"/api/jobs/{running.id}/analyze", data={"type": "protocol"}).status_code
        == 400
    )

    empty = _seed_done_job(repo, text="", segments=[], source_name="пусто.mp4")
    assert (
        client.post(f"/api/jobs/{empty.id}/analyze", data={"type": "protocol"}).status_code
        == 400
    )

    child = client.post(f"/api/jobs/{parent.id}/analyze", data={"type": "protocol"}).json()
    _wait_done(repo, child["id"])
    nested = client.post(f"/api/jobs/{child['id']}/analyze", data={"type": "protocol"})
    assert nested.status_code == 400
