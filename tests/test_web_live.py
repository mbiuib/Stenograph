"""Browser live capture: WebSocket upload feeds the same live pipeline (no GPU)."""

from __future__ import annotations

from pathlib import Path
from time import monotonic, sleep

import numpy as np
from fastapi.testclient import TestClient

from fakes import FakeEngine, PositionTranscriber, encoded_chunk
from stenograph.api.app import create_app
from stenograph.config import Settings
from stenograph.events import EventBus
from stenograph.live.manager import LiveManager
from stenograph.service import TranscriptionService
from stenograph.storage import JobRepository

TRACK_MIC = 0
TRACK_SYSTEM = 1


def _frame(track: int, index: int) -> bytes:
    """One upload frame: track byte + int16 LE position-encoded chunk."""
    pcm = (np.clip(encoded_chunk(index), -1.0, 1.0) * 32767.0).astype("<i2")
    return bytes([track]) + pcm.tobytes()


def _make_stack(tmp_path: Path, *, reprocess=None, auto_reprocess: bool = False):
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
        capture_factory=None,
        reprocess=reprocess,
        auto_reprocess=auto_reprocess,
    )
    return service, live, bus


def _stream(ws, track: int, start_index: int, count: int, pause: float = 0.2) -> None:
    """Send position-encoded frames with pauses so the decode pump can tick."""
    for step in range(count):
        ws.send_bytes(_frame(track, start_index + step))
        sleep(pause)


def _wait_done(client: TestClient, job_id: str, timeout: float = 15.0) -> dict:
    deadline = monotonic() + timeout
    job: dict = {}
    while monotonic() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in ("done", "error"):
            return job
        sleep(0.2)
    return job


def test_web_live_upload_end_to_end(tmp_path: Path) -> None:
    """WS handshake → binary frames per track → stop → finalized done job."""
    service, live, bus = _make_stack(tmp_path)
    client = TestClient(create_app(settings=service.settings, service=service, live=live))

    with client.websocket_connect("/ws/live") as ws:
        ws.send_json({"type": "start", "tracks": ["system", "mic"]})
        ready = ws.receive_json()
        assert ready["type"] == "ready"
        assert ready["sample_rate"] == 16000
        job_id = ready["job_id"]
        channel = bus.subscribe(job_id)

        _stream(ws, TRACK_SYSTEM, 0, 6)
        _stream(ws, TRACK_MIC, 20, 4)
        ws.send_json({"type": "stop"})

    job = _wait_done(client, job_id)
    assert job["status"] == "done", job
    assert job["segments"], job
    speakers = {segment["speaker"] for segment in job["segments"]}
    assert speakers <= {"Вы", "Они"}, speakers
    assert any(
        segment["speaker"] == "Они" and "фраза" in segment["text"]
        for segment in job["segments"]
    ), job["segments"]
    assert any(segment["speaker"] == "Вы" for segment in job["segments"]), job["segments"]

    audio_dir = tmp_path / "live" / job_id
    assert (audio_dir / "system.wav").stat().st_size > 44
    assert (audio_dir / "mic.wav").stat().st_size > 44

    # partials/levels streamed over the bus while the upload was running
    types: set[str] = set()
    while True:
        try:
            types.add(str(channel.get_nowait().get("type") or ""))
        except Exception:  # noqa: BLE001 — queue drained
            break
    assert "partial" in types or "segment" in types, types

    assert client.get("/api/live/status").json()["active"] is False


def test_web_live_second_session_rejected(tmp_path: Path) -> None:
    """One live session at a time: a second WS gets an error, REST gets 409."""
    service, live, _ = _make_stack(tmp_path)
    client = TestClient(create_app(settings=service.settings, service=service, live=live))

    with client.websocket_connect("/ws/live") as first:
        first.send_json({"type": "start", "tracks": ["mic"]})
        assert first.receive_json()["type"] == "ready"

        with client.websocket_connect("/ws/live") as second:
            second.send_json({"type": "start", "tracks": ["mic"]})
            error = second.receive_json()
            assert error["type"] == "error"

        assert client.post("/api/live/start", data={"tracks": "mic"}).status_code == 409

        first.send_json({"type": "stop"})

    assert client.get("/api/live/status").json()["active"] is False


def test_web_live_requires_start_first(tmp_path: Path) -> None:
    """A bad handshake is answered with an error message, not a crash."""
    service, live, _ = _make_stack(tmp_path)
    client = TestClient(create_app(settings=service.settings, service=service, live=live))

    with client.websocket_connect("/ws/live") as ws:
        ws.send_json({"type": "nonsense"})
        error = ws.receive_json()
        assert error["type"] == "error"


def test_web_live_auto_reprocess_chains(tmp_path: Path) -> None:
    """Stopping a browser session hands the recording to the quality re-pass."""
    handed_over: list[str] = []

    def reprocess(job):
        handed_over.append(job.id)
        return None

    service, live, _ = _make_stack(tmp_path, reprocess=reprocess, auto_reprocess=True)
    client = TestClient(create_app(settings=service.settings, service=service, live=live))

    with client.websocket_connect("/ws/live") as ws:
        ws.send_json({"type": "start", "tracks": ["system"]})
        job_id = ws.receive_json()["job_id"]
        _stream(ws, TRACK_SYSTEM, 0, 4)
        ws.send_json({"type": "stop"})

    assert handed_over == [job_id]
    job = _wait_done(client, job_id)
    assert job["status"] == "done", job
