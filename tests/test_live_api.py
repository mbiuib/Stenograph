"""Live session API tests: fake capture source + scripted transcriber, no GPU."""

from __future__ import annotations

import queue
from pathlib import Path
from time import monotonic

from fastapi.testclient import TestClient

from fakes import FakeCaptureSource, FakeEngine, PositionTranscriber
from stenograph.api.app import create_app
from stenograph.config import Settings
from stenograph.events import EventBus
from stenograph.live.manager import LiveManager
from stenograph.service import TranscriptionService
from stenograph.storage import JobRepository


def _capture_factory(track: str, on_chunk, *, chunk_sec: float = 0.2) -> FakeCaptureSource:
    """System track words start at «фраза0», mic words at «фраза10»."""
    return FakeCaptureSource(
        track, on_chunk, chunk_sec=chunk_sec, start_chunk=0 if track == "system" else 20
    )


def _make_stack(tmp_path: Path, capture_factory=None):
    settings = Settings(data_dir=tmp_path, live_step_sec=0.4, live_max_window_sec=10.0)
    settings.ensure_dirs()
    repo = JobRepository(settings.db_path)
    bus = EventBus()
    service = TranscriptionService(
        settings, repo, bus, engine_factory=lambda name, s: FakeEngine()
    )
    live = LiveManager(
        settings,
        repo,
        bus,
        transcriber_factory=lambda language: PositionTranscriber(),
        capture_factory=capture_factory or _capture_factory,
    )
    return service, live, bus


def _drain(channel: queue.Queue, stop_types: set[str], timeout: float = 10.0) -> list[dict]:
    events: list[dict] = []
    deadline = monotonic() + timeout
    while monotonic() < deadline:
        try:
            event = channel.get(timeout=0.2)
        except queue.Empty:
            continue
        events.append(event)
        if event.get("type") in stop_types:
            break
    return events


def test_live_session_end_to_end(tmp_path: Path) -> None:
    """Start → partials/levels/segments stream → stop → finalized done job."""
    service, live, bus = _make_stack(tmp_path)
    client = TestClient(create_app(settings=service.settings, service=service, live=live))

    assert client.get("/api/live/status").json()["active"] is False

    response = client.post("/api/live/start", data={"tracks": "system,mic"})
    assert response.status_code == 201
    job_id = response.json()["id"]
    channel = bus.subscribe(job_id)

    events = _drain(channel, stop_types={"segment"})
    types = [event.get("type") for event in events]
    assert "partial" in types, types
    assert "level" in types, types
    assert "segment" in types, types

    # a second session cannot start while one is running
    assert client.post("/api/live/start").status_code == 409

    stopped = client.post("/api/live/stop")
    assert stopped.status_code == 200
    job = stopped.json()
    assert job["status"] == "done"
    assert job["segments"], job
    assert any(
        "фраза0" in segment["text"] and segment["speaker"] == "Они"
        for segment in job["segments"]
    ), job["segments"]
    assert any(segment["speaker"] == "Вы" for segment in job["segments"]), job["segments"]
    assert all(segment["speaker"] in ("Вы", "Они") for segment in job["segments"])

    tail = _drain(channel, stop_types={"done", "error"}, timeout=3.0)
    assert any(event.get("type") == "done" for event in tail), [e.get("type") for e in tail]

    audio_dir = tmp_path / "live" / job_id
    assert (audio_dir / "system.wav").is_file()
    assert (audio_dir / "mic.wav").is_file()
    assert (audio_dir / "system.wav").stat().st_size > 44  # header + samples

    assert client.get("/api/live/status").json()["active"] is False
    # the service is reusable after a stop
    again = client.post("/api/live/start")
    assert again.status_code == 201
    assert client.post("/api/live/stop").status_code == 200


def test_live_start_accepts_meeting_title(tmp_path: Path) -> None:
    """A custom title names the session; the default carries a date/time."""
    service, live, _bus = _make_stack(tmp_path)
    client = TestClient(create_app(settings=service.settings, service=service, live=live))

    titled = client.post("/api/live/start", data={"tracks": "system", "title": "Планёрка"})
    assert titled.status_code == 201
    assert titled.json()["source_name"] == "Планёрка"
    assert client.post("/api/live/stop").status_code == 200

    default = client.post("/api/live/start", data={"tracks": "system"})
    assert default.status_code == 201
    assert default.json()["source_name"].startswith("Live (машина) — ")
    assert client.post("/api/live/stop").status_code == 200


def test_live_stop_without_session(tmp_path: Path) -> None:
    """Stopping with no session returns 404."""
    service, live, _ = _make_stack(tmp_path)
    client = TestClient(create_app(settings=service.settings, service=service, live=live))
    assert client.post("/api/live/stop").status_code == 404


def test_live_rejects_unknown_track(tmp_path: Path) -> None:
    """Unknown track names are refused before anything starts."""
    service, live, _ = _make_stack(tmp_path)
    client = TestClient(create_app(settings=service.settings, service=service, live=live))
    response = client.post("/api/live/start", data={"tracks": "bogus"})
    assert response.status_code == 409
    assert client.get("/api/live/status").json()["active"] is False


def test_live_capture_failure_marks_job_error(tmp_path: Path) -> None:
    """A device that cannot open yields a 400 and an errored job."""

    class FailingSource(FakeCaptureSource):
        fail = True

    service, live, _ = _make_stack(tmp_path, capture_factory=FailingSource)
    client = TestClient(create_app(settings=service.settings, service=service, live=live))
    response = client.post("/api/live/start")
    assert response.status_code == 400
    assert client.get("/api/live/status").json()["active"] is False
    jobs = service.list_jobs()
    assert jobs and jobs[0].status == "error"
    assert "захват" in (jobs[0].error or "")


def test_live_auto_reprocess_chains_after_stop(tmp_path: Path) -> None:
    """With auto_reprocess on, stopping a session hands the recording over."""
    settings = Settings(data_dir=tmp_path, live_step_sec=0.4, live_max_window_sec=10.0)
    settings.ensure_dirs()
    repo = JobRepository(settings.db_path)
    bus = EventBus()
    service = TranscriptionService(
        settings, repo, bus, engine_factory=lambda name, s: FakeEngine()
    )
    handed_over: list[str] = []

    def reprocess(job):
        handed_over.append(job.id)
        return None

    live = LiveManager(
        settings,
        repo,
        bus,
        transcriber_factory=lambda language: PositionTranscriber(),
        capture_factory=_capture_factory,
        reprocess=reprocess,
        auto_reprocess=True,
    )
    client = TestClient(create_app(settings=settings, service=service, live=live))

    response = client.post("/api/live/start", data={"tracks": "system"})
    assert response.status_code == 201
    job_id = response.json()["id"]
    stopped = client.post("/api/live/stop")
    assert stopped.status_code == 200
    assert stopped.json()["status"] == "done"
    assert handed_over == [job_id]
