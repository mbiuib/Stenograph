"""PATCH /api/jobs/{id}: renaming jobs and mapping speaker labels to names."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from fakes import FakeEngine
from stenograph.api.app import create_app
from stenograph.config import Settings
from stenograph.domain.models import Job
from stenograph.events import EventBus
from stenograph.service import TranscriptionService
from stenograph.storage import JobRepository


def _make_client(tmp_path: Path) -> tuple[TestClient, JobRepository]:
    settings = Settings(data_dir=tmp_path / "data")
    settings.ensure_dirs()
    repo = JobRepository(settings.db_path)
    service = TranscriptionService(
        settings, repo, EventBus(), engine_factory=lambda n, s: FakeEngine()
    )
    return TestClient(create_app(settings=settings, service=service)), repo


def test_api_renames_a_job(tmp_path: Path) -> None:
    """A rename is trimmed, persisted and returned in the payload."""
    client, repo = _make_client(tmp_path)
    job = Job(kind="file", source_name="old name.wav")
    repo.save(job)

    response = client.patch(f"/api/jobs/{job.id}", json={"source_name": "  Планёрка команды  "})
    assert response.status_code == 200
    assert response.json()["source_name"] == "Планёрка команды"

    stored = repo.get(job.id)
    assert stored is not None and stored.source_name == "Планёрка команды"


def test_api_maps_speaker_names(tmp_path: Path) -> None:
    """Speaker labels can be mapped to human names and cleared again."""
    client, repo = _make_client(tmp_path)
    job = Job(kind="file", source_name="meeting.wav")
    repo.save(job)

    response = client.patch(f"/api/jobs/{job.id}", json={"speaker_names": {"SPEAKER_00": "Иван"}})
    assert response.status_code == 200
    assert response.json()["meta"]["speaker_names"] == {"SPEAKER_00": "Иван"}

    cleared = client.patch(f"/api/jobs/{job.id}", json={"speaker_names": {}})
    assert cleared.status_code == 200
    assert "speaker_names" not in cleared.json()["meta"]


def test_api_update_rejects_bad_input(tmp_path: Path) -> None:
    """Missing jobs, blank names and overlong names are refused without side effects."""
    client, repo = _make_client(tmp_path)
    job = Job(kind="file", source_name="meeting.wav")
    repo.save(job)

    assert client.patch("/api/jobs/missing", json={"source_name": "x"}).status_code == 404
    assert client.patch(f"/api/jobs/{job.id}", json={"source_name": "   "}).status_code == 400
    assert client.patch(f"/api/jobs/{job.id}", json={"source_name": "x" * 201}).status_code == 400

    stored = repo.get(job.id)
    assert stored is not None and stored.source_name == "meeting.wav"
