"""Browser live capture: audio frames uploaded over a websocket.

The web page captures the user's microphone and (optionally) shared tab or
screen audio with WebAudio, converts it to 16 kHz mono int16 and streams it
to the server in small binary frames. Frame layout: one track byte
(0 = mic, 1 = system) followed by the little-endian int16 PCM payload.
"""

from __future__ import annotations

import numpy as np

TRACK_INDEX: dict[int, str] = {0: "mic", 1: "system"}


def parse_upload_frame(data: bytes) -> tuple[str, np.ndarray] | None:
    """Parse one upload frame into (track, float32 audio); None when malformed."""
    if len(data) < 3:
        return None
    track = TRACK_INDEX.get(data[0])
    if track is None:
        return None
    payload = data[1:]
    if len(payload) % 2:  # tolerate an odd trailing byte from the browser
        payload = payload[:-1]
    if not payload:
        return None
    audio = np.frombuffer(payload, dtype="<i2").astype(np.float32) / 32768.0
    return track, audio
