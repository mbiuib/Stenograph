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

    def transcribe(
        self,
        audio_path: Path,
        options: TranscribeOptions,
        *,
        on_progress: Callable[[TranscribeProgress], None] | None = None,
        on_segment: Callable[[Segment], None] | None = None,
        is_cancelled: Callable[[], bool] | None = None,
    ) -> AsrResult:
        """Return canned segments; honour cancellation like a real engine."""
        from stenograph.domain.errors import JobCancelled

        if is_cancelled and is_cancelled():
            raise JobCancelled()
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
