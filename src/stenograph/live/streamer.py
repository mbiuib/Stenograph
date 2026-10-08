"""Local-agreement streaming transcription.

A tracker keeps a rolling window of *uncommitted* audio per track. On every
inference step the ASR engine transcribes the whole window; words that agree
across two consecutive hypotheses are committed as final text (LocalAgreement-2,
as in whisper_streaming). The unstable tail is emitted as a partial for the UI.
Committed audio is dropped from the window so per-step cost stays bounded.

The tracker is engine-agnostic: it takes a ``transcribe`` callable returning
word tuples, so it is fully testable without a GPU or a model.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

import numpy as np

log = logging.getLogger(__name__)

SAMPLE_RATE = 16000

WindowWord = tuple[float, float, str]  # (start, end, text), seconds from window start
WindowTranscriber = Callable[[np.ndarray], list[WindowWord]]
FinalCallback = Callable[[float, float, str], None]  # (start, end, text) absolute
PartialCallback = Callable[[str], None]


def _rms(audio: np.ndarray) -> float:
    """Root-mean-square level of a float32 signal."""
    if audio.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(np.square(audio.astype(np.float64)))))


def _common_prefix_len(left: list[WindowWord], right: list[WindowWord]) -> int:
    """Length of the case-insensitive common text prefix of two hypotheses."""
    limit = 0
    for a, b in zip(left, right, strict=False):
        if a[2].strip().casefold() != b[2].strip().casefold():
            break
        limit += 1
    return limit


class StreamTracker:
    """Streaming decoder for one audio track (not thread-safe: feed from one thread)."""

    def __init__(
        self,
        transcribe: WindowTranscriber,
        *,
        on_final: FinalCallback,
        on_partial: PartialCallback,
        step_sec: float = 0.8,
        max_window_sec: float = 25.0,
        min_rms: float = 0.0015,
        silence_drop_sec: float = 2.0,
        silence_keep_sec: float = 0.5,
        sample_rate: int = SAMPLE_RATE,
    ) -> None:
        self._transcribe = transcribe
        self._on_final = on_final
        self._on_partial = on_partial
        self._step_sec = step_sec
        self._max_window_sec = max_window_sec
        self._min_rms = min_rms
        self._silence_drop_sec = silence_drop_sec
        self._silence_keep_sec = silence_keep_sec
        self._rate = sample_rate

        self._buffer = np.zeros(0, dtype=np.float32)
        self._buffer_start = 0.0  # session time (seconds) of buffer[0]
        self._run_mark = 0.0  # buffer seconds already covered by an inference
        self._prev: list[WindowWord] = []

    # -- input ---------------------------------------------------------------

    def feed(self, audio: np.ndarray) -> None:
        """Append captured 16 kHz mono audio to the window."""
        if audio.size:
            self._buffer = np.concatenate((self._buffer, audio.astype(np.float32, copy=False)))

    @property
    def buffer_seconds(self) -> float:
        """Duration of the uncommitted audio window."""
        return self._buffer.size / self._rate

    @property
    def lag_seconds(self) -> float:
        """Audio awaiting inference beyond one normal step (≈0 when caught up)."""
        return max(0.0, self.buffer_seconds - self._run_mark - self._step_sec)

    def ready(self) -> bool:
        """True when at least ``step_sec`` of new audio awaits inference."""
        return self.buffer_seconds - self._run_mark >= self._step_sec

    # -- inference -----------------------------------------------------------

    def tick(self) -> bool:
        """Run one inference step if enough new audio is buffered."""
        if not self.ready():
            return False
        if self._buffer.size and _rms(self._buffer) < self._min_rms:
            self._drop_silence()
            return False

        words = list(self._transcribe(self._buffer))
        agree = _common_prefix_len(self._prev, words)
        finals: list[tuple[float, float, str]] = []
        partial = ""

        if agree:
            committed = words[:agree]
            finals.append(
                (
                    self._buffer_start + committed[0][0],
                    self._buffer_start + committed[-1][1],
                    " ".join(word[2] for word in committed),
                )
            )
            self._drop_before(self._buffer_start + max(0.0, committed[-1][1]))
            tail = [(s - committed[-1][1], e - committed[-1][1], t) for s, e, t in words[agree:]]
            self._prev = tail
            partial = " ".join(word[2] for word in tail)
        else:
            self._prev = list(words)
            partial = " ".join(word[2] for word in words)

        self._run_mark = self.buffer_seconds

        if self.buffer_seconds >= self._max_window_sec and self._prev:
            finals.append(
                (
                    self._buffer_start + self._prev[0][0],
                    self._buffer_start + self._prev[-1][1],
                    " ".join(word[2] for word in self._prev),
                )
            )
            self._prev = []
            self._drop_before(self._buffer_start + self.buffer_seconds)
            partial = ""

        for start, end, text in finals:
            self._on_final(start, end, text)
        self._on_partial(partial)
        return True

    def flush(self) -> None:
        """Commit whatever is buffered (session stop); always clears the window."""
        if self.buffer_seconds >= 0.1 and _rms(self._buffer) >= self._min_rms:
            words = list(self._transcribe(self._buffer))
            if words:
                self._on_final(
                    self._buffer_start + words[0][0],
                    self._buffer_start + words[-1][1],
                    " ".join(word[2] for word in words),
                )
        self._drop_before(self._buffer_start + self.buffer_seconds)
        self._prev = []
        self._run_mark = 0.0
        self._on_partial("")

    # -- window bookkeeping ----------------------------------------------------

    def _drop_silence(self) -> None:
        """Drop long silent audio, keeping a short tail for context."""
        if self.buffer_seconds >= self._silence_drop_sec:
            self._drop_before(self._buffer_start + self.buffer_seconds - self._silence_keep_sec)
            self._prev = []
            self._run_mark = self.buffer_seconds
            self._on_partial("")

    def _drop_before(self, when: float) -> None:
        """Discard buffered audio strictly before ``when`` (session seconds)."""
        cut = int((when - self._buffer_start) * self._rate)
        cut = max(0, min(cut, self._buffer.size))
        if cut >= self._buffer.size:
            self._buffer = np.zeros(0, dtype=np.float32)
            self._buffer_start = max(self._buffer_start, when)
        elif cut > 0:
            self._buffer = self._buffer[cut:]
            self._buffer_start += cut / self._rate
