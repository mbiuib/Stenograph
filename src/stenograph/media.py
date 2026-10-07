"""FFmpeg-backed media helpers: probing and audio extraction.

Whisper expects 16 kHz mono PCM; extracting through FFmpeg up front is both
faster and more predictable than decoding inside the model.
"""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path

from .config import Settings

log = logging.getLogger(__name__)

AUDIO_EXTS = {".wav", ".mp3", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".wma"}
VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".webm", ".wmv", ".flv", ".ts", ".mts"}


def probe(path: Path, settings: Settings) -> dict:
    """Return basic media metadata via ffprobe; an empty dict when unavailable."""
    cmd = [
        settings.ffprobe, "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", str(path),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60, check=True)
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("ffprobe failed for %s: %s", path.name, exc)
        return {}
    data = json.loads(result.stdout or "{}")
    fmt = data.get("format", {})
    return {
        "format": fmt.get("format_name", ""),
        "duration": float(fmt.get("duration") or 0.0),
        "size": int(fmt.get("size") or 0),
        "has_audio": any(s.get("codec_type") == "audio" for s in data.get("streams", [])),
    }


def extract_audio(source: Path, target: Path, settings: Settings) -> None:
    """Extract a 16 kHz mono WAV track from any media file."""
    target.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        settings.ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(source), "-vn", "-ac", "1", "-ar", "16000",
        "-c:a", "pcm_s16le", str(target),
    ]
    try:
        subprocess.run(cmd, capture_output=True, text=True, timeout=3600, check=True)
    except subprocess.CalledProcessError as exc:
        tail = " / ".join((exc.stderr or "").strip().splitlines()[-3:])
        raise RuntimeError(f"ffmpeg failed: {tail}") from exc
    except OSError as exc:
        raise RuntimeError(f"ffmpeg not available: {exc}") from exc
