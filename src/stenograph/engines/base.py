"""Engine contracts.

ASR and diarization backends implement these protocols; the pipeline only
depends on them, never on concrete libraries.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from ..domain.models import Segment


@dataclass(slots=True)
class TranscribeOptions:
    """User-tunable transcription parameters."""

    language: str | None = None  # None = auto-detect
    beam_size: int = 5
    vad: bool = True
    batch_size: int = 8
    initial_prompt: str | None = None
    hotwords: str | None = None


@dataclass(slots=True)
class TranscribeProgress:
    """Progress tick from a running engine."""

    fraction: float  # 0..1 of the audio processed
    message: str = ""


@dataclass(slots=True)
class AsrResult:
    """Final result of a transcription run."""

    language: str = ""
    language_probability: float = 0.0
    duration: float = 0.0
    segments: list[Segment] = field(default_factory=list)


class NoSpeechError(RuntimeError):
    """The engine found no recognizable speech in the audio.

    An empty result on silent or music-only input is a legitimate outcome,
    not a failure; callers decide whether to skip the input or surface it.
    """


ProgressCallback = Callable[[TranscribeProgress], None]
SegmentCallback = Callable[[Segment], None]
CancelCallback = Callable[[], bool]


class AsrEngine(Protocol):
    """Speech-to-text engine.

    Implementations are called from a single worker thread and report
    progress/segments incrementally through the callbacks.
    """

    name: str  # engine name as registered in the engine registry

    def transcribe(
        self,
        audio_path: Path,
        options: TranscribeOptions,
        *,
        on_progress: ProgressCallback | None = None,
        on_segment: SegmentCallback | None = None,
        is_cancelled: CancelCallback | None = None,
    ) -> AsrResult:
        """Transcribe a local audio file, raising JobCancelled on cancellation."""
        ...
