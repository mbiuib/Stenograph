"""Record-only mode: Live and Jitsi save audio, transcription runs later.

With ``realtime_transcribe=False`` (the settings default) sessions write the
per-track WAV files without touching a decoder; the quality re-pass (or a
manual improvement) transcribes them through the shared queue afterwards.
Each test opts out of the suite-wide realtime default explicitly.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import numpy as np
from fastapi.testclient import TestClient

from fakes import FakeEngine, PositionTranscriber, encoded_chunk
from stenograph.api.app import create_app
from stenograph.bridge.manager import BridgeManager
from stenograph.config import Settings
from stenograph.domain.models import JobStatus
from stenograph.events import EventBus
from stenograph.live.manager import LiveManager
from stenograph.service import TranscriptionService
from stenograph.storage import JobRepository

TRACK_SYSTEM = 1


def _wait_until(condition: Any, timeout: float = 20.0, pause: float = 0.1) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(pause)
    return condition()


def _frame(track: int, index: int) -> bytes:
    """One live upload frame: track byte + int16 LE position-encoded chunk."""
    pcm = (np.clip(encoded_chunk(index), -1.0, 1.0) * 32767.0).astype("<i2")
    return bytes([track]) + pcm.tobytes()


def _bridge_frame(participant_id: str, audio: np.ndarray) -> bytes:
    """One Jigasi frame: 60-byte header + int16 LE PCM."""
    header = f"{participant_id}|ru-RU".encode().ljust(60, b"\x00")
    pcm = (np.clip(audio, -1.0, 1.0) * 32767.0).astype("<i2")
    return header + pcm.tobytes()


def _drain_events(websocket: Any, timeout_s: float = 20.0) -> list[dict]:
    """Read relayed event frames until the job finishes; returns them all."""
    events: list[dict] = []
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            message = websocket.receive()
        except Exception:  # noqa: BLE001 — the socket may already be closed
            break
        if message.get("type") == "websocket.disconnect":
            break
        text = message.get("text")
        if not text:
            continue
        payload = json.loads(text)
        if payload.get("type") == "event":
            events.append(payload["event"])
            if payload["event"].get("type") in ("done", "error", "cancelled"):
                break
    return events


def _meeting(client: TestClient) -> dict | None:
    meetings = client.get("/api/jitsi/status").json()["meetings"]
    return meetings[0] if meetings else None


def _stack(
    tmp_path: Path,
    *,
    realtime: bool,
    chain: bool = False,
):
    """App stack with a factory recorder: decoding shows up as factory calls."""
    calls: list[str | None] = []

    def factory(language: str | None) -> PositionTranscriber:
        calls.append(language)
        return PositionTranscriber()

    settings = Settings(
        data_dir=tmp_path,
        live_step_sec=0.4,
        live_max_window_sec=10.0,
        realtime_transcribe=realtime,
    )
    settings.ensure_dirs()
    repo = JobRepository(settings.db_path)
    bus = EventBus()
    service = TranscriptionService(
        settings, repo, bus, engine_factory=lambda name, prepared: FakeEngine()
    )
    live = LiveManager(
        settings,
        repo,
        bus,
        transcriber_factory=factory,
        reprocess=service.chain_reprocess if chain else None,
        auto_reprocess=chain,
    )
    bridge = BridgeManager(
        settings,
        repo,
        bus,
        transcriber_factory=factory,
        pool=live,
        reprocess=service.chain_reprocess if chain else None,
        auto_reprocess=chain,
    )
    client = TestClient(create_app(settings=settings, service=service, live=live, bridge=bridge))
    return client, repo, service, calls


def test_live_record_only_skips_decoding(tmp_path: Path) -> None:
    """Record-only Live: audio hits the disk, no decoder runs, no text."""
    client, repo, _, calls = _stack(tmp_path, realtime=False)
    with client.websocket_connect("/ws/live") as websocket:
        websocket.send_json({"type": "start", "tracks": ["system"], "title": "Тихая"})
        ready = websocket.receive_json()
        assert ready["type"] == "ready"
        job_id = ready["job_id"]
        for index in range(6):
            websocket.send_bytes(_frame(TRACK_SYSTEM, index))
        websocket.send_json({"type": "stop"})
        events = _drain_events(websocket)

    assert calls == []  # трекеры не создавались: распознавания не было
    kinds = {event["type"] for event in events}
    assert "segment" not in kinds and "partial" not in kinds
    assert "done" in kinds

    job = repo.get(job_id)
    assert job is not None and job.status == JobStatus.DONE
    assert job.text == ""
    assert job.message == "Запись завершена (без распознавания)"
    assert job.meta["transcribe"] is False

    wav = tmp_path / "live" / job_id / "system.wav"
    assert wav.is_file()
    assert wav.stat().st_size == 44 + 6 * (len(_frame(TRACK_SYSTEM, 0)) - 1)


def test_ws_start_transcribe_flag_overrides_default(tmp_path: Path) -> None:
    """The start frame can force record-only for this session — and back on."""
    client, _, _, calls = _stack(tmp_path, realtime=True)

    with client.websocket_connect("/ws/live") as websocket:
        websocket.send_json({"type": "start", "tracks": ["system"], "transcribe": False})
        assert websocket.receive_json()["type"] == "ready"
        websocket.send_bytes(_frame(TRACK_SYSTEM, 0))
        websocket.send_json({"type": "stop"})
        _drain_events(websocket)
    assert calls == []  # флаг выключил распознавание для этой сессии

    with client.websocket_connect("/ws/live") as websocket:
        websocket.send_json({"type": "start", "tracks": ["system"], "transcribe": True})
        assert websocket.receive_json()["type"] == "ready"
        for index in range(4):
            websocket.send_bytes(_frame(TRACK_SYSTEM, index))
        websocket.send_json({"type": "stop"})
        events = _drain_events(websocket)
    assert calls  # явное «включено» вернуло живое распознавание
    assert any(event["type"] == "segment" for event in events)


def test_record_only_stop_chains_the_improvement(tmp_path: Path) -> None:
    """A record-only session still goes to the queue for later decoding."""
    client, repo, _, _ = _stack(tmp_path, realtime=False, chain=True)
    with client.websocket_connect("/ws/live") as websocket:
        websocket.send_json({"type": "start", "tracks": ["system"]})
        ready = websocket.receive_json()
        job_id = ready["job_id"]
        websocket.send_bytes(_frame(TRACK_SYSTEM, 0))
        websocket.send_json({"type": "stop"})
        _drain_events(websocket)

    assert _wait_until(
        lambda: any(job.kind == "reprocess" for job in repo.list_jobs()), timeout=15
    ), [(job.kind, job.status) for job in repo.list_jobs()]
    child = next(job for job in repo.list_jobs() if job.kind == "reprocess")
    assert child.meta["parent"] == job_id
    assert child.meta["auto"] is True


def test_bridge_record_only_records_without_decoding(tmp_path: Path) -> None:
    """Record-only Jitsi: participant audio is saved, no captions decoded."""
    client, repo, _, calls = _stack(tmp_path, realtime=False)
    with client.websocket_connect("/ws/room-quiet") as websocket:
        websocket.send_bytes(_bridge_frame("p1", encoded_chunk(0)))
        websocket.send_bytes(_bridge_frame("p1", encoded_chunk(1)))

        assert _wait_until(
            lambda: bool((_meeting(client) or {}).get("participants")), timeout=10
        ), _meeting(client)
        info = _meeting(client)
        assert info is not None
        assert info["transcribe"] is False
        participant = info["participants"][0]
        assert participant["audio_sec"] > 0
        assert participant["segments"] == 0
        assert calls == []

    assert _wait_until(
        lambda: any(
            job.kind == "jitsi" and job.status == JobStatus.DONE for job in repo.list_jobs()
        ),
        timeout=20,
    ), [(job.kind, job.status) for job in repo.list_jobs()]
    job = next(job for job in repo.list_jobs() if job.kind == "jitsi")
    assert job.text == ""
    assert job.message == "Запись завершена (без распознавания)"
    assert job.meta["transcribe"] is False
    wav = tmp_path / "jitsi" / job.id / "p1.wav"
    assert wav.is_file()
    assert wav.stat().st_size == 44 + 2 * (len(_bridge_frame("p1", encoded_chunk(0))) - 60)


def test_bridge_transcribe_toggle_mid_meeting(tmp_path: Path) -> None:
    """The Jitsi page flips realtime decoding on and off during a meeting."""
    client, _, _, calls = _stack(tmp_path, realtime=False)
    with client.websocket_connect("/ws/room-switch") as websocket:
        websocket.send_bytes(_bridge_frame("p1", encoded_chunk(0)))
        websocket.send_bytes(_bridge_frame("p1", encoded_chunk(1)))
        assert _wait_until(
            lambda: bool((_meeting(client) or {}).get("participants")), timeout=10
        ), _meeting(client)
        assert calls == []

        response = client.post(
            "/api/jitsi/transcribe", data={"meeting_id": "room-switch", "enabled": "true"}
        )
        assert response.status_code == 200
        assert response.json() == {"meeting_id": "room-switch", "transcribe": True}
        time.sleep(0.3)

        for index in range(2, 12):
            websocket.send_bytes(_bridge_frame("p1", encoded_chunk(index)))
            time.sleep(0.12)
        assert _wait_until(
            lambda: (
                ((_meeting(client) or {}).get("participants") or [{}])[0].get("segments", 0) > 0
            ),
            timeout=20,
        ), _meeting(client)
        assert calls  # трекер создан на лету при включении

        response = client.post(
            "/api/jitsi/transcribe", data={"meeting_id": "room-switch", "enabled": "false"}
        )
        assert response.status_code == 200
        assert response.json()["transcribe"] is False
        assert (_meeting(client) or {}).get("transcribe") is False

    response = client.post(
        "/api/jitsi/transcribe", data={"meeting_id": "nope", "enabled": "true"}
    )
    assert response.status_code == 404


def test_record_only_session_does_not_hold_file_jobs(tmp_path: Path) -> None:
    """Files keep flowing while a record-only session is airing."""
    client, repo, service, _ = _stack(tmp_path, realtime=False)
    source = tmp_path / "clip.wav"
    source.write_bytes(b"\x00" * 32)

    with client.websocket_connect("/ws/live") as websocket:
        websocket.send_json({"type": "start", "tracks": ["system"]})
        assert websocket.receive_json()["type"] == "ready"
        websocket.send_bytes(_frame(TRACK_SYSTEM, 0))
        time.sleep(0.3)

        job = service.submit_file(source, language="ru")
        assert _wait_until(
            lambda: (repo.get(job.id) or job).status == JobStatus.DONE, timeout=15
        ), (repo.get(job.id) or job).model_dump()
        websocket.send_json({"type": "stop"})
        _drain_events(websocket)


def test_transcribing_session_still_holds_file_jobs(tmp_path: Path) -> None:
    """The realtime gate still protects the decoder when it does work."""
    client, repo, service, _ = _stack(tmp_path, realtime=True)
    source = tmp_path / "clip.wav"
    source.write_bytes(b"\x00" * 32)

    with client.websocket_connect("/ws/live") as websocket:
        websocket.send_json({"type": "start", "tracks": ["system"]})
        assert websocket.receive_json()["type"] == "ready"
        websocket.send_bytes(_frame(TRACK_SYSTEM, 0))
        time.sleep(0.3)

        job = service.submit_file(source, language="ru")
        time.sleep(1.0)
        held = repo.get(job.id)
        assert held is not None and held.status == JobStatus.QUEUED
        assert held.message == "Ждёт: идёт живая запись"

        websocket.send_json({"type": "stop"})
        _drain_events(websocket)

    assert _wait_until(
        lambda: (repo.get(job.id) or job).status == JobStatus.DONE, timeout=15
    ), (repo.get(job.id) or job).model_dump()
