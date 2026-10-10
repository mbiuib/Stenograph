"""Browser live capture: WebSocket upload feeds the same live pipeline (no GPU)."""

from __future__ import annotations

import re
import threading
from pathlib import Path
from time import monotonic, sleep

import numpy as np
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from fakes import FakeCaptureSource, FakeEngine, PositionTranscriber, encoded_chunk
from stenograph.api.app import create_app
from stenograph.config import Settings
from stenograph.domain.models import Job
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


def _silence_frame(track: int, seconds: float = 0.25) -> bytes:
    """One upload frame carrying digital silence (below the RMS gate)."""
    pcm = np.zeros(int(seconds * 16000), dtype="<i2")
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


def test_web_live_ws_title_names_the_job(tmp_path: Path) -> None:
    """The title goes last; the type prefix (and device tag) always come first."""
    service, live, _bus = _make_stack(tmp_path)
    client = TestClient(create_app(settings=service.settings, service=service, live=live))

    with client.websocket_connect("/ws/live") as ws:
        ws.send_json({"type": "start", "tracks": ["mic"], "title": "Созвон команды"})
        ready = ws.receive_json()
        assert ready["type"] == "ready"
        titled_job = service.get(ready["job_id"])
        assert titled_job is not None
        assert titled_job.source_name.startswith("Live — ")
        assert titled_job.source_name.endswith(" — Созвон команды")

    with client.websocket_connect("/ws/live") as ws:
        ws.send_json({"type": "start", "tracks": ["mic"]})
        ready = ws.receive_json()
        default_job = service.get(ready["job_id"])
        assert default_job is not None and default_job.source_name.startswith("Live — ")


def test_web_live_ws_client_device_info(tmp_path: Path) -> None:
    """The start-frame device report lands in the metadata and the job name."""
    service, live, _bus = _make_stack(tmp_path)
    client = TestClient(create_app(settings=service.settings, service=service, live=live))
    user_agent = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
    )

    with client.websocket_connect("/ws/live") as ws:
        ws.send_json(
            {
                "type": "start",
                "tracks": ["mic"],
                "title": "Планёрка",
                "client": {"user_agent": user_agent, "platform": "Win32", "language": "ru-RU"},
                "capture_devices": {"mic": "Микрофон (USB Audio)"},
            }
        )
        ready = ws.receive_json()
        assert ready["type"] == "ready"
        job = service.get(ready["job_id"])

    assert job is not None
    assert re.fullmatch(
        r"Live — Chrome 141 · Windows — \d{1,2} [а-я]{3}, \d{2}:\d{2} — Планёрка",
        job.source_name,
    ), job.source_name
    assert job.meta["client"]["user_agent"] == user_agent
    assert job.meta["client"]["language"] == "ru-RU"
    assert job.meta["client"]["ip"]  # stamped from the websocket
    assert job.meta["capture_devices"] == {"mic": "Микрофон (USB Audio)"}


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


def test_live_ws_relays_job_events(tmp_path: Path) -> None:
    """The capture socket also carries this job's transcript events.

    Chrome allows only six HTTP/1.1 sockets per host: an EventSource per
    recording tab consumes one and starves every other request (status
    polls included) once several tabs record. So the live events must ride
    the capture WebSocket, which lives outside that pool.

    Falsification: without the relay only the handshake answers — no event
    ever arrives and the receive loop below times out.
    """
    service, live, _bus = _make_stack(tmp_path)
    client = TestClient(create_app(settings=service.settings, service=service, live=live))

    with client.websocket_connect("/ws/live") as ws:
        ws.send_json({"type": "start", "tracks": ["mic"]})
        ready = ws.receive_json()
        assert ready["type"] == "ready"
        for step in range(6):
            ws.send_bytes(_frame(TRACK_MIC, step))
            sleep(0.1)

        seen: dict = {}
        for _ in range(80):
            message = ws.receive_json()
            if message.get("type") == "event":
                seen = message["event"]
                break
        assert seen, "живые события обязаны приезжать по сокету захвата"
        assert seen.get("type") in ("status", "meta", "level", "partial", "segment")
        ws.send_json({"type": "stop"})

    assert _wait_done(client, ready["job_id"])["status"] == "done"


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


def test_web_live_rest_stop_closes_the_upload_socket(tmp_path: Path) -> None:
    """Stopping a browser session from another page closes its websocket.

    The recording page must learn the session ended (clean close) instead of
    streaming into a finished session.
    """
    service, live, _ = _make_stack(tmp_path)
    client = TestClient(create_app(settings=service.settings, service=service, live=live))

    with client.websocket_connect("/ws/live") as ws:
        ws.send_json({"type": "start", "tracks": ["mic"]})
        job_id = ws.receive_json()["job_id"]
        _stream(ws, TRACK_MIC, 0, 4)

        stopped = client.post("/api/live/stop", data={"job_id": job_id})
        assert stopped.status_code == 200
        assert stopped.json()["status"] == "done", stopped.json()

        # server closes the upload socket once the session is finalized;
        # relayed live events may still arrive before the close frame
        with pytest.raises(WebSocketDisconnect):
            for _ in range(50):
                message = ws.receive_json()
                assert message.get("type") == "event", message

    assert _wait_done(client, job_id)["status"] == "done"
    assert client.get("/api/live/status").json()["sessions"] == []


def test_web_live_stop_finalizes_after_a_silent_tail(tmp_path: Path) -> None:
    """Stopping with a short silent tail must not hang.

    A silent window inside the tracker cannot be drained by inference (RMS
    gate) and is too short for the silence drop: the queue must flush it and
    complete the job instead of waiting for the lag to reach zero forever.
    """
    service, live, _ = _make_stack(tmp_path)
    client = TestClient(create_app(settings=service.settings, service=service, live=live))

    with client.websocket_connect("/ws/live") as ws:
        ws.send_json({"type": "start", "tracks": ["mic"]})
        job_id = ws.receive_json()["job_id"]
        _stream(ws, TRACK_MIC, 0, 4)  # speech: the session starts transcribing
        sleep(0.6)  # let the queue drain it up to the live edge
        for _ in range(5):  # ~1.25 s of silence, below the 2 s drop threshold
            ws.send_bytes(_silence_frame(TRACK_MIC))
            sleep(0.15)

        status = client.get("/api/live/status").json()
        lag = next(item["lag_sec"] for item in status["sessions"] if item["job_id"] == job_id)
        assert lag > 0, f"the silent tail must sit in the undrainable zone (lag={lag})"

        ws.send_json({"type": "stop"})

    job = _wait_done(client, job_id, timeout=10.0)
    assert job["status"] == "done", job


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


def test_chain_reprocess_uses_configured_engine(tmp_path: Path) -> None:
    """MEETSCRIBE_REPROCESS_ENGINE picks the auto-chained improvement engine."""
    settings = Settings(data_dir=tmp_path, reprocess_engine="whisper")
    settings.ensure_dirs()
    repo = JobRepository(settings.db_path)
    service = TranscriptionService(
        settings, repo, EventBus(), engine_factory=lambda n, s: FakeEngine()
    )
    track = tmp_path / "system.wav"
    track.write_bytes(b"RIFF")
    recording = Job(kind="live", source_name="Live — тест")
    recording.meta["audio"] = {"system": str(track)}
    recording.meta["audio_timeline"] = "realtime"
    repo.save(recording)

    child = service.chain_reprocess(recording)
    assert child.meta["request"]["engine"] == "whisper"
    assert child.meta["auto"] is True
    assert child.meta["audio_timeline"] == "realtime"  # ребёнок играет как запись


def test_chain_reprocess_unknown_engine_falls_back(tmp_path: Path) -> None:
    """A typo in the setting must not break auto-improvement for every stop."""
    settings = Settings(data_dir=tmp_path, reprocess_engine="nope")
    settings.ensure_dirs()
    repo = JobRepository(settings.db_path)
    service = TranscriptionService(
        settings, repo, EventBus(), engine_factory=lambda n, s: FakeEngine()
    )
    track = tmp_path / "system.wav"
    track.write_bytes(b"RIFF")
    recording = Job(kind="live", source_name="Live — тест")
    recording.meta["audio"] = {"system": str(track)}
    repo.save(recording)

    child = service.chain_reprocess(recording)
    assert child.meta["request"]["engine"] is None


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
