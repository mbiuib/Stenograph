"""GigaAM Multilingual ASR engine (``ai-sage/GigaAM-Multilingual``).

Conformer-энкодер + символьный CTC-декодер, 70+ языков (лучшие открытые
результаты для ru/kk/ky/uz, умеренные для en). HF-интеграция — remote code:
``config.json`` + ``modeling_gigaam.py`` (trust_remote_code); веса — ревизии
``ctc`` (220M) и ``large_ctc`` (600M).

Наши особенности против собратьев (whisper/moss):

- модель принимает не больше 25 с за проход — длинный файл режется на чанки
  ≤ ``chunk_sec`` с поиском тихого места на границе. Штатный longform модели
  не используем: он тянет gated-модель ``pyannote/segmentation-3.0`` и
  HF-токен;
- окна живой расшифровки (Live/Jitsi) идут в энкодер тензором напрямую:
  без временных файлов и без ffmpeg;
- языковая подсказка не нужна: CTC-модель многоязычная, язык определяется
  самим текстом;
- подтверждение пауз от VAD отсутствует — сегменты файла равны чанкам, для
  слов используются кадры CTC (они же питают живой путь).
"""

from __future__ import annotations

import gc
import logging
from pathlib import Path
from typing import Any

import numpy as np

from ..domain.errors import JobCancelled
from ..domain.models import Segment
from ..metrics import note_model_loaded, note_model_unloaded, touch_engine
from ..model_budget import evict_idle, tracked
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

log = logging.getLogger(__name__)

SAMPLE_RATE = 16000
MODEL_LIMIT_SEC = 24.5  # жёсткий предел модели — 25 с на проход

WindowWords = list[tuple[float, float, str]]


def split_on_silence(
    wave: np.ndarray,
    rate: int,
    max_sec: float,
    *,
    search_sec: float = 3.0,
    floor_sec: float = 0.4,
) -> list[tuple[float, np.ndarray]]:
    """Нарезать аудио на куски ≤ ``max_sec``, по возможности — по тихому месту.

    Возвращает ``[(смещение_в_секундах, сэмплы), ...]``. Границу ищем в окне
    ``search_sec`` перед лимитом: режем в кадре (20 мс) с минимальной RMS,
    чтобы не разорвать слово; если запись не замолкает — режем по лимиту.
    """
    total = int(wave.size)
    if total == 0 or max_sec <= 0:
        return [(0.0, wave)]
    frame = max(int(0.02 * rate), 1)
    limit = max(int(max_sec * rate), frame)
    floor = max(int(floor_sec * rate), frame)
    out: list[tuple[float, np.ndarray]] = []
    pos = 0
    while pos < total:
        end = min(pos + limit, total)
        span = min(int(search_sec * rate), end - pos - floor)
        if end < total and span > frame:
            window = wave[end - span : end]
            count = window.size // frame
            if count > 1:
                frames = window[: count * frame].reshape(count, frame)
                rms = np.sqrt(np.mean(frames.astype(np.float64) ** 2, axis=1))
                quiet = int(np.argmin(rms))
                # Тихое место принимаем, только если оно заметно тише окна:
                # в ровном шуме/тоне «минимум» случаен, и резать по нему —
                # терять до search_sec аудио на каждом чанке.
                if rms[quiet] <= 0.25 * float(np.median(rms)):
                    candidate = end - span + (quiet + 1) * frame
                    if candidate - pos >= floor:
                        end = candidate
        out.append((pos / rate, wave[pos:end]))
        pos = end
    return out


class GigaAmEngine:
    """GigaAM Multilingual: файлы (чанками) и окна Live/Jitsi (напрямую)."""

    name = "gigaam"

    def __init__(
        self,
        model: str = "ai-sage/GigaAM-Multilingual",
        revision: str = "ctc",
        models_dir: Path | None = None,
        device: str = "cuda",
        chunk_sec: float = 22.0,
        batch_size: int = 8,
    ) -> None:
        self.model = model
        self.revision = revision
        self.models_dir = models_dir
        self.device = device
        self.chunk_sec = min(max(float(chunk_sec), 5.0), MODEL_LIMIT_SEC)
        self.batch_size = max(1, int(batch_size))
        self._loaded: Any = None

    # -- model lifecycle -------------------------------------------------------

    @property
    def model_ref(self) -> str:
        """Human id for the monitor/model budget (repo@revision)."""
        return f"{self.model}@{self.revision}"

    def resolve_model(self) -> str:
        """Prefer a local model directory over a Hub download."""
        if self.models_dir:
            candidates = [
                self.models_dir / "GigaAM-Multilingual",
                self.models_dir / "ai-sage" / "GigaAM-Multilingual",
            ]
            for candidate in candidates:
                if (candidate / "config.json").is_file() and (
                    candidate / "pytorch_model.bin"
                ).is_file():
                    return str(candidate)
        return self.model

    def _load(self) -> Any:
        if self._loaded is None:
            from transformers import AutoModel  # noqa: PLC0415 — тяжёлый импорт по требованию

            ref = self.resolve_model()
            evict_idle(exclude=self)
            kwargs: dict[str, Any] = {"trust_remote_code": True}
            if ref == self.model:
                kwargs["revision"] = self.revision
            log.info("loading GigaAM '%s' (%s) on %s", ref, self.revision, self.device)
            model = AutoModel.from_pretrained(ref, **kwargs)
            model.eval()
            if self.device:
                model.to(self.device)
            self._loaded = model
            note_model_loaded(self.name, self.model_ref)
            log.info("GigaAM '%s' loaded", self.model_ref)
        return self._loaded

    def unload(self) -> None:
        """Release the model and free its GPU memory (idle eviction)."""
        if self._loaded is None:
            return
        self._loaded = None
        gc.collect()
        note_model_unloaded(self.name, self.model_ref)
        log.info("GigaAM '%s' unloaded", self.model_ref)

    # -- inference -------------------------------------------------------------

    def _forward_decode(
        self, waves: list[np.ndarray], *, words: bool
    ) -> list[tuple[str, WindowWords]]:
        """One encoder pass over a padded batch; per-item (text, words).

        Идём в энкодер тензором и раскодируем CTC-кадры напрямую: это тот же
        путь, что у ``transcribe`` модели, но без файлов и без ffmpeg (и умеет
        батч — длинные файлы обрабатываются пачками чанков).
        """
        import torch  # noqa: PLC0415 — тяжёлый импорт по требованию

        model = self._load()
        inner = model.model
        param = next(inner.parameters())
        lengths = [max(int(wave.size), 1) for wave in waves]
        batch = np.zeros((len(waves), max(lengths)), dtype=np.float32)
        for row, wave in enumerate(waves):
            batch[row, : wave.size] = wave
        wav = torch.from_numpy(batch).to(param.device).to(param.dtype)
        wav_lens = torch.tensor(lengths, device=param.device)
        encoded, encoded_len = inner(wav, wav_lens)
        decoded = inner._decode(encoded, encoded_len, wav_lens, words)  # noqa: SLF001
        out: list[tuple[str, WindowWords]] = []
        for text, word_list in decoded:
            parsed: WindowWords = [
                (float(word.start), float(word.end), str(word.text))
                for word in (word_list or [])
            ]
            out.append((str(text).strip(), parsed))
        return out

    # -- files -----------------------------------------------------------------

    @tracked
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
        """Transcribe a prepared 16 kHz mono WAV in silence-aligned chunks."""
        import soundfile as sf  # noqa: PLC0415 — тяжёлый импорт по требованию

        data, rate = sf.read(str(audio_path), dtype="float32", always_2d=True)
        if rate != SAMPLE_RATE:
            raise ValueError(f"gigaam: ожидается WAV 16 кГц, получено {rate} Гц")
        wave = data.mean(axis=1) if data.shape[1] > 1 else data[:, 0]
        duration = float(wave.size) / SAMPLE_RATE
        if wave.size == 0:
            raise NoSpeechError("gigaam: пустое аудио")

        chunks = split_on_silence(wave, SAMPLE_RATE, self.chunk_sec)
        segments: list[Segment] = []
        for position in range(0, len(chunks), self.batch_size):
            if is_cancelled and is_cancelled():
                raise JobCancelled()
            if pause_gate is not None:
                pause_gate()
            batch = chunks[position : position + self.batch_size]
            results = self._forward_decode([samples for _start, samples in batch], words=False)
            touch_engine(self)
            for (start_sec, samples), (text, _words) in zip(batch, results, strict=True):
                end_sec = start_sec + samples.size / SAMPLE_RATE
                if text:
                    segment = Segment(
                        index=len(segments),
                        start=round(start_sec, 3),
                        end=round(end_sec, 3),
                        text=text,
                    )
                    segments.append(segment)
                    if on_segment is not None:
                        on_segment(segment)
                if on_progress is not None and duration > 0:
                    on_progress(
                        TranscribeProgress(
                            fraction=min(end_sec / duration, 1.0),
                            message=text[:80] or "…",
                        )
                    )
        if not segments:
            raise NoSpeechError("gigaam: речь не найдена")
        touch_engine(self)
        return AsrResult(
            language=options.language or "",
            duration=duration,
            segments=segments,
        )

    # -- live windows ----------------------------------------------------------

    @tracked
    def transcribe_window(
        self,
        audio: np.ndarray,
        *,
        language: str | None = None,
        beam_size: int = 1,
    ) -> WindowWords:
        """Transcribe one short float32 16 kHz window for the live mode.

        ``language``/``beam_size`` приняты для совместимости с контрактом
        окон (whisper): модель многоязычная, подсказки не требует, а декод
        CTC детерминированный.
        """
        if audio.size == 0:
            return []
        text, words = self._forward_decode([np.asarray(audio, dtype=np.float32)], words=True)[0]
        return words or _synth_words(text, float(audio.size) / SAMPLE_RATE)

    @tracked
    def transcribe_batch(
        self,
        windows: list[np.ndarray],
        *,
        language: str | None = None,
    ) -> list[WindowWords]:
        """Transcribe independent short windows in one encoder pass."""
        if not windows:
            return []
        prepared = [np.asarray(window, dtype=np.float32) for window in windows]
        results = self._forward_decode(
            [window if window.size else np.zeros(1, dtype=np.float32) for window in prepared],
            words=True,
        )
        out: list[WindowWords] = []
        for window, (text, words) in zip(windows, results, strict=True):
            if window.size == 0:
                out.append([])
                continue
            out.append(words or _synth_words(text, float(window.size) / SAMPLE_RATE))
        return out


def _synth_words(text: str, duration: float) -> WindowWords:
    """Last-resort word timing when the CTC frames told us nothing.

    Живой шлюз коммитит текст по словам; при пустых кадрах распределяем
    слова равномерно по окну — текст важнее микроточности времени.
    """
    tokens = [token for token in str(text or "").split() if token]
    if not tokens:
        return []
    step = duration / len(tokens) if duration > 0 else 0.0
    return [(index * step, (index + 1) * step, token) for index, token in enumerate(tokens)]
