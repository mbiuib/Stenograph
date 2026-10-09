"""API tests with an injected fake engine (no GPU, no ffmpeg)."""

import asyncio
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
    """The health endpoint responds without a GPU and reports loop lag."""
    client = TestClient(create_app(service=_make_service(tmp_path)))
    response = client.get("/api/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["loop_lag_ms"] >= 0
    assert body["loop_lag_max_ms"] >= 0


def test_upload_runs_off_the_event_loop(tmp_path: Path) -> None:
    """File I/O and the probe must not run on the event loop.

    A large upload with ffprobe on the shared loop would stall every request;
    the save+submit block runs in a worker thread instead.
    """
    service = _make_service(tmp_path)
    seen: dict = {}
    original = service.submit_file

    def spy(*args, **kwargs):
        try:
            asyncio.get_running_loop()
            seen["on_loop"] = True
        except RuntimeError:
            seen["on_loop"] = False
        return original(*args, **kwargs)

    service.submit_file = spy
    client = TestClient(create_app(settings=service.settings, service=service))
    response = client.post("/api/jobs", files={"file": ("clip.wav", b"data", "audio/wav")})
    assert response.status_code == 201
    assert seen == {"on_loop": False}, seen


def test_dashboard_endpoints(tmp_path: Path) -> None:
    """Stats, queue, engines and config endpoints return the expected shapes."""
    service = _make_service(tmp_path)
    client = TestClient(create_app(settings=service.settings, service=service))

    engines = client.get("/api/engines").json()
    assert "whisper" in engines["available"] and "moss" in engines["available"]
    assert engines["default"] == service.engine_name
    assert engines["improve_default"] == service.engine_name  # no reprocess_engine set

    config = client.get("/api/config").json()
    assert config["engine"] == service.engine_name
    assert config["reprocess_engine"] is None

    response = client.post("/api/jobs", files={"file": ("clip.wav", b"data", "audio/wav")})
    assert response.status_code == 201
    job_id = response.json()["id"]

    deadline = monotonic() + 5
    while monotonic() < deadline:
        if client.get(f"/api/jobs/{job_id}").json()["status"] == "done":
            break
        sleep(0.05)

    stats = client.get("/api/stats").json()
    assert stats["jobs"]["total"] >= 1
    assert stats["jobs"]["by_status"].get("done", 0) >= 1
    assert stats["audio_seconds"] >= 2.0  # the fake engine reports 2.0 seconds
    assert stats["recent"]

    queue = client.get("/api/queue").json()
    assert "active" in queue and "waiting" in queue


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
