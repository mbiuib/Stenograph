"""Media helper tests: ffmpeg/ffprobe are faked, no real binaries are used."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from stenograph.config import Settings
from stenograph.media import extract_audio, probe


def _settings(tmp_path: Path) -> Settings:
    settings = Settings(data_dir=tmp_path / "data")
    settings.ensure_dirs()
    return settings


def test_probe_parses_ffprobe_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """probe() maps the ffprobe JSON onto our meta fields."""
    settings = _settings(tmp_path)
    payload = json.dumps(
        {
            "format": {"format_name": "mp3", "duration": "5.5", "size": "100"},
            "streams": [{"codec_type": "video"}, {"codec_type": "audio"}],
        }
    )

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(cmd, 0, stdout=payload, stderr="")

    monkeypatch.setattr("stenograph.media.subprocess.run", fake_run)

    meta = probe(tmp_path / "a.mp3", settings)
    assert meta["has_audio"] is True
    assert meta["duration"] == 5.5
    assert meta["format"] == "mp3"


def test_probe_without_audio_stream(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A stream-less or video-only container reports has_audio False."""
    settings = _settings(tmp_path)
    payload = json.dumps(
        {
            "format": {"format_name": "mp4", "duration": "1.0", "size": "10"},
            "streams": [{"codec_type": "video"}],
        }
    )

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(cmd, 0, stdout=payload, stderr="")

    monkeypatch.setattr("stenograph.media.subprocess.run", fake_run)

    assert probe(tmp_path / "a.mp4", settings)["has_audio"] is False


def test_probe_returns_empty_dict_when_ffprobe_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unreadable input: ffprobe errors, probe() degrades to an empty dict."""
    settings = _settings(tmp_path)

    def fake_run(cmd: list[str], **kwargs: object) -> object:
        raise subprocess.CalledProcessError(1, cmd, stderr="invalid data")

    monkeypatch.setattr("stenograph.media.subprocess.run", fake_run)

    assert probe(tmp_path / "a.pdf", settings) == {}


def test_probe_logs_empty_ffprobe_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """An ffprobe that answers with an empty document is logged, not silent."""
    settings = _settings(tmp_path)

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(cmd, 0, stdout="{}", stderr="")

    monkeypatch.setattr("stenograph.media.subprocess.run", fake_run)

    with caplog.at_level("WARNING", logger="stenograph.media"):
        meta = probe(tmp_path / "a.mp3", settings)

    assert meta["has_audio"] is False
    assert meta["format"] == ""
    assert "returned no streams" in caplog.text


def test_extract_audio_reports_non_media_clearly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A PDF or truncated download gets a human error, not an ffmpeg dump."""
    settings = _settings(tmp_path)

    def fake_run(cmd: list[str], **kwargs: object) -> object:
        raise subprocess.CalledProcessError(
            1,
            cmd,
            stderr=(
                "[in#0 @ 000000] Error opening input: Invalid data found when processing input\n"
                "Error opening input file x.pdf."
            ),
        )

    monkeypatch.setattr("stenograph.media.subprocess.run", fake_run)

    with pytest.raises(ValueError, match="не удалось прочитать файл «x.pdf»"):
        extract_audio(tmp_path / "x.pdf", tmp_path / "out.wav", settings)


def test_extract_audio_keeps_technical_error_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ordinary ffmpeg failures keep the technical tail for debugging."""
    settings = _settings(tmp_path)

    def fake_run(cmd: list[str], **kwargs: object) -> object:
        raise subprocess.CalledProcessError(1, cmd, stderr="Conversion failed!")

    monkeypatch.setattr("stenograph.media.subprocess.run", fake_run)

    with pytest.raises(RuntimeError, match="ffmpeg failed: Conversion failed!"):
        extract_audio(tmp_path / "x.wav", tmp_path / "out.wav", settings)
