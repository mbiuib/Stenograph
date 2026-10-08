"""Jigasi bridge tests: wire protocol, websocket streaming, persistence."""

from __future__ import annotations

import time
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from fakes import FakeEngine, PositionTranscriber, encoded_chunk
from stenograph.api.app import create_app
from stenograph.bridge.manager import BridgeManager
from stenograph.bridge.protocol import FrameError, is_eof, normalize_language, parse_frame
from stenograph.config import Settings
from stenograph.events import EventBus
from stenograph.service import TranscriptionService
from stenograph.storage import JobRepository


def frame(participant_id: str, language: str, audio: np.ndarray) -> bytes:
    """Build one wire frame the way Jigasi's WhisperWebsocket does."""
    header = f"{participant_id}|{language}".encode().ljust(60, b"\x00")
    pcm = (np.clip(audio, -1.0, 1.0) * 32767.0).astype("<i2")
    return header + pcm.tobytes()


def test_parse_frame_roundtrip() -> None:
    """A Jigasi frame round-trips: header + int16 PCM back to float audio."""
    audio = encoded_chunk(3)
    data = frame("abcd-1234", "ru-RU", audio)
    participant_id, language, decoded = parse_frame(data)
    assert participant_id == "abcd-1234"
    assert language == "ru"
    assert decoded.shape == audio.shape
    assert np.allclose(decoded, audio, atol=1e-3)


def test_frame_edge_cases_and_languages() -> None:
    """EOF detection, malformed frames and language normalisation."""
    assert is_eof(b"\x00")
    assert not is_eof(b"\x00\x00")
    assert not is_eof(b"")
    with pytest.raises(FrameError):
        parse_frame(b"short")
    with pytest.raises(FrameError):
        parse_frame(b"|ru" + b"\x00" * 57 + b"\x01\x02")
    assert normalize_language("en-US") == "en"
    assert normalize_language("ru") == "ru"
    assert normalize_language("multi") is None
    assert normalize_language("") is None
    assert normalize_language("auto") is None


def _make_stack(tmp_path: Path):
    settings = Settings(data_dir=tmp_path, live_step_sec=0.4, live_max_window_sec=10.0)
    settings.ensure_dirs()
    repo = JobRepository(settings.db_path)
    bus = EventBus()
    service = TranscriptionService(settings, repo, bus, engine_factory=lambda n, s: FakeEngine())
    bridge = BridgeManager(
        settings, repo, bus, transcriber_factory=lambda language: PositionTranscriber()
    )
    client = TestClient(create_app(settings=settings, service=service, bridge=bridge))
    return client, repo


def test_bridge_streams_captions_and_persists(tmp_path: Path) -> None:
    """Captions flow back over the websocket; the session lands as a done job."""
    client, repo = _make_stack(tmp_path)
    with client.websocket_connect("/ws/room-1") as websocket:
        for index in range(8):
            websocket.send_bytes(frame("p1", "ru-RU", encoded_chunk(index)))

        # The worker ticks shortly after the frames arrive, but commits finals
        # only when more audio shows up or the session is flushed. So: wait
        # for the first caption, then send EOF (Jigasi's flush), then finals.
        messages = []
        for _ in range(30):
            message = websocket.receive_json()
            messages.append(message)
            if message["type"] in ("partial", "final"):
                break
        assert any(item["type"] == "partial" for item in messages), messages[:5]
        assert any("фраза" in item["text"] for item in messages)

        websocket.send_bytes(b"\x00")
        for _ in range(30):
            message = websocket.receive_json()
            messages.append(message)
            if message["type"] == "final":
                break

        final = messages[-1]
        assert final["type"] == "final"
        assert final["participant_id"] == "p1"
        assert "фраза" in final["text"]

    deadline = time.monotonic() + 10
    jobs = []
    while time.monotonic() < deadline:
        jobs = [job for job in repo.list_jobs() if job.kind == "jitsi"]
        if jobs and jobs[0].status == "done":
            break
        time.sleep(0.05)

    assert jobs, "job не создан"
    job = jobs[0]
    assert job.status == "done", job.error
    assert job.segments
    assert any("фраза" in segment.text for segment in job.segments)
    assert job.text.startswith("Спикер 1: ")
    assert job.meta["participants"] == {"p1": "Спикер 1"}
    audio_file = tmp_path / "jitsi" / job.id / "p1.wav"
    assert audio_file.is_file()
    assert audio_file.stat().st_size > 44


def test_bridge_second_participant_and_reconnect(tmp_path: Path) -> None:
    """A reconnect with the same meeting id replaces the stale session."""
    client, repo = _make_stack(tmp_path)
    with client.websocket_connect("/ws/room-x") as websocket:
        websocket.send_bytes(frame("p1", "ru", encoded_chunk(0)))
    with client.websocket_connect("/ws/room-x") as websocket:
        websocket.send_bytes(frame("p2", "ru", encoded_chunk(1)))
        websocket.send_bytes(b"\x00")

    deadline = time.monotonic() + 10
    jobs = []
    while time.monotonic() < deadline:
        jobs = [job for job in repo.list_jobs() if job.kind == "jitsi"]
        if jobs and all(job.status == "done" for job in jobs):
            break
        time.sleep(0.05)

    assert len(jobs) == 2, [(job.id, job.status) for job in jobs]
    second = jobs[0]
    assert second.status == "done"
    assert second.meta["participants"] == {"p2": "Спикер 1"}
