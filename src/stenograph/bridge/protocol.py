"""Streaming-whisper wire format used between Jigasi and this service.

Verified against both ends:
- Jigasi `WhisperWebsocket.java`: URL `ws://host:port/ws/<uuid>`; each binary
  message is a 60-byte zero-padded UTF-8 header ``participantId|language``
  followed by raw audio; a single zero byte signals end of stream. Responses
  are JSON text frames with ``type``/``participant_id``/``text``/``variance``.
- `jitsi/skynet` streaming_whisper `utils.load_audio`: the audio payload is
  16 kHz mono little-endian int16 PCM.
"""

from __future__ import annotations

import json

import numpy as np

HEADER_BYTES = 60
SAMPLE_RATE = 16000


class FrameError(ValueError):
    """Malformed binary frame from the transcription client."""


def is_eof(data: bytes) -> bool:
    """A lone zero byte marks the end of the audio stream."""
    return len(data) == 1 and data[0] == 0


def normalize_language(tag: str) -> str | None:
    """Map a Jitsi language tag ("ru-RU") to a whisper code ("ru"); None = auto."""
    tag = (tag or "").strip().lower()
    if not tag:
        return None
    short = tag.split("-")[0]
    if short in ("", "auto", "multi", "und"):
        return None
    return short if 2 <= len(short) <= 3 and short.isalpha() else None


def parse_frame(data: bytes) -> tuple[str, str | None, np.ndarray]:
    """Parse one binary frame into (participant_id, language, float32 audio)."""
    if len(data) <= HEADER_BYTES:
        raise FrameError(f"кадр короче заголовка ({len(data)} байт)")
    header = data[:HEADER_BYTES].decode("utf-8", "ignore").rstrip("\x00")
    participant_id, _, language = header.partition("|")
    participant_id = participant_id.strip()
    if not participant_id:
        raise FrameError("пустой идентификатор участника")
    pcm = np.frombuffer(data[HEADER_BYTES:], dtype="<i2").astype(np.float32) / 32768.0
    return participant_id, normalize_language(language), pcm


def result_message(kind: str, participant_id: str, text: str, variance: float = 0.0) -> str:
    """Serialize one caption message ("partial"/"final") for Jigasi."""
    return json.dumps(
        {"type": kind, "participant_id": participant_id, "text": text, "variance": variance},
        ensure_ascii=False,
    )
