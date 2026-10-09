"""Playback audio endpoints: source files, tracks and mixed live recordings."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import quote

from fastapi.testclient import TestClient

import stenograph.api.app as app_module
from fakes import FakeEngine
from stenograph.api.app import create_app
from stenograph.config import Settings
from stenograph.domain.models import Job, JobStatus
from stenograph.events import EventBus
from stenograph.service import TranscriptionService
from stenograph.storage import JobRepository


def _stack(tmp_path: Path):
    settings = Settings(data_dir=tmp_path / "data")
    settings.ensure_dirs()
    repo = JobRepository(settings.db_path)
    service = TranscriptionService(
        settings, repo, EventBus(), engine_factory=lambda name, prepared: FakeEngine()
    )
    client = TestClient(create_app(settings=settings, service=service))
    return client, repo, settings


def _wav(path: Path, payload: bytes) -> Path:
    path.write_bytes(b"RIFF" + payload)
    return path


def test_audio_serves_the_source_file(tmp_path: Path) -> None:
    """A file job streams its uploaded media back for playback."""
    client, repo, _ = _stack(tmp_path)
    media = tmp_path / "clip.mp3"
    media.write_bytes(b"ID3" + b"\x00" * 256)
    job = Job(kind="file", source_name="clip.mp3", source_path=str(media), status=JobStatus.DONE)
    repo.save(job)

    response = client.get(f"/api/jobs/{job.id}/audio")

    assert response.status_code == 200
    assert response.content == media.read_bytes()
    assert "audio" in response.headers["content-type"]


def test_audio_track_supports_range_requests(tmp_path: Path) -> None:
    """A recording track is served whole, in ranges, and only when known."""
    client, repo, _ = _stack(tmp_path)
    track = _wav(tmp_path / "system.wav", bytes(range(200)))
    job = Job(kind="live", source_name="Live — тест", status=JobStatus.DONE)
    job.meta["audio"] = {"system": str(track), "mic": str(tmp_path / "missing.wav")}
    repo.save(job)

    full = client.get(f"/api/jobs/{job.id}/audio/system")
    assert full.status_code == 200
    assert full.content == track.read_bytes()

    part = client.get(
        f"/api/jobs/{job.id}/audio/system", headers={"Range": "bytes=0-99"}
    )
    assert part.status_code == 206
    assert part.content == track.read_bytes()[:100]
    assert part.headers.get("content-range") == "bytes 0-99/204"

    assert client.get(f"/api/jobs/{job.id}/audio/mic").status_code == 404  # файла нет
    assert client.get(f"/api/jobs/{job.id}/audio/nope").status_code == 404
    assert client.get("/api/jobs/missing/audio").status_code == 404


def test_audio_live_mix_is_built_and_served(tmp_path: Path, monkeypatch) -> None:
    """A live recording without a track parameter serves the ffmpeg mix."""
    client, repo, settings = _stack(tmp_path)
    system = _wav(tmp_path / "system.wav", b"\x01" * 50)
    mic = _wav(tmp_path / "mic.wav", b"\x02" * 50)
    job = Job(kind="live", source_name="Live — тест", status=JobStatus.DONE)
    job.meta["audio"] = {"system": str(system), "mic": str(mic)}
    repo.save(job)

    calls: list[str] = []

    def fake_mix(settings_arg, job_arg):  # noqa: ANN001, ANN202 — подмена ffmpeg-микса
        calls.append(job_arg.id)
        out = settings_arg.data_dir / "mixes" / f"{job_arg.id}.mp3"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"MIXED")
        return out

    monkeypatch.setattr(app_module, "ensure_mix", fake_mix)
    response = client.get(f"/api/jobs/{job.id}/audio")

    assert response.status_code == 200
    assert response.content == b"MIXED"
    assert calls == [job.id]
    assert settings.data_dir.exists()


def test_audio_live_mix_waits_for_a_running_recording(tmp_path: Path) -> None:
    """A running live session must finish before the mix is offered."""
    client, repo, _ = _stack(tmp_path)
    track = _wav(tmp_path / "system.wav", b"\x03" * 40)
    job = Job(kind="live", source_name="Live — тест", status=JobStatus.RUNNING)
    job.meta["audio"] = {"system": str(track)}
    repo.save(job)

    assert client.get(f"/api/jobs/{job.id}/audio").status_code == 409


def test_audio_jitsi_serves_participant_tracks_only(tmp_path: Path) -> None:
    """Jitsi meetings have no single stream; participant files play directly."""
    client, repo, _ = _stack(tmp_path)
    speaker = _wav(tmp_path / "p1.wav", b"\x04" * 60)
    job = Job(kind="jitsi", source_name="Jitsi — тест", status=JobStatus.DONE)
    job.meta["audio"] = {"Спикер 1": str(speaker)}
    repo.save(job)

    assert client.get(f"/api/jobs/{job.id}/audio").status_code == 404
    named = client.get(f"/api/jobs/{job.id}/audio/{quote('Спикер 1')}")
    assert named.status_code == 200
    assert named.content == speaker.read_bytes()


def test_audio_reprocess_follows_the_recording_kind(tmp_path: Path, monkeypatch) -> None:
    """An improvement job plays like its recording: mix for live, tracks for jitsi."""
    client, repo, settings = _stack(tmp_path)
    system = _wav(tmp_path / "system.wav", b"\x05" * 30)
    live = Job(kind="reprocess", source_name="Улучшение", status=JobStatus.DONE)
    live.meta["source_kind"] = "live"
    live.meta["audio"] = {"system": str(system)}
    repo.save(live)

    def fake_mix(settings_arg, job_arg):  # noqa: ANN001, ANN202 — подмена ffmpeg-микса
        out = settings_arg.data_dir / "mixes" / f"{job_arg.id}.mp3"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"MIX")
        return out

    monkeypatch.setattr(app_module, "ensure_mix", fake_mix)
    assert client.get(f"/api/jobs/{live.id}/audio").status_code == 200

    speaker = _wav(tmp_path / "p2.wav", b"\x06" * 20)
    jitsi = Job(kind="reprocess", source_name="Улучшение", status=JobStatus.DONE)
    jitsi.meta["source_kind"] = "jitsi"
    jitsi.meta["audio"] = {"Спикер 1": str(speaker)}
    repo.save(jitsi)
    assert client.get(f"/api/jobs/{jitsi.id}/audio").status_code == 404
    assert client.get(f"/api/jobs/{jitsi.id}/audio/{quote('Спикер 1')}").status_code == 200
