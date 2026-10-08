"""Browser live capture: WebSocket upload feeds the same live pipeline (no GPU)."""

from __future__ import annotations

import threading
from pathlib import Path
from time import monotonic, sleep

import numpy as np
from fastapi.testclient import TestClient

from fakes import FakeCaptureSource, FakeEngine, PositionTranscriber, encoded_chunk
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


def _make_stack(
    tmp_path: Path,
    *,
    reprocess=None,
    auto_reprocess: bool = False,
    capture_factory=None,
    transcriber_factory=None,
):
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
        transcriber_factory=transcriber_factory or (lambda language: PositionTranscriber()),
        capture_factory=capture_factory,
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


def _wait_until(condition, timeout: float = 15.0, pause: float = 0.1) -> bool:
    """Poll ``condition`` until true; used for orderings the queue finishes async."""
    deadline = monotonic() + timeout
    while monotonic() < deadline:
        if condition():
            return True
        sleep(pause)
    return condition()


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


def test_web_live_parallel_sessions(tmp_path: Path) -> None:
    """Many users record at once: two concurrent WS sessions are both accepted.

    Both get live text while running (the shared decode queue serves both).
    """
    service, live, _ = _make_stack(tmp_path)
    client = TestClient(create_app(settings=service.settings, service=service, live=live))

    with (
        client.websocket_connect("/ws/live") as first,
        client.websocket_connect("/ws/live") as second,
    ):
        first.send_json({"type": "start", "tracks": ["mic"]})
        ready_first = first.receive_json()
        assert ready_first["type"] == "ready"
        second.send_json({"type": "start", "tracks": ["mic"]})
        ready_second = second.receive_json()
        assert ready_second["type"] == "ready"
        assert ready_first["job_id"] != ready_second["job_id"]
        first_id = ready_first["job_id"]
        second_id = ready_second["job_id"]

        # interleaved streaming on both connections
        for step in range(8):
            first.send_bytes(_frame(TRACK_MIC, step))
            second.send_bytes(_frame(TRACK_MIC, 40 + step))
            sleep(0.2)

        def both_have_text() -> bool:
            segments_first = client.get(f"/api/jobs/{first_id}").json()["segments"]
            segments_second = client.get(f"/api/jobs/{second_id}").json()["segments"]
            return bool(segments_first) and bool(segments_second)

        assert _wait_until(both_have_text), "both sessions must be transcribed while both run"

        status = client.get("/api/live/status").json()
        assert status["active"] is True
        assert {item["job_id"] for item in status["sessions"]} == {first_id, second_id}
        assert all(item["capture"] == "browser" for item in status["sessions"])

        first.send_json({"type": "stop"})

    job_first = _wait_done(client, first_id)
    assert job_first["status"] == "done", job_first
    job_second = _wait_done(client, second_id)
    assert job_second["status"] == "done", job_second
    assert job_second["segments"], job_second
    for job_id in (first_id, second_id):
        assert (tmp_path / "live" / job_id / "mic.wav").stat().st_size > 44


def test_web_live_stop_one_session_keeps_other(tmp_path: Path) -> None:
    """Stopping one recording finalizes only it; the other keeps running."""
    service, live, _ = _make_stack(tmp_path)
    client = TestClient(create_app(settings=service.settings, service=service, live=live))

    with (
        client.websocket_connect("/ws/live") as first,
        client.websocket_connect("/ws/live") as second,
    ):
        first.send_json({"type": "start", "tracks": ["mic"]})
        first_id = first.receive_json()["job_id"]
        second.send_json({"type": "start", "tracks": ["mic"]})
        second_id = second.receive_json()["job_id"]

        _stream(first, TRACK_MIC, 0, 5)
        _stream(second, TRACK_MIC, 40, 5)

        first.send_json({"type": "stop"})

        job_first = _wait_done(client, first_id)
        assert job_first["status"] == "done", job_first
        assert _wait_until(
            lambda: [item["job_id"] for item in client.get("/api/live/status").json()["sessions"]]
            == [second_id]
        ), "after stopping the first session only the second must remain active"

        _stream(second, TRACK_MIC, 45, 4)
        assert client.get(f"/api/jobs/{second_id}").json()["status"] == "running"
        second.send_json({"type": "stop"})

    job_second = _wait_done(client, second_id)
    assert job_second["status"] == "done", job_second
    assert job_second["segments"], job_second


def test_web_live_transcription_queue_is_serialized(tmp_path: Path) -> None:
    """The decode queue never runs two windows concurrently, and both are served.

    Each session must get text while both record: no starvation, fair turns.
    """
    state: dict = {"lock": threading.Lock(), "active": 0, "violations": 0, "tags": []}
    counter = iter(range(1000))

    class ObservedTranscriber:
        """Position transcriber that records overlapping calls — they must not happen."""

        def __init__(self) -> None:
            self._inner = PositionTranscriber()
            self._tag = next(counter)

        def __call__(self, audio):
            with state["lock"]:
                state["active"] += 1
                if state["active"] > 1:
                    state["violations"] += 1
                state["tags"].append(self._tag)
            try:
                sleep(0.02)  # widen the race window
                return self._inner(audio)
            finally:
                with state["lock"]:
                    state["active"] -= 1

    service, live, _ = _make_stack(
        tmp_path, transcriber_factory=lambda language: ObservedTranscriber()
    )
    client = TestClient(create_app(settings=service.settings, service=service, live=live))

    with (
        client.websocket_connect("/ws/live") as first,
        client.websocket_connect("/ws/live") as second,
    ):
        first.send_json({"type": "start", "tracks": ["mic"]})
        first_id = first.receive_json()["job_id"]
        second.send_json({"type": "start", "tracks": ["mic"]})
        second_id = second.receive_json()["job_id"]

        for step in range(8):
            first.send_bytes(_frame(TRACK_MIC, step))
            second.send_bytes(_frame(TRACK_MIC, 40 + step))
            sleep(0.2)

        assert _wait_until(
            lambda: bool(client.get(f"/api/jobs/{first_id}").json()["segments"])
            and bool(client.get(f"/api/jobs/{second_id}").json()["segments"])
        ), "both sessions must get text while both record"

        first.send_json({"type": "stop"})
        second.send_json({"type": "stop"})

    assert _wait_done(client, first_id)["status"] == "done"
    assert _wait_done(client, second_id)["status"] == "done"
    assert state["violations"] == 0, "two transcription windows ran concurrently"
    assert len(set(state["tags"])) >= 2, "both sessions' transcriber instances were used"


def test_web_live_browser_and_server_sessions_coexist(tmp_path: Path) -> None:
    """A server-side capture session and browser sessions run side by side.

    A second server-side session is still rejected (shared devices).
    """

    def capture_factory(track, on_chunk, *, chunk_sec=0.2):
        return FakeCaptureSource(track, on_chunk, chunk_sec=chunk_sec)

    service, live, _ = _make_stack(tmp_path, capture_factory=capture_factory)
    client = TestClient(create_app(settings=service.settings, service=service, live=live))

    started = client.post("/api/live/start", data={"tracks": "mic"})
    assert started.status_code == 201
    server_id = started.json()["id"]
    assert client.post("/api/live/start", data={"tracks": "mic"}).status_code == 409

    with client.websocket_connect("/ws/live") as browser:
        browser.send_json({"type": "start", "tracks": ["mic"]})
        browser_id = browser.receive_json()["job_id"]
        _stream(browser, TRACK_MIC, 0, 4)

        status = client.get("/api/live/status").json()
        assert {item["job_id"] for item in status["sessions"]} == {server_id, browser_id}
        captures = {item["job_id"]: item["capture"] for item in status["sessions"]}
        assert captures[server_id] == "server"
        assert captures[browser_id] == "browser"

        browser.send_json({"type": "stop"})

    assert _wait_done(client, browser_id)["status"] == "done"
    stopped = client.post("/api/live/stop")
    assert stopped.status_code == 200
    assert stopped.json()["id"] == server_id
    assert stopped.json()["status"] == "done"


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

    assert _wait_until(lambda: handed_over == [job_id], timeout=10.0), handed_over
    job = _wait_done(client, job_id)
    assert job["status"] == "done", job


def test_web_live_each_session_chains_its_own_reprocess(tmp_path: Path) -> None:
    """With several recordings, every stopped session gets its own quality pass."""
    handed_over: list[str] = []

    def reprocess(job):
        handed_over.append(job.id)
        return None

    service, live, _ = _make_stack(tmp_path, reprocess=reprocess, auto_reprocess=True)
    client = TestClient(create_app(settings=service.settings, service=service, live=live))

    with (
        client.websocket_connect("/ws/live") as first,
        client.websocket_connect("/ws/live") as second,
    ):
        first.send_json({"type": "start", "tracks": ["mic"]})
        first_id = first.receive_json()["job_id"]
        second.send_json({"type": "start", "tracks": ["mic"]})
        second_id = second.receive_json()["job_id"]
        _stream(first, TRACK_MIC, 0, 4)
        _stream(second, TRACK_MIC, 40, 4)
        first.send_json({"type": "stop"})
        second.send_json({"type": "stop"})

    assert _wait_until(lambda: len(handed_over) == 2, timeout=15.0), handed_over
    assert set(handed_over) == {first_id, second_id}
    assert _wait_done(client, first_id)["status"] == "done"
    assert _wait_done(client, second_id)["status"] == "done"
