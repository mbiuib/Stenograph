"""Shared test doubles."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from stenograph.domain.models import Segment
from stenograph.engines.base import AsrResult, TranscribeOptions, TranscribeProgress


class FakeEngine:
    """ASR engine stand-in: emits two deterministic segments, no GPU needed."""

    name = "fake"

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.last_pause_gate: Callable[[], None] | None = None

    def transcribe(
        self,
        audio_path: Path,
        options: TranscribeOptions,
        *,
        on_progress: Callable[[TranscribeProgress], None] | None = None,
        on_segment: Callable[[Segment], None] | None = None,
        is_cancelled: Callable[[], bool] | None = None,
        pause_gate: Callable[[], None] | None = None,
    ) -> AsrResult:
        """Return canned segments; honour cancellation like a real engine."""
        from stenograph.domain.errors import JobCancelled

        if is_cancelled and is_cancelled():
            raise JobCancelled()
        self.last_pause_gate = pause_gate
        self.calls.append(str(audio_path))
        segments = [
            Segment(index=0, start=0.0, end=1.0, text="раз"),
            Segment(index=1, start=1.0, end=2.0, text="два"),
        ]
        for segment in segments:
            if on_segment:
                on_segment(segment)
        if on_progress:
            on_progress(TranscribeProgress(fraction=1.0, message="готово"))
        return AsrResult(language="ru", language_probability=0.99, duration=2.0, segments=segments)


# -- live-mode doubles --------------------------------------------------------

import threading  # noqa: E402
import time  # noqa: E402

import numpy as np  # noqa: E402

from stenograph.live.capture import CaptureError  # noqa: E402
from stenograph.live.streamer import WindowWord  # noqa: E402

RATE = 16000


def speech_audio(seconds: float, amplitude: float = 0.1) -> np.ndarray:
    """Deterministic speech-like signal loud enough to pass the RMS gate."""
    t = np.arange(int(seconds * RATE), dtype=np.float64) / RATE
    return (amplitude * np.sin(2 * np.pi * 220 * t)).astype(np.float32)


def silence_audio(seconds: float) -> np.ndarray:
    """Near-zero noise below the RMS gate."""
    rng = np.random.default_rng(0)
    return (0.0001 * rng.standard_normal(int(seconds * RATE))).astype(np.float32)


class ScriptedTranscriber:
    """Returns queued hypotheses (and repeats the last one) for streamer tests."""

    def __init__(self, scripts: list[list[WindowWord]]) -> None:
        self._scripts = list(scripts)
        self.windows: list[float] = []

    def __call__(self, audio: np.ndarray) -> list[WindowWord]:
        """Return the scripted hypothesis for this window."""
        self.windows.append(audio.size / RATE)
        if len(self._scripts) > 1:
            return self._scripts.pop(0)
        return list(self._scripts[0]) if self._scripts else []


CHIRP_BASE_HZ = 300.0
CHIRP_STEP_HZ = 50.0
CHUNK_SEC = 0.25


def encoded_chunk(index: int, seconds: float = CHUNK_SEC, amplitude: float = 0.1) -> np.ndarray:
    """A speech-loud tone whose frequency encodes the chunk's absolute index.

    PositionTranscriber decodes it back, so fake hypotheses correspond to the
    audio actually inside the window (like a real ASR would).
    """
    t = np.arange(int(seconds * RATE), dtype=np.float64) / RATE
    freq = CHIRP_BASE_HZ + CHIRP_STEP_HZ * index
    return (amplitude * np.sin(2 * np.pi * freq * t)).astype(np.float32)


class PositionTranscriber:
    """Window transcriber double that decodes the chunk grid from the audio.

    Emits one word («фразаN») per WORD_SEC of absolute time, so consecutive
    hypotheses agree on their common prefix exactly like a growing real window.
    """

    WORD_SEC = 0.5

    def __call__(self, audio: np.ndarray) -> list[WindowWord]:
        """Transcribe the window by decoding its starting chunk index."""
        if audio.size < 1024:
            return []
        probe = audio[:4096].astype(np.float64)
        probe = probe * np.hanning(probe.size)
        spectrum = np.abs(np.fft.rfft(probe))
        freqs = np.fft.rfftfreq(probe.size, 1 / RATE)
        peak = float(freqs[int(np.argmax(spectrum))])
        start_index = max(0, round((peak - CHIRP_BASE_HZ) / CHIRP_STEP_HZ))
        t0 = start_index * CHUNK_SEC
        span = t0 + audio.size / RATE
        words: list[WindowWord] = []
        m = int(t0 // self.WORD_SEC)
        while m * self.WORD_SEC < span:
            seg_start = m * self.WORD_SEC
            seg_end = seg_start + self.WORD_SEC
            overlap = min(seg_end, span) - max(seg_start, t0)
            if overlap >= 0.15:
                words.append((seg_start - t0, seg_end - t0, f"фраза{m}"))
            m += 1
        return words


class FakeCaptureSource(threading.Thread):
    """Capture source stand-in: pushes position-encoded chunks on a timer."""

    fail = False

    def __init__(self, track: str, on_chunk, *, chunk_sec: float = 0.2,
                 chunks: int = 10, interval: float = 0.2, start_chunk: int = 0) -> None:
        super().__init__(daemon=True)
        self.track = track
        self._on_chunk = on_chunk
        self._chunks = chunks
        self._interval = interval
        self._start_chunk = start_chunk
        self._stop_event = threading.Event()

    def start(self) -> None:
        """Start pushing chunks; imitates a device-open failure when fail is set."""
        if self.fail:
            raise CaptureError("устройство недоступно (тест)")
        super().start()

    def stop(self) -> None:
        """Ask the fake source to finish."""
        self._stop_event.set()

    def run(self) -> None:
        """Push the encoded chunks with small pauses, like a live device."""
        for step in range(self._chunks):
            if self._stop_event.is_set():
                break
            self._on_chunk(self.track, encoded_chunk(self._start_chunk + step))
            time.sleep(self._interval)


# -- LLM doubles ---------------------------------------------------------------


class FakeLlm:
    """Deterministic LlmClient stand-in: records prompts, returns marked text."""

    def __init__(self, *, delay: float = 0.0) -> None:
        self.calls: list[tuple[str, str]] = []
        self.delay = delay

    def describe(self) -> str:
        """Target description, like the real client reports it."""
        return "fake-llm"

    def chat(self, system: str, user: str, *, max_tokens: int | None = None) -> str:
        """Record the prompt and answer with a marker carrying the call number."""
        if self.delay:
            time.sleep(self.delay)
        self.calls.append((system, user))
        return f"ответ #{len(self.calls)}"
