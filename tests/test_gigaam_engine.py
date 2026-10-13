"""GigaAM (ai-sage/GigaAM-Multilingual): нарезка файлов, окна Live/Jitsi, алиасы.

Модель не грузим: `_load` подменяется фейковым HF-модулем с тензорной
заглушкой энкодера и предсказуемым CTC-декодом — проверяем логику движка
(чанки, смещения времени, отмена, батч, слова), а не саму модель.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
import soundfile as sf
import torch

from stenograph.asr_service import advertised_engines, build_default_engine
from stenograph.config import Settings
from stenograph.domain.errors import JobCancelled
from stenograph.engines import available_asr
from stenograph.engines import gigaam as gigaam_module
from stenograph.engines.base import NoSpeechError, TranscribeOptions
from stenograph.engines.gigaam import GigaAmEngine, _synth_words, split_on_silence
from stenograph.live import manager as live_manager

RATE = 16000


class _FakeWord:
    """Подделка gigaam.types.Word (text/start/end)."""

    def __init__(self, text: str, start: float, end: float) -> None:
        self.text = text
        self.start = start
        self.end = end


class _FakeInner(torch.nn.Module):
    """Заглушка GigaAMASR: forward пропускает звук, _decode даёт готовый текст."""

    def __init__(self) -> None:
        super().__init__()
        self._p = torch.nn.Parameter(torch.zeros(1))
        self.batch_rows: list[int] = []

    def forward(self, wav: torch.Tensor, lengths: torch.Tensor):  # noqa: ANN201
        self.batch_rows.append(int(wav.shape[0]))
        return wav, lengths

    def _decode(  # noqa: ANN001, ANN202
        self, encoded, encoded_len, wav_lens, word_timestamps=False
    ):
        out = []
        for n in wav_lens.tolist():
            duration = int(n) / RATE
            words = [
                _FakeWord("привет", 0.0, duration / 2),
                _FakeWord("мир", duration / 2, duration),
            ]
            out.append(("привет мир", words if word_timestamps else []))
        return out


class _FakeModel:
    """Подделка GigaAMModel: движку нужен только внутренний модуль .model."""

    def __init__(self) -> None:
        self.model = _FakeInner()


def _engine(monkeypatch, tmp_path, **overrides) -> GigaAmEngine:  # noqa: ANN001
    fake = _FakeModel()
    monkeypatch.setattr(GigaAmEngine, "_load", lambda self: fake)
    params: dict = dict(models_dir=tmp_path, device="cpu", chunk_sec=10.0)
    params.update(overrides)
    return GigaAmEngine(**params)


def _write_wav(tmp_path, name: str, samples: np.ndarray) -> str:  # noqa: ANN001
    path = tmp_path / name
    sf.write(str(path), samples, RATE)
    return str(path)


def _tone(seconds: float, amplitude: float = 0.4) -> np.ndarray:
    t = np.arange(int(RATE * seconds), dtype=np.float32) / RATE
    return (amplitude * np.sin(2 * np.pi * 220.0 * t)).astype(np.float32)


# -- нарезка длинного аудио ---------------------------------------------------


def test_split_uses_the_quiet_gap_for_the_boundary() -> None:
    """Граница чанка ложится в паузу, а не рвёт слово на лимите."""
    wave = np.concatenate([_tone(10.0), np.zeros(2 * RATE, np.float32), _tone(10.0)])
    chunks = split_on_silence(wave, RATE, max_sec=12.0)
    assert len(chunks) == 2
    second_start = chunks[1][0]
    assert 10.0 <= second_start <= 11.9  # внутри паузы [10, 12]
    assert chunks[0][1].size + chunks[1][1].size == wave.size


def test_split_without_silence_cuts_at_the_limit() -> None:
    """Непрерывная запись режется ровно по лимиту (модель не примет больше)."""
    chunks = split_on_silence(_tone(25.0), RATE, max_sec=10.0)
    assert [round(start, 3) for start, _ in chunks] == [0.0, 10.0, 20.0]
    assert chunks[-1][1].size == 5 * RATE


# -- файловая транскрибация ---------------------------------------------------


def test_file_is_chunked_with_monotonic_offsets(tmp_path, monkeypatch) -> None:
    """Смещения сегментов идут от начала файла; прогресс доходит до конца."""
    engine = _engine(monkeypatch, tmp_path)
    path = _write_wav(tmp_path, "meeting.wav", _tone(25.0))
    seen_segments = []
    progress = []
    result = engine.transcribe(
        Path(path),
        TranscribeOptions(),
        on_segment=seen_segments.append,
        on_progress=progress.append,
    )
    assert [s.text for s in result.segments] == ["привет мир"] * 3
    assert [round(s.start, 3) for s in result.segments] == [0.0, 10.0, 20.0]
    assert [round(s.end, 3) for s in result.segments] == [10.0, 20.0, 25.0]
    assert result.duration == 25.0
    assert len(seen_segments) == 3
    assert progress[-1].fraction == pytest.approx(1.0)
    # энкодер прошёл пачками, а не по чанку на проход
    inner = engine._load().model
    assert inner.batch_rows == [3]


def test_file_cancellation_raises(tmp_path, monkeypatch) -> None:
    """Отменённый прогон падает JobCancelled до инференса."""
    engine = _engine(monkeypatch, tmp_path)
    path = _write_wav(tmp_path, "meeting.wav", _tone(5.0))
    with pytest.raises(JobCancelled):
        engine.transcribe(
            Path(path),
            TranscribeOptions(),
            is_cancelled=lambda: True,
        )


def test_empty_audio_is_no_speech(tmp_path, monkeypatch) -> None:
    """Пустой файл честно отвечает NoSpeechError."""
    engine = _engine(monkeypatch, tmp_path)
    path = _write_wav(tmp_path, "empty.wav", np.zeros(0, np.float32))
    with pytest.raises(NoSpeechError):
        engine.transcribe(Path(path), TranscribeOptions())


# -- окна Live/Jitsi ----------------------------------------------------------


def test_window_returns_ctc_word_tuples(tmp_path, monkeypatch) -> None:
    """Окно декодируется в слова (start, end, text) от начала окна."""
    engine = _engine(monkeypatch, tmp_path)
    words = engine.transcribe_window(np.zeros(RATE, np.float32))
    assert words == [(0.0, 0.5, "привет"), (0.5, 1.0, "мир")]


def test_batch_windows_share_one_encoder_pass(tmp_path, monkeypatch) -> None:
    """Пачка окон уходит в энкодер одним проходом; слова — на каждое окно."""
    engine = _engine(monkeypatch, tmp_path)
    windows = [np.zeros(RATE, np.float32), np.zeros(2 * RATE, np.float32)]
    out = engine.transcribe_batch(windows)
    assert out[0][-1][1] == pytest.approx(1.0)
    assert out[1][-1][1] == pytest.approx(2.0)
    assert engine._load().model.batch_rows == [2]


def test_live_paths_touch_metrics(tmp_path, monkeypatch) -> None:
    """Окна и батч обновляют «последнее использование» — иначе Монитор врёт."""
    engine = _engine(monkeypatch, tmp_path)
    calls: list[object] = []
    monkeypatch.setattr(gigaam_module, "touch_engine", calls.append)
    engine.transcribe_window(np.zeros(RATE, np.float32))
    engine.transcribe_batch([np.zeros(RATE, np.float32), np.zeros(2 * RATE, np.float32)])
    assert calls == [engine, engine]


def test_synth_words_spreads_evenly() -> None:
    """Запасная раскладка слов по окну, когда CTC-кадры пусты."""
    assert _synth_words("а б в", 3.0) == [(0.0, 1.0, "а"), (1.0, 2.0, "б"), (2.0, 3.0, "в")]
    assert _synth_words("", 1.0) == []


# -- реестр, сервис и live ----------------------------------------------------


def test_registry_and_service_aliases(tmp_path) -> None:
    """Движок виден реестру; сервис отвечает и на алиас gigaam-live."""
    assert "gigaam" in available_asr()
    settings = Settings(data_dir=tmp_path)
    assert {"gigaam", "gigaam-live"} <= set(advertised_engines())
    assert isinstance(build_default_engine("gigaam-live", settings), GigaAmEngine)
    assert isinstance(build_default_engine("gigaam", settings), GigaAmEngine)


def test_live_bundle_uses_gigaam_windows(tmp_path, monkeypatch) -> None:
    """Настройка live_engine=gigaam даёт окна через GigaAM (локальный режим)."""
    live_manager._ENGINE_FACTORIES.clear()
    fake = _FakeModel()
    monkeypatch.setattr(GigaAmEngine, "_load", lambda self: fake)
    settings = Settings(data_dir=tmp_path)
    settings.live_engine = "gigaam"
    factory, batch_factory = live_manager._engine_bundle(settings)
    words = factory("ru")(np.zeros(RATE, np.float32))
    assert [(round(s, 3), round(e, 3), t) for s, e, t in words] == [
        (0.0, 0.5, "привет"),
        (0.5, 1.0, "мир"),
    ]
    batched = batch_factory("ru")([np.zeros(RATE, np.float32)])
    assert batched[0][0][2] == "привет"
    assert fake.model.batch_rows == [1, 1]
    # Live-сессии и мост Jitsi делят одну модель: повторный запрос — тот же бандл.
    again_factory, _ = live_manager._engine_bundle(settings)
    assert again_factory is factory
    assert live_manager._engine_bundle(settings)[1] is batch_factory


def test_remote_bundle_picks_the_gigaam_alias(tmp_path, monkeypatch) -> None:
    """Remote-режим просит у сервиса gigaam-live, когда включён GigaAM."""
    seen: list[str] = []

    class StubClient:
        def transcribe_windows(self, windows, *, model, language=None, timeout=None):  # noqa: ANN001, ANN202
            seen.append(model)
            return [[(0.0, 1.0, "ок")] for _ in windows]

    import stenograph.engines.remote as remote_module

    monkeypatch.setattr(
        remote_module, "remote_client_for", lambda settings: StubClient()
    )
    settings = Settings(data_dir=tmp_path)
    settings.asr_backend = "remote"
    settings.live_engine = "gigaam"
    factory, batch_factory = live_manager._engine_bundle(settings)
    factory("ru")(np.zeros(RATE, np.float32))
    batch_factory("ru")([np.zeros(RATE, np.float32)])
    assert seen == ["gigaam-live", "gigaam-live"]

    settings.live_engine = "whisper"
    whisper_factory, _ = live_manager._engine_bundle(settings)
    whisper_factory("ru")(np.zeros(RATE, np.float32))
    assert seen[-1] == "whisper-live"
