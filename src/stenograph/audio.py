"""Playback audio for jobs: the source file, a mixed recording or one track.

Live recordings keep one WAV per track (system/mic), Jitsi keeps one per
participant. A single playable stream for a Live-style recording is produced
by mixing its tracks with ffmpeg and caching the result under ``data/mixes``.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from .config import Settings
from .domain.models import Job

log = logging.getLogger(__name__)

LIVE_TRACKS = ("system", "mic")
MIX_BITRATE = "64k"


def track_kind(job: Job) -> str:
    """Effective recording kind of the job: file / live / jitsi / none.

    Improvement (reprocess) children carry ``meta.source_kind`` and play the
    same way as the recording they improved.
    """
    if job.kind == "file":
        return "file"
    if job.kind == "reprocess":
        return str(job.meta.get("source_kind") or "live")
    if job.kind in ("live", "jitsi"):
        return job.kind
    return "none"


def existing_tracks(job: Job) -> dict[str, str]:
    """``meta.audio`` entries whose files still exist on disk."""
    audio = job.meta.get("audio") or {}
    return {
        str(key): str(path)
        for key, path in audio.items()
        if isinstance(path, str) and Path(path).is_file()
    }


def source_file(job: Job) -> Path | None:
    """The uploaded media of a file job, when it is still in the storage."""
    if track_kind(job) != "file" or not job.source_path:
        return None
    path = Path(job.source_path)
    return path if path.is_file() else None


def mix_path(settings: Settings, job: Job) -> Path:
    """Where the cached mixed stream of this recording lives."""
    return settings.data_dir / "mixes" / f"{job.id}.mp3"


def ensure_mix(settings: Settings, job: Job) -> Path:
    """Build (or reuse) a mixed MP3 of a Live-style recording.

    The cache is invalidated by mtime: a recording whose tracks are newer than
    the mix is re-mixed. Raises ValueError when nothing is recorded and
    RuntimeError when ffmpeg fails.
    """
    tracks = existing_tracks(job)
    ordered = [tracks[key] for key in LIVE_TRACKS if key in tracks]
    if not ordered:
        raise ValueError("у записи нет сохранённых дорожек")
    out = mix_path(settings, job)
    newest = max(Path(path).stat().st_mtime for path in ordered)
    if out.is_file() and out.stat().st_mtime >= newest:
        return out
    out.parent.mkdir(parents=True, exist_ok=True)
    command = [settings.ffmpeg, "-hide_banner", "-loglevel", "error", "-y"]
    for path in ordered:
        command += ["-i", path]
    if len(ordered) > 1:
        command += [
            "-filter_complex",
            f"amix=inputs={len(ordered)}:duration=longest:normalize=0",
        ]
    command += ["-ar", "16000", "-ac", "1", "-b:a", MIX_BITRATE, str(out)]
    completed = subprocess.run(command, capture_output=True)  # noqa: S603 — local ffmpeg
    if completed.returncode != 0 or not out.is_file():
        raise RuntimeError(
            "не удалось собрать микс: " + completed.stderr.decode(errors="replace")[-300:]
        )
    log.info("аудио: микс %s собран (%d дорожек)", out.name, len(ordered))
    return out
