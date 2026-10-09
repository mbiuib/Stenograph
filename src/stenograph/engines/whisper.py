"""Faster-Whisper ASR engine.

Runs CTranslate2 Whisper models on GPU (CUDA) or CPU. Two known pitfalls are
handled here: CUDA DLL registration on Windows (via ..cuda, before import)
and Whisper's occasional multi-minute segments when VAD finds no pauses —
those are re-split on word boundaries.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import numpy as np

from .. import cuda
from ..domain.errors import JobCancelled
from ..domain.models import Segment
from ..metrics import note_model_loaded, touch_engine
from .base import (
    AsrResult,
    CancelCallback,
    PauseGate,
    ProgressCallback,
    SegmentCallback,
    TranscribeOptions,
    TranscribeProgress,
)

log = logging.getLogger(__name__)

# Register CUDA DLL directories before anything imports ctranslate2.
cuda.prepare()

MAX_SEGMENT_SEC = 25.0

# Live streaming audio arrives as float32 mono at 16 kHz (see live/streamer.py).
_LIVE_RATE = 16000


class FasterWhisperEngine:
    """Whisper via faster-whisper; the model is loaded lazily, once per process."""

    name = "whisper"

    def __init__(
        self,
        model: str = "large-v3",
        models_dir: Path | None = None,
        device: str = "cuda",
        compute_type: str = "float16",
    ) -> None:
        self.model_id = model
        self.models_dir = models_dir
        self.device = device
        self.compute_type = compute_type
        self._model: Any = None

    def resolve_model(self) -> str:
        """Prefer a local model directory (LM-Studio-like layout) over a Hub download."""
        if self.models_dir:
            candidates = [
                self.models_dir / "Systran" / f"faster-whisper-{self.model_id}",
                self.models_dir / self.model_id,
            ]
            for candidate in candidates:
                if (candidate / "model.bin").is_file():
                    return str(candidate)
        return self.model_id

    def _load(self) -> Any:
        if self._model is None:
            cuda.prepare()
            from faster_whisper import WhisperModel

            ref = self.resolve_model()
            log.info("loading whisper model '%s' on %s (%s)", ref, self.device, self.compute_type)
            self._model = WhisperModel(ref, device=self.device, compute_type=self.compute_type)
            note_model_loaded(self.name, self.model_id)
        return self._model

    def transcribe(
        self,
        audio_path: Path,
        options: TranscribeOptions,
        *,
        on_progress: ProgressCallback | None = None,
        on_segment: SegmentCallback | None = None,
        is_cancelled: CancelCallback | None = None,
        pause_gate: PauseGate | None = None,
    ) -> AsrResult:
        """Transcribe the file; reports every segment as soon as it is decoded."""
        if pause_gate is not None:
            # The batched pass cannot yield mid-run: hold right before it.
            pause_gate()
        model = self._load()
        touch_engine(self)
        from faster_whisper import BatchedInferencePipeline

        kwargs: dict[str, Any] = {
            "beam_size": options.beam_size,
            "vad_filter": options.vad,
            "word_timestamps": True,
            "temperature": [0.0, 0.2, 0.4, 0.6, 0.8, 1.0],
            "no_speech_threshold": 0.5,
            "log_prob_threshold": -1.0,
            "compression_ratio_threshold": 2.4,
            "condition_on_previous_text": False,
        }
        if options.vad:
            kwargs["vad_parameters"] = {"min_silence_duration_ms": 200, "speech_pad_ms": 150}
        if options.language:
            kwargs["language"] = options.language
        if options.initial_prompt:
            kwargs["initial_prompt"] = options.initial_prompt
        if options.hotwords:
            kwargs["hotwords"] = options.hotwords

        if options.vad:
            segments, info = BatchedInferencePipeline(model=model).transcribe(
                str(audio_path), batch_size=options.batch_size, **kwargs
            )
        else:
            segments, info = model.transcribe(str(audio_path), **kwargs)

        duration = float(getattr(info, "duration", 0.0) or 0.0)
        raw_segments: list[dict[str, Any]] = []
        for raw in segments:
            if is_cancelled and is_cancelled():
                raise JobCancelled()
            text = (raw.text or "").strip()
            if not text:
                continue
            words = [
                {"start": float(w.start), "end": float(w.end), "word": str(w.word)}
                for w in (getattr(raw, "words", None) or [])
            ]
            raw_segments.append(
                {"start": float(raw.start), "end": float(raw.end), "text": text, "words": words}
            )
            if on_segment:
                on_segment(
                    Segment(
                        index=len(raw_segments) - 1,
                        start=float(raw.start),
                        end=float(raw.end),
                        text=text,
                    )
                )
            if on_progress and duration > 0:
                on_progress(
                    TranscribeProgress(
                        fraction=min(float(raw.end) / duration, 1.0), message=text[:80]
                    )
                )

        final_segments = resplit_long_segments(raw_segments, MAX_SEGMENT_SEC)
        return AsrResult(
            language=str(getattr(info, "language", "") or ""),
            language_probability=float(getattr(info, "language_probability", 0.0) or 0.0),
            duration=duration,
            segments=[
                Segment(index=i, start=s["start"], end=s["end"], text=s["text"])
                for i, s in enumerate(final_segments)
            ],
        )

    def transcribe_window(
        self,
        audio: np.ndarray,
        *,
        language: str | None = None,
        beam_size: int = 1,
        no_speech_threshold: float = 0.6,
    ) -> list[tuple[float, float, str]]:
        """Transcribe a short float32 16 kHz window for the live mode.

        Returns word tuples ``(start, end, text)`` relative to the window start;
        greedy decoding keeps each streaming step cheap.
        """
        model = self._load()
        touch_engine(self)
        segments, _info = model.transcribe(
            audio,
            language=language,
            beam_size=beam_size,
            vad_filter=False,
            word_timestamps=True,
            temperature=0.0,
            no_speech_threshold=no_speech_threshold,
            log_prob_threshold=-1.0,
            compression_ratio_threshold=2.4,
            condition_on_previous_text=False,
        )
        words: list[tuple[float, float, str]] = []
        for segment in segments:
            for word in getattr(segment, "words", None) or []:
                text = str(getattr(word, "word", "") or "").strip()
                if text:
                    words.append(
                        (
                            float(getattr(word, "start", 0.0) or 0.0),
                            float(getattr(word, "end", 0.0) or 0.0),
                            text,
                        )
                    )
        return words

    def transcribe_batch(
        self, windows: list[np.ndarray], *, language: str | None = None
    ) -> list[list[tuple[float, float, str]]]:
        """Transcribe independent short windows in one batched engine pass.

        Each window is padded to the model's 30 s grid and stacked into one
        batch; CTranslate2 decodes the rows independently, so a window's text
        does not depend on its batch neighbours (unlike a joined pass). The
        singled-window fallback keeps live decoding alive if the internals of
        the installed faster-whisper ever drift.
        """
        model = self._load()
        touch_engine(self)
        if not windows:
            return []
        output: list[list[tuple[float, float, str]]] = [[] for _ in windows]
        indices = [index for index, window in enumerate(windows) if window.size > 0]
        if not indices:
            return output
        try:
            words = _batched_windows(model, [windows[index] for index in indices], language)
            for position, index in enumerate(indices):
                output[index] = words[position]
        except Exception:  # noqa: BLE001 — private pipeline internals may drift
            log.exception("батч-транскрибация не удалась — падаю на одиночные окна")
            for index in indices:
                output[index] = self.transcribe_window(
                    windows[index], language=language, beam_size=1
                )
        return output


def _batched_windows(
    model: Any, windows: list[np.ndarray], language: str | None
) -> list[list[tuple[float, float, str]]]:
    """One CTranslate2 pass over stacked windows; per-row independent decode.

    Mirrors faster-whisper's ``BatchedInferencePipeline`` without the VAD
    chunking: every window becomes its own padded row, the rows are decoded
    as a batch, and the word timestamps are aligned per row (short windows
    spend nearly all their cost on fixed per-inference overhead, so a batch
    of rows multiplies the streaming throughput).
    """
    from faster_whisper.audio import pad_or_trim
    from faster_whisper.tokenizer import Tokenizer
    from faster_whisper.transcribe import TranscriptionOptions, get_compression_ratio

    feats = [model.feature_extractor(window)[..., :-1] for window in windows]
    features = np.stack([pad_or_trim(feat) for feat in feats])
    tokenizer = Tokenizer(
        model.hf_tokenizer,
        model.model.is_multilingual,
        task="transcribe",
        language=language or "en",
    )
    options = TranscriptionOptions(
        beam_size=1,
        best_of=1,
        patience=1.0,
        length_penalty=1.0,
        repetition_penalty=1.0,
        no_repeat_ngram_size=0,
        log_prob_threshold=-1.0,
        no_speech_threshold=0.6,
        compression_ratio_threshold=2.4,
        temperatures=[0.0],
        initial_prompt=None,
        prefix=None,
        suppress_blank=True,
        suppress_tokens=None,
        prepend_punctuations="\"'“¿([{-",
        append_punctuations="\"'.。,，!！?？:：”)]}、",
        max_new_tokens=None,
        hotwords=None,
        word_timestamps=True,
        hallucination_silence_threshold=None,
        condition_on_previous_text=False,
        clip_timestamps="0",
        prompt_reset_on_temperature=0.5,
        multilingual=bool(model.model.is_multilingual and language is None),
        without_timestamps=False,
        max_initial_timestamp=0.0,
    )
    prompt = model.get_prompt(
        tokenizer, previous_tokens=[], without_timestamps=False, hotwords=None
    )
    encoder_output = model.encode(features)
    prompts = [prompt.copy() for _ in windows]
    if options.multilingual:
        language_token_index = prompt.index(tokenizer.language)
        for row, detected in enumerate(model.model.detect_language(encoder_output)):
            prompts[row][language_token_index] = tokenizer.tokenizer.token_to_id(
                detected[0][0]
            )
    results = model.model.generate(
        encoder_output,
        prompts,
        beam_size=options.beam_size,
        patience=options.patience,
        length_penalty=options.length_penalty,
        max_length=model.max_length,
        suppress_blank=options.suppress_blank,
        suppress_tokens=options.suppress_tokens,
        return_scores=True,
        return_no_speech_prob=True,
        sampling_temperature=options.temperatures[0],
        repetition_penalty=options.repetition_penalty,
        no_repeat_ngram_size=options.no_repeat_ngram_size,
    )
    segmented: list[list[dict[str, Any]]] = []
    sizes: list[int] = []
    for window, result in zip(windows, results, strict=True):
        duration = window.size / _LIVE_RATE
        segment_size = int(np.ceil(duration) * model.frames_per_second)
        sizes.append(segment_size)
        seq_len = len(result.sequences_ids[0])
        cum_logprob = result.scores[0] * (seq_len**options.length_penalty)
        subsegments, _seek, _single_timestamp_ending = model._split_segments_by_timestamps(
            tokenizer=tokenizer,
            tokens=result.sequences_ids[0],
            time_offset=0.0,
            segment_size=segment_size,
            segment_duration=duration,
            seek=0,
        )
        segmented.append(
            [
                dict(
                    text=tokenizer.decode(subsegment["tokens"]),
                    avg_logprob=cum_logprob / (seq_len + 1),
                    no_speech_prob=result.no_speech_prob,
                    tokens=subsegment["tokens"],
                    start=subsegment["start"],
                    end=subsegment["end"],
                    compression_ratio=get_compression_ratio(
                        tokenizer.decode(subsegment["tokens"])
                    ),
                    seek=0,
                )
                for subsegment in subsegments
            ]
        )
    model.add_word_timestamps(
        segmented,
        tokenizer,
        encoder_output,
        sizes,
        options.prepend_punctuations,
        options.append_punctuations,
        0.0,
    )
    output: list[list[tuple[float, float, str]]] = []
    for segments in segmented:
        words: list[tuple[float, float, str]] = []
        for segment in segments:
            for word in segment.get("words") or []:
                text = str(word.get("word", "")).strip()
                if text:
                    words.append((float(word["start"]), float(word["end"]), text))
        output.append(words)
    return output


def resplit_long_segments(
    segments: list[dict[str, Any]], max_sec: float = MAX_SEGMENT_SEC
) -> list[dict[str, Any]]:
    """Split segments longer than max_sec into word-aligned chunks.

    Whisper occasionally returns multi-minute "segments" when VAD finds no
    pauses; splitting keeps downstream diarization and reading sane. Segments
    without word timestamps pass through untouched.
    """
    out: list[dict[str, Any]] = []
    for segment in segments:
        duration = segment["end"] - segment["start"]
        if duration <= max_sec or not segment.get("words"):
            out.append(segment)
            continue
        bucket: list[dict[str, Any]] = []
        for word in segment["words"]:
            bucket.append(word)
            if bucket[-1]["end"] - bucket[0]["start"] >= max_sec:
                out.append(_bucket_to_segment(bucket))
                bucket = []
        if bucket:
            out.append(_bucket_to_segment(bucket))
    return out


def _bucket_to_segment(words: list[dict[str, Any]]) -> dict[str, Any]:
    """Collapse a word bucket into a segment dict."""
    return {
        "start": words[0]["start"],
        "end": words[-1]["end"],
        "text": "".join(str(w["word"]) for w in words).strip(),
        "words": list(words),
    }
