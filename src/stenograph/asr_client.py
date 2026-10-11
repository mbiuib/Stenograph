"""Client for the external ASR service (asr_service.py over HTTP).

The wire protocol:
  - files:    POST /v1/audio/transcriptions (multipart, verbose_json)
  - realtime: POST /v1/realtime/windows (JSON; each window is base64 of a
              16 kHz mono int16 WAV; the reply carries word tuples)

The same codec functions are used by the service side, so the marshaling
cannot drift between the two ends.
"""

from __future__ import annotations

import base64
import io
import wave
from pathlib import Path

import httpx
import numpy as np

from .domain.models import Segment
from .engines.base import AsrResult
from .live.streamer import WindowWord

SAMPLE_RATE = 16000  # the wire format: 16 kHz mono
REALTIME_MODEL = "whisper-live"  # the service's turbo alias for realtime windows


class AsrServiceError(RuntimeError):
    """The external ASR service refused or failed a request."""


def encode_wav_b64(audio: np.ndarray) -> str:
    """float32 mono audio → base64 of a 16 kHz int16 WAV."""
    pcm = np.clip(audio, -1.0, 1.0)
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(SAMPLE_RATE)
        writer.writeframes((pcm * 32767.0).astype("<i2").tobytes())
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def decode_wav_b64(data: str) -> np.ndarray:
    """base64 of a 16 kHz int16 WAV → float32 mono audio."""
    with wave.open(io.BytesIO(base64.b64decode(data)), "rb") as reader:
        raw = reader.readframes(reader.getnframes())
    return np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0


class RemoteAsrClient:
    """HTTP client for the ASR service: files and realtime windows.

    One instance is thread-safe (httpx.Client pools connections); the
    service serializes GPU work on its side, so callers may overlap.
    """

    def __init__(
        self,
        base_url: str,
        *,
        file_timeout: float = 1800.0,
        window_timeout: float = 120.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._http = httpx.Client(
            base_url=base_url.rstrip("/"),
            timeout=httpx.Timeout(file_timeout, connect=10.0),
            transport=transport,
        )
        self._file_timeout = file_timeout
        self._window_timeout = window_timeout

    def close(self) -> None:
        """Release the underlying connection pool."""
        self._http.close()

    def transcribe_file(
        self,
        audio_path: Path,
        *,
        model: str,
        language: str | None = None,
        timeout: float | None = None,
    ) -> AsrResult:
        """Upload one audio file and parse the verbose_json reply."""
        try:
            with open(audio_path, "rb") as handle:
                response = self._http.post(
                    "/v1/audio/transcriptions",
                    files={"file": (Path(audio_path).name, handle, "application/octet-stream")},
                    data={
                        "model": model,
                        "language": language or "",
                        "response_format": "verbose_json",
                    },
                    timeout=timeout or self._file_timeout,
                )
        except httpx.HTTPError as exc:
            raise AsrServiceError(f"ASR service unreachable: {exc}") from exc
        if response.status_code != 200:
            raise AsrServiceError(
                f"ASR service error {response.status_code}: {response.text[:300]}"
            )
        payload = response.json()
        segments = [
            Segment(
                index=index,
                start=float(item.get("start") or 0.0),
                end=float(item.get("end") or 0.0),
                text=str(item.get("text") or ""),
                speaker=item.get("speaker") or None,
            )
            for index, item in enumerate(payload.get("segments") or [])
        ]
        return AsrResult(
            language=str(payload.get("language") or ""),
            duration=float(payload.get("duration") or 0.0),
            segments=segments,
        )

    def transcribe_windows(
        self,
        windows: list[np.ndarray],
        *,
        model: str,
        language: str | None = None,
        timeout: float | None = None,
    ) -> list[list[WindowWord]]:
        """Send a batch of realtime windows; get word tuples back per window."""
        payload = {
            "model": model,
            "language": language,
            "windows": [encode_wav_b64(window) for window in windows],
        }
        try:
            response = self._http.post(
                "/v1/realtime/windows",
                json=payload,
                timeout=timeout or self._window_timeout,
            )
        except httpx.HTTPError as exc:
            raise AsrServiceError(f"ASR service unreachable: {exc}") from exc
        if response.status_code != 200:
            raise AsrServiceError(
                f"ASR service error {response.status_code}: {response.text[:300]}"
            )
        data = response.json()
        return [
            [(float(start), float(end), str(text)) for start, end, text in words]
            for words in data.get("windows") or []
        ]
