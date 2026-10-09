"""Playback audio for jobs: the source file, a mixed recording or one track.

Live recordings keep one WAV per track (system/mic), Jitsi keeps one per
participant. A single playable stream is produced by mixing the tracks with
ffmpeg and caching the result under ``data/mixes`` — for live tracks and for
Jitsi meetings recorded on the realtime timeline (``meta.audio_timeline``)
the tracks share one clock, so the mix is the meeting «как вживую».
"""

from __future__ import annotations

import logging
import re
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


def _speaker_number(name: str) -> tuple[int, str]:
    """Sort key «Спикер 2» < «Спикер 10» (не словарный порядок)."""
    match = re.search(r"(\d+)\s*$", name)
    return (int(match.group(1)) if match else 999, name)


def mixable_tracks(job: Job) -> list[str]:
    """Tracks that share one real clock and can be mixed into a single stream.

    Live tracks are continuous by construction. Jitsi tracks became continuous
    once recordings switched to the realtime timeline; meetings recorded
    before that kept speech only — no honest mix, per-speaker playback remains.
    """
    existing = existing_tracks(job)
    kind = track_kind(job)
    if kind == "live":
        ordered = [name for name in LIVE_TRACKS if name in existing]
        ordered += [name for name in existing if name not in LIVE_TRACKS]
        return ordered
    if kind == "jitsi" and str(job.meta.get("audio_timeline") or "") == "realtime":
        return sorted(existing, key=_speaker_number)
    return []


def source_file(job: Job) -> Path | None:
    """The uploaded media of a file job, when it is still in the storage."""
    if track_kind(job) != "file" or not job.source_path:
        return None
    path = Path(job.source_path)
    return path if path.is_file() else None


def mix_path(settings: Settings, job: Job) -> Path:
    """Where the cached mixed stream of this recording lives."""
    return settings.data_dir / "mixes" / f"{job.id}.mp3"


def ensure_mix(settings: Settings, job: Job, tracks: list[str]) -> Path:
    """Build (or reuse) a mixed MP3 from ``tracks`` of a recording.

    All tracks must share the recording's clock (see :func:`mixable_tracks`).
    The cache is invalidated by mtime: tracks newer than the mix trigger a
    rebuild. Raises ValueError when nothing is recorded and RuntimeError when
    ffmpeg fails.
    """
    available = existing_tracks(job)
    ordered = [available[name] for name in tracks if name in available]
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
