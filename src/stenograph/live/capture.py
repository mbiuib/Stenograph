"""WASAPI audio capture on Windows: system loopback and microphone.

Each source is a thread that delivers 16 kHz mono float32 chunks to a
callback. PyAudioWPatch drives WASAPI — unlike ``soundcard`` it opens
loopback endpoints reliably (including Bluetooth outputs, where soundcard
hangs). Imports of the native library are deferred so the module stays
importable on every platform.
"""

from __future__ import annotations

import contextlib
import logging
import threading
from collections.abc import Callable
from typing import Any, Literal, Protocol

import numpy as np

log = logging.getLogger(__name__)

SAMPLE_RATE = 16000
TRACKS: tuple[str, ...] = ("system", "mic")

_TRACK_LABELS = {"system": "Они", "mic": "Вы"}
Track = Literal["system", "mic"]
ChunkCallback = Callable[[str, np.ndarray], None]


class CaptureError(RuntimeError):
    """Raised when an audio source cannot be opened."""


class AudioSource(Protocol):
    """A capture thread feeding one track."""

    track: str

    def start(self) -> None:
        """Start capturing; raises CaptureError if the device cannot be opened."""

    def stop(self) -> None:
        """Ask the capture loop to finish."""

    def join(self, timeout: float | None = None) -> None:
        """Wait for the capture thread to exit."""


def track_label(track: str) -> str:
    """Human-readable speaker label for a capture track."""
    return _TRACK_LABELS.get(track, track)


def capture_supported() -> bool:
    """True when WASAPI capture is available (Windows + PyAudioWPatch)."""
    try:
        import pyaudiowpatch  # noqa: F401
    except Exception:
        return False
    return True


def describe_devices() -> dict[str, str | None]:
    """Names of the default output (its loopback) and the default input device."""
    import pyaudiowpatch as pyaudio

    api = pyaudio.PyAudio()
    try:
        return _defaults(api, pyaudio)
    finally:
        api.terminate()


def open_source(track: Track, on_chunk: ChunkCallback, *, chunk_sec: float = 0.2) -> AudioSource:
    """Create an unstarted capture source for one track."""
    if track not in TRACKS:
        raise CaptureError(f"неизвестный источник: {track}")
    return _WasapiCapture(track, on_chunk, chunk_sec=chunk_sec)


def _defaults(api: Any, pyaudio: Any) -> dict[str, str | None]:
    """Resolve the default loopback and microphone device names via WASAPI."""
    wasapi = api.get_host_api_info_by_type(pyaudio.paWASAPI)
    out = api.get_device_info_by_index(wasapi["defaultOutputDevice"])["name"]
    inp = api.get_device_info_by_index(wasapi["defaultInputDevice"])["name"]
    return {"loopback": str(out), "microphone": str(inp)}


def to_mono_16k(data: np.ndarray, rate: int) -> np.ndarray:
    """Downmix interleaved frames to mono and resample to 16 kHz."""
    if data.ndim == 2:
        data = data.mean(axis=1)
    data = data.astype(np.float32, copy=False)
    if rate == SAMPLE_RATE or data.size == 0:
        return data
    if rate % SAMPLE_RATE == 0:  # integer decimation (48k → 16k) without a resampler
        factor = rate // SAMPLE_RATE
        usable = data.size - data.size % factor
        if usable <= 0:
            return np.zeros(0, dtype=np.float32)
        return data[:usable].reshape(-1, factor).mean(axis=1).astype(np.float32)
    try:
        import soxr

        return np.asarray(soxr.resample(data, rate, SAMPLE_RATE), dtype=np.float32)
    except Exception:  # pragma: no cover - fallback for exotic rates
        count = int(data.size * SAMPLE_RATE / rate)
        if count <= 0:
            return np.zeros(0, dtype=np.float32)
        return np.interp(
            np.linspace(0, data.size - 1, count), np.arange(data.size), data
        ).astype(np.float32)


class _WasapiCapture(threading.Thread):
    """Capture thread for the default speaker loopback or the default microphone."""

    def __init__(self, track: Track, on_chunk: ChunkCallback, *, chunk_sec: float) -> None:
        super().__init__(name=f"stenograph-capture-{track}", daemon=True)
        self.track: str = track
        self._on_chunk = on_chunk
        self._chunk_sec = chunk_sec
        self._stop_event = threading.Event()
        self._ready = threading.Event()
        self._open_error: BaseException | None = None

    def start(self) -> None:
        super().start()
        self._ready.wait(timeout=5.0)
        if self._open_error is not None:
            raise CaptureError(f"не удалось открыть «{self.track}»: {self._open_error}")

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        import pyaudiowpatch as pyaudio

        api = pyaudio.PyAudio()
        stream = None
        try:
            device = self._resolve_device(api, pyaudio)
            rate = int(device["defaultSampleRate"])
            channels = max(1, int(device["maxInputChannels"]))
            frames = max(1, int(rate * self._chunk_sec))
            stream = api.open(
                format=pyaudio.paFloat32,
                channels=channels,
                rate=rate,
                input=True,
                input_device_index=int(device["index"]),
                frames_per_buffer=frames,
            )
            self._ready.set()
            while not self._stop_event.is_set():
                raw = stream.read(frames, exception_on_overflow=False)
                block = np.frombuffer(raw, dtype=np.float32)
                if block.size:
                    mono = to_mono_16k(block.reshape(-1, channels), rate)
                    if mono.size:
                        self._on_chunk(self.track, mono)
        except BaseException as exc:  # propagate to start() or log mid-run failures
            if not self._ready.is_set():
                self._open_error = exc
            else:
                log.exception("источник «%s» остановлен с ошибкой", self.track)
            self._ready.set()
        finally:
            if stream is not None:
                with contextlib.suppress(Exception):
                    stream.stop_stream()
                    stream.close()
            api.terminate()

    def _resolve_device(self, api: Any, pyaudio: Any) -> dict:
        """WASAPI device info for the source: mic input or speaker loopback."""
        wasapi = api.get_host_api_info_by_type(pyaudio.paWASAPI)
        if self.track == "mic":
            return api.get_device_info_by_index(wasapi["defaultInputDevice"])
        name = api.get_device_info_by_index(wasapi["defaultOutputDevice"])["name"]
        for info in api.get_loopback_device_info_generator():
            if str(name) in str(info["name"]):
                return info
        raise CaptureError(f"loopback-устройство для «{name}» не найдено")
