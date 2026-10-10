"""MOSS-Transcribe-Diarize engine: end-to-end ASR with built-in diarization.

The model is a small audio LLM that emits a diarized transcript formatted as
``[start][Sxx]text[end]``. Long inputs are processed in overlapping chunks
(the KV cache grows linearly with audio length) and duplicate segments on
chunk boundaries are removed afterwards.

Trade-off worth knowing: speaker labels (S01, S02, ...) are local to a chunk.
After all chunks are decoded, cross-chunk stitching (``speaker_stitch``)
re-labels every segment with a global speaker: segments are embedded with
ECAPA-TDNN and clustered, so one voice keeps one label across the recording.
When the embedder is unavailable (dependency or model missing), the local
labels are kept as-is — transcription never fails because of stitching.

Cancellation is cooperative: it takes effect between chunks and segments, and
also inside generation via the token callback.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import tempfile
import threading
import wave
from pathlib import Path
from typing import Any

import numpy as np

from ..domain.errors import JobCancelled
from ..domain.models import Segment
from ..metrics import note_model_loaded, touch_engine
from .base import (
    AsrResult,
    CancelCallback,
    NoSpeechError,
    PauseGate,
    ProgressCallback,
    SegmentCallback,
    TranscribeOptions,
    TranscribeProgress,
)
from .speaker_stitch import SpeakerStitcher

log = logging.getLogger(__name__)

MODEL_ID = "OpenMOSS-Team/MOSS-Transcribe-Diarize"
DEFAULT_CHUNK_SEC = 300.0
DEFAULT_OVERLAP_SEC = 2.0
DEFAULT_MAX_NEW_TOKENS = 4096
SLIVER_SEC = 10.0  # tiny trailing chunks are merged: a separate pass is too expensive
MIN_CHUNK_SEC = 60.0
MIN_MAX_NEW_TOKENS = 512

_SPEAKER_RE = re.compile(r"S(\d+)")


def _read_wav_float(path: Path) -> np.ndarray:
    """Read a 16 kHz mono PCM WAV chunk as float32 [-1..1] (for embeddings)."""
    with wave.open(str(path), "rb") as handle:
        if (
            handle.getframerate() != 16000
            or handle.getnchannels() != 1
            or handle.getsampwidth() != 2
        ):
            raise ValueError("неожиданный формат чанка для склейки спикеров")
        data = handle.readframes(handle.getnframes())
    return np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0


def normalize_speaker(raw: str) -> str:
    """Convert a MOSS speaker label such as ``S02`` to the project's ``SPEAKER_01``."""
    match = _SPEAKER_RE.search(raw)
    if match:
        return f"SPEAKER_{int(match.group(1)) - 1:02d}"
    return "SPEAKER_00"


def plan_chunks(duration: float, chunk_sec: float, overlap_sec: float) -> list[tuple[float, float]]:
    """Split a duration into overlapping ``(offset, length)`` chunks.

    Every full chunk is ``chunk_sec + overlap_sec`` long; the last chunk is
    trimmed to the remaining audio. A tiny trailing sliver is merged into the
    previous chunk instead of becoming its own pass.
    """
    if duration <= 0 or duration <= chunk_sec:
        return [(0.0, max(duration, 0.0))]
    step = chunk_sec - overlap_sec
    plan: list[tuple[float, float]] = []
    offset = 0.0
    while offset < duration:
        length = min(chunk_sec + overlap_sec, duration - offset)
        plan.append((round(offset, 3), round(length, 3)))
        offset += step
    if len(plan) > 1 and plan[-1][1] <= SLIVER_SEC:
        previous_offset = plan[-2][0]
        plan[-2] = (previous_offset, round(duration - previous_offset, 3))
        plan.pop()
    return plan


def dedupe_overlap(
    segments: list[dict[str, Any]], text_similarity: float = 0.7
) -> list[dict[str, Any]]:
    """Drop segments duplicated by chunk overlap; the longer variant wins.

    Two neighbouring segments count as duplicates when their time ranges
    overlap and their texts match via word-set similarity (Jaccard) or via
    normalized containment (chunk edges often cut a phrase differently, which
    lowers Jaccard below the threshold).
    """
    if len(segments) <= 1:
        return segments
    ordered = sorted(segments, key=lambda item: (item["start"], item["end"]))
    result: list[dict[str, Any]] = [ordered[0]]
    for segment in ordered[1:]:
        previous = result[-1]
        overlaps = (
            segment["start"] < previous["end"] - 0.5 and segment["end"] > previous["start"] + 0.5
        )
        if overlaps:
            previous_text = str(previous["text"])
            segment_text = str(segment["text"])
            if _is_duplicate_text(previous_text, segment_text, text_similarity):
                if len(segment_text) > len(previous_text):
                    result[-1] = segment
                continue
        result.append(segment)
    return result


def _is_duplicate_text(first: str, second: str, text_similarity: float) -> bool:
    """Decide whether two overlapping segments carry the same utterance."""
    words_first = set(first.lower().split())
    words_second = set(second.lower().split())
    if words_first and words_second:
        jaccard = len(words_first & words_second) / len(words_first | words_second)
        if jaccard >= text_similarity:
            return True
    return _contains_normalized(first, second)


def _contains_normalized(first: str, second: str) -> bool:
    """True when one normalized text fully contains the other (both long enough)."""
    normalized_first = _normalize_for_compare(first)
    normalized_second = _normalize_for_compare(second)
    if len(normalized_first) < 10 or len(normalized_second) < 10:
        return False
    return normalized_first in normalized_second or normalized_second in normalized_first


def _normalize_for_compare(text: str) -> str:
    """Lowercase, strip punctuation, treat ё as е, collapse spaces."""
    lowered = text.lower().replace("ё", "е")
    cleaned = re.sub(r"[^\w\s]", "", lowered, flags=re.UNICODE)
    return re.sub(r"\s+", " ", cleaned).strip()


def inject_hotwords(messages: list[dict[str, Any]], hotwords: str) -> None:
    """Append a vocabulary hint to the user text part (same slot as the reference code)."""
    hint = f" 热词提示：{hotwords}"
    for message in reversed(messages):
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    part["text"] = str(part.get("text", "")) + hint
                    return
        return


class MossEngine:
    """MOSS end-to-end transcription + diarization (offline, chunked)."""

    name = "moss"

    def __init__(
        self,
        model_id: str = MODEL_ID,
        models_dir: Path | None = None,
        device: str = "cuda",
        hf_token: str | None = None,
        chunk_sec: float = DEFAULT_CHUNK_SEC,
        overlap_sec: float = DEFAULT_OVERLAP_SEC,
        max_new_tokens: int = DEFAULT_MAX_NEW_TOKENS,
        ffmpeg: str = "ffmpeg",
        ffprobe: str = "ffprobe",
        work_dir: Path | None = None,
    ) -> None:
        self.model_id = model_id
        self.models_dir = models_dir
        self.device = device
        self.hf_token = hf_token
        self.chunk_sec = chunk_sec if chunk_sec >= MIN_CHUNK_SEC else DEFAULT_CHUNK_SEC
        self.overlap_sec = overlap_sec if 0 <= overlap_sec < self.chunk_sec else DEFAULT_OVERLAP_SEC
        self.max_new_tokens = (
            max_new_tokens if max_new_tokens >= MIN_MAX_NEW_TOKENS else DEFAULT_MAX_NEW_TOKENS
        )
        self.ffmpeg = ffmpeg
        self.ffprobe = ffprobe
        self.work_dir = work_dir
        self._model: Any = None
        self._processor: Any = None
        self._torch: Any = None
        self._lock = threading.RLock()

    # Model loading mutates GLOBAL transformers/torch state (the meta-device
    # init context), so two lazy loads running at once corrupt each other
    # ("Cannot copy out of meta tensor") — parallel queue workers each hold
    # their OWN engine instance, hence the load lock MUST be class-level,
    # not per-instance.
    _load_lock = threading.Lock()

    def unload(self) -> None:
        """Release the model and free GPU memory."""
        with self._lock:
            if self._model is None:
                return
            self._model = None
            self._processor = None
            if self._torch is not None:
                import gc

                gc.collect()
                if self._torch.cuda.is_available():
                    self._torch.cuda.empty_cache()

    def resolve_model(self) -> tuple[str, bool]:
        """Return ``(source, local_only)``, preferring a pre-downloaded local copy."""
        if self.models_dir:
            org, repo = self.model_id.split("/", 1)
            local = self.models_dir / org / repo
            if (
                (local / "config.json").is_file()
                and (local / "chat_template.jinja").is_file()
                and any(local.glob("*.safetensors"))
            ):
                return str(local), True
        return self.model_id, False

    def _load(self) -> tuple[Any, Any, Any]:
        """Load model and processor once per instance.

        Loads from ALL instances serialize through the class-level lock:
        parallel workers each hold their own engine, and concurrent
        ``from_pretrained`` calls corrupt transformers' global meta-device
        context ("Cannot copy out of meta tensor").
        """
        with self._load_lock:
            if self._model is not None:
                return self._torch, self._model, self._processor
            try:
                import torch
                from transformers import AutoModelForCausalLM, AutoProcessor
            except ImportError as exc:
                raise RuntimeError(
                    "зависимости MOSS не установлены — выполните: "
                    'uv pip install -e ".[moss]"'
                ) from exc

            source, local_only = self.resolve_model()
            device = "cuda" if self.device == "cuda" and torch.cuda.is_available() else "cpu"
            dtype = torch.bfloat16 if device == "cuda" else torch.float32
            log.info("loading MOSS '%s' on %s (%s)", source, device, dtype)

            # transformers v5 exposes auto classes through a lazy wrapper whose
            # typing mypy cannot introspect; treat the class as a plain callable.
            model_class: Any = AutoModelForCausalLM

            model = (
                model_class.from_pretrained(
                    source,
                    trust_remote_code=True,
                    dtype="auto",
                    token=self.hf_token or None,
                    local_files_only=local_only,
                )
                .to(dtype=dtype)
                .to(device)
                .eval()
            )
            processor = AutoProcessor.from_pretrained(
                source,
                trust_remote_code=True,
                token=self.hf_token or None,
                local_files_only=local_only,
            )
            if getattr(processor, "chat_template", None) is None and local_only:
                template = Path(source) / "chat_template.jinja"
                if template.is_file():
                    processor.chat_template = template.read_text(encoding="utf-8")
                    log.info("chat template loaded manually from chat_template.jinja")

            self._torch, self._model, self._processor = torch, model, processor
            note_model_loaded(self.name, self.model_id)
            return self._torch, self._model, self._processor

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
        """Transcribe and diarize the file; segments stream as chunks finish."""
        torch, model, processor = self._load()
        touch_engine(self)
        from moss_transcribe_diarize import parse_transcript
        from moss_transcribe_diarize.inference_utils import (
            build_transcription_messages,
            generate_transcription,
        )

        duration = self._probe_duration(audio_path)
        plan = plan_chunks(duration, self.chunk_sec, self.overlap_sec)
        chunk_paths: list[Path] = [audio_path]
        temp_dir: Path | None = None
        if len(plan) > 1:
            chunk_paths, temp_dir = self._split_chunks(audio_path, plan)

        all_segments: list[dict[str, Any]] = []
        # Мульти-чанковые файлы: локальные метки спикеров склеиваются в
        # глобальные после всех чанков (см. speaker_stitch).
        stitcher = (
            SpeakerStitcher(models_dir=self.models_dir) if len(chunk_paths) > 1 else None
        )
        total = len(chunk_paths)
        try:
            for index, chunk_path in enumerate(chunk_paths):
                if is_cancelled and is_cancelled():
                    raise JobCancelled()
                if pause_gate is not None:
                    pause_gate()
                offset = plan[index][0]
                self._report(on_progress, index, total, f"Чанк {index + 1}/{total}: генерация…")

                messages = build_transcription_messages(str(chunk_path))
                if options.hotwords:
                    inject_hotwords(messages, options.hotwords)

                def on_tokens(count: int, _index: int = index) -> None:
                    if is_cancelled and is_cancelled():
                        raise JobCancelled()
                    self._report(
                        on_progress,
                        _index,
                        total,
                        f"Чанк {_index + 1}/{total}: {count} токенов…",
                    )

                result = generate_transcription(
                    model,
                    processor,
                    messages,
                    max_new_tokens=self.max_new_tokens,
                    do_sample=False,
                    device=next(model.parameters()).device,
                    dtype=next(model.parameters()).dtype,
                    token_callback=on_tokens,
                )
                parsed_segments = [
                    parsed
                    for parsed in parse_transcript(result["text"])
                    if (parsed.text or "").strip()
                ]

                if stitcher is not None:
                    try:
                        stitcher.add_chunk(
                            index,
                            [
                                (float(parsed.start), float(parsed.end))
                                for parsed in parsed_segments
                            ],
                            _read_wav_float(chunk_path),
                        )
                    except Exception:  # noqa: BLE001 — склейка не должна ронять транскрибацию
                        log.exception(
                            "MOSS: не удалось собрать аудио для склейки спикеров (чанк %d)",
                            index,
                        )
                        stitcher = None

                for seq, parsed in enumerate(parsed_segments):
                    if is_cancelled and is_cancelled():
                        raise JobCancelled()
                    segment = Segment(
                        index=len(all_segments),
                        start=round(float(parsed.start) + offset, 3),
                        end=round(float(parsed.end) + offset, 3),
                        text=(parsed.text or "").strip(),
                        speaker=normalize_speaker(getattr(parsed, "speaker", "S01")),
                    )
                    item = segment.model_dump()
                    if stitcher is not None:
                        item["_chunk"] = index
                        item["_seq"] = seq
                    all_segments.append(item)
                    if on_segment:
                        on_segment(segment)

                log.info("MOSS chunk %d/%d: %d segments", index + 1, total, len(parsed_segments))
                self._report(
                    on_progress,
                    index + 1,
                    total,
                    f"Готово чанков: {index + 1}/{total}, сегментов: {len(all_segments)}",
                )
                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
        finally:
            if temp_dir is not None:
                shutil.rmtree(temp_dir, ignore_errors=True)

        before = len(all_segments)
        all_segments = dedupe_overlap(all_segments)
        if len(all_segments) != before:
            log.info("MOSS: dropped %d boundary duplicates", before - len(all_segments))

        if stitcher is not None:
            mapping = stitcher.resolve()
            for item in all_segments:
                chunk = item.pop("_chunk", None)
                seq = item.pop("_seq", None)
                if chunk is None or mapping is None:
                    continue
                new_label = mapping.get((chunk, seq))
                if new_label:
                    item["speaker"] = new_label
            if mapping:
                globals_ = sorted({label for label in mapping.values()})
                log.info(
                    "MOSS: склейка спикеров — %d глобальных %s",
                    len(globals_),
                    globals_,
                )

        for index, item in enumerate(all_segments):
            item["index"] = index

        if not all_segments:
            raise NoSpeechError("MOSS не обнаружил речи в аудио")

        return AsrResult(
            language=options.language or "auto",
            language_probability=0.0,
            duration=duration,
            segments=[
                Segment(
                    index=item["index"],
                    start=item["start"],
                    end=item["end"],
                    text=item["text"],
                    speaker=item["speaker"],
                )
                for item in all_segments
            ],
        )

    def _probe_duration(self, audio_path: Path) -> float:
        """Duration in seconds via ffprobe; 0.0 when unavailable."""
        cmd = [
            self.ffprobe, "-v", "quiet",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(audio_path),
        ]
        try:
            output = subprocess.check_output(cmd, timeout=30, stderr=subprocess.DEVNULL)
            return float(output.decode().strip())
        except (OSError, subprocess.SubprocessError, ValueError):
            return 0.0

    def _split_chunks(
        self, audio_path: Path, plan: list[tuple[float, float]]
    ) -> tuple[list[Path], Path]:
        """Cut planned chunks with ffmpeg; returns (chunk paths, temp dir)."""
        base = self.work_dir or Path(tempfile.gettempdir())
        temp_dir = Path(tempfile.mkdtemp(prefix="moss_chunks_", dir=str(base)))
        paths: list[Path] = []
        for index, (offset, length) in enumerate(plan):
            target = temp_dir / f"chunk_{index:03d}.wav"
            cmd = [
                self.ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                "-ss", f"{offset:.3f}", "-t", f"{length:.3f}",
                "-i", str(audio_path),
                "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(target),
            ]
            try:
                subprocess.run(cmd, check=True, capture_output=True, timeout=1800)
            except subprocess.CalledProcessError as exc:
                tail = (exc.stderr or b"").decode(errors="ignore").strip().splitlines()[-3:]
                raise RuntimeError(
                    f"ffmpeg не смог нарезать чанк {index}: {' / '.join(tail)}"
                ) from exc
            paths.append(target)
        return paths, temp_dir

    @staticmethod
    def _report(
        callback: ProgressCallback | None, done: int, total: int, message: str
    ) -> None:
        """Emit a progress tick; chunk-level granularity."""
        if callback is not None:
            callback(
                TranscribeProgress(fraction=min(done / max(total, 1), 1.0), message=message)
            )
