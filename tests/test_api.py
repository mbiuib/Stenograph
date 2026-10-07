"""API tests with an injected fake engine (no GPU, no ffmpeg)."""

from pathlib import Path
from time import monotonic, sleep

from fastapi.testclient import TestClient

from fakes import FakeEngine
from stenograph.api.app import create_app
from stenograph.config import Settings
from stenograph.events import EventBus
from stenograph.service import TranscriptionService
from stenograph.storage import JobRepository


def _make_service(tmp_path: Path) -> TranscriptionService:
    settings = Settings(data_dir=tmp_path)
    settings.ensure_dirs()
    service = TranscriptionService(
        settings,
        JobRepository(settings.db_path),
        EventBus(),
        engine_factory=lambda name, settings: FakeEngine(),
    )
    return service


def test_health(tmp_path: Path) -> None:
    """The health endpoint responds without a GPU."""
    client = TestClient(create_app(service=_make_service(tmp_path)))
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_upload_and_complete(tmp_path: Path) -> None:
    """Uploading a file queues it, the worker finishes it, polling returns the text."""
    service = _make_service(tmp_path)
    client = TestClient(create_app(settings=service.settings, service=service))

    response = client.post(
        "/api/jobs", files={"file": ("clip.wav", b"fake audio bytes", "audio/wav")}
    )
    assert response.status_code == 201
    job_id = response.json()["id"]

    job = None
    deadline = monotonic() + 5
    while monotonic() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in ("done", "error"):
            break
        sleep(0.05)

    assert job is not None
    assert job["status"] == "done", job
    assert job["text"] == "раз\nдва"

    listed = client.get("/api/jobs").json()
    assert any(j["id"] == job_id for j in listed)

    assert client.get("/api/jobs/missing").status_code == 404
    assert client.delete(f"/api/jobs/{job_id}").status_code == 204
