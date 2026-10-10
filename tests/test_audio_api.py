"""Playback audio endpoints: source files, tracks and mixed live recordings."""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from urllib.parse import quote

import pytest
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

    calls: list[tuple[str, tuple[str, ...]]] = []

    def fake_mix(settings_arg, job_arg, tracks_arg):  # noqa: ANN001, ANN202 — подмена ffmpeg-микса
        calls.append((job_arg.id, tuple(tracks_arg)))
        out = settings_arg.data_dir / "mixes" / f"{job_arg.id}.mp3"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"MIXED")
        return out

    monkeypatch.setattr(app_module, "ensure_mix", fake_mix)
    response = client.get(f"/api/jobs/{job.id}/audio")

    assert response.status_code == 200
    assert response.content == b"MIXED"
    assert calls == [(job.id, ("system", "mic"))]
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


def test_audio_jitsi_realtime_meeting_gets_a_mix(tmp_path: Path, monkeypatch) -> None:
    """Встречи с единым таймлайном микшируются в одну дорожку; старые — нет."""
    client, repo, settings = _stack(tmp_path)
    p1 = _wav(tmp_path / "p1.wav", b"\x01" * 30)
    p2 = _wav(tmp_path / "p2.wav", b"\x02" * 30)
    fresh = Job(kind="jitsi", source_name="Jitsi — новая", status=JobStatus.DONE)
    fresh.meta["audio_timeline"] = "realtime"
    fresh.meta["audio"] = {"Спикер 2": str(p2), "Спикер 1": str(p1)}
    repo.save(fresh)
    calls: list[tuple[str, ...]] = []

    def fake_mix(settings_arg, job_arg, tracks_arg):  # noqa: ANN001, ANN202 — подмена ffmpeg-микса
        calls.append(tuple(tracks_arg))
        out = settings_arg.data_dir / "mixes" / f"{job_arg.id}.mp3"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"MIXED")
        return out

    monkeypatch.setattr(app_module, "ensure_mix", fake_mix)
    response = client.get(f"/api/jobs/{fresh.id}/audio")
    assert response.status_code == 200
    assert response.content == b"MIXED"
    assert calls == [("Спикер 1", "Спикер 2")]  # порядок спикеров, а не словарный

    old = Job(kind="jitsi", source_name="Jitsi — старая", status=JobStatus.DONE)
    old.meta["audio"] = {"Спикер 1": str(p1)}
    repo.save(old)
    assert client.get(f"/api/jobs/{old.id}/audio").status_code == 404


def test_audio_reprocess_follows_the_recording_kind(tmp_path: Path, monkeypatch) -> None:
    """An improvement job plays like its recording: mix for live, tracks for jitsi."""
    client, repo, settings = _stack(tmp_path)
    system = _wav(tmp_path / "system.wav", b"\x05" * 30)
    live = Job(kind="reprocess", source_name="Улучшение", status=JobStatus.DONE)
    live.meta["source_kind"] = "live"
    live.meta["audio"] = {"system": str(system)}
    repo.save(live)

    def fake_mix(settings_arg, job_arg, tracks_arg):  # noqa: ANN001, ANN202 — подмена ffmpeg-микса
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


def test_audio_video_source_gets_an_extracted_track(tmp_path: Path, monkeypatch) -> None:
    """A video upload plays through the extracted MP3, not the raw container."""
    client, repo, settings = _stack(tmp_path)
    video = tmp_path / "meeting.mp4"
    video.write_bytes(b"\x00" * 64)
    job = Job(
        kind="file", source_name="meeting.mp4", source_path=str(video), status=JobStatus.DONE
    )
    repo.save(job)

    calls: list[str] = []

    def fake_extract(settings_arg, job_arg):  # noqa: ANN001, ANN202 — подмена ffmpeg-извлечения
        calls.append(job_arg.id)
        out = settings_arg.data_dir / "audio" / f"{job_arg.id}.mp3"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"EXTRACTED")
        return out

    monkeypatch.setattr(app_module, "playable_source", fake_extract)
    response = client.get(f"/api/jobs/{job.id}/audio")

    assert response.status_code == 200
    assert response.content == b"EXTRACTED"
    assert calls == [job.id]


def test_playable_source_serves_audio_files_directly(tmp_path: Path) -> None:
    """Audio containers need no transcoding; a vanished upload is a ValueError."""
    from stenograph.audio import playable_source

    settings = Settings(data_dir=tmp_path / "data")
    settings.ensure_dirs()
    clip = tmp_path / "clip.mp3"
    clip.write_bytes(b"ID3" + b"\x00" * 16)
    job = Job(kind="file", source_name="clip.mp3", source_path=str(clip), status=JobStatus.DONE)

    assert playable_source(settings, job) == clip

    missing = Job(kind="file", source_name="gone.mp3", source_path=str(tmp_path / "gone.mp3"))
    with pytest.raises(ValueError, match="недоступен"):
        playable_source(settings, missing)


def test_playable_source_extracts_videos_and_caches(tmp_path: Path, monkeypatch) -> None:
    """A video is extracted once; the cached copy is reused until the source changes."""
    from stenograph import audio as audio_module

    settings = Settings(data_dir=tmp_path / "data")
    settings.ensure_dirs()
    video = tmp_path / "meeting.mp4"
    video.write_bytes(b"\x00" * 50)
    job = Job(
        kind="file", source_name="meeting.mp4", source_path=str(video), status=JobStatus.DONE
    )

    calls: list[list[str]] = []

    def fake_run(command, capture_output=False, **kwargs):  # noqa: ANN001, ANN202 — подмена ffmpeg
        calls.append(list(command))
        out = Path(command[-1])
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"MP3")
        return subprocess.CompletedProcess(command, 0, b"", b"")

    monkeypatch.setattr(audio_module.subprocess, "run", fake_run)

    first = audio_module.playable_source(settings, job)
    assert first.read_bytes() == b"MP3"
    assert len(calls) == 1 and "-vn" in calls[0]

    # Кэш: пока источник не новее копии, ffmpeg не зовём.
    second = audio_module.playable_source(settings, job)
    assert second == first and len(calls) == 1

    # Источник обновился — копия пересобирается.
    newer = time.time() + 5
    os.utime(video, (newer, newer))
    assert audio_module.playable_source(settings, job) == first
    assert len(calls) == 2


def test_playable_source_raises_when_ffmpeg_fails(tmp_path: Path, monkeypatch) -> None:
    """A broken video surfaces as RuntimeError, not an empty file."""
    from stenograph import audio as audio_module

    settings = Settings(data_dir=tmp_path / "data")
    settings.ensure_dirs()
    video = tmp_path / "broken.mkv"
    video.write_bytes(b"\x00" * 20)
    job = Job(
        kind="file", source_name="broken.mkv", source_path=str(video), status=JobStatus.DONE
    )

    def fake_run(command, capture_output=False, **kwargs):  # noqa: ANN001, ANN202 — подмена ffmpeg
        return subprocess.CompletedProcess(command, 1, b"", b"Invalid data")

    monkeypatch.setattr(audio_module.subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="не удалось извлечь звук"):
        audio_module.playable_source(settings, job)
