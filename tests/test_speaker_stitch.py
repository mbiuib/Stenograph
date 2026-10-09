"""Unit and engine-level tests for cross-chunk speaker stitching.

Everything is deterministic: synthetic voices are sine tones (frequency = the
«voice»), the embedder is a fake that maps the tone frequency to a vector,
and the engine-level test fakes the MOSS model itself.
"""

from __future__ import annotations

import math
from functools import partial

import numpy as np
import pytest

from stenograph.engines import speaker_stitch
from stenograph.engines.speaker_stitch import (
    SpeakerStitcher,
    agglomerative_labels,
    stabilize_labels,
)

RATE = 16000
FREQ_A = 220.0
FREQ_B = 320.0
FREQ_C = 420.0


def voice(seconds: float, freq: float, amplitude: float = 0.2) -> np.ndarray:
    """Deterministic «voice»: a tone whose frequency is the speaker identity."""
    samples = int(seconds * RATE)
    return np.array(
        [amplitude * math.sin(2 * math.pi * freq * index / RATE) for index in range(samples)],
        dtype=np.float32,
    )


def layout(chunk_seconds: float, segments: list[tuple[float, float, float]]) -> np.ndarray:
    """Chunk PCM with voices placed at (start, end, freq)."""
    pcm = np.zeros(int(chunk_seconds * RATE), dtype=np.float32)
    for start, end, freq in segments:
        piece = voice(end - start, freq)
        pcm[int(start * RATE) : int(start * RATE) + piece.size] = piece
    return pcm


def freq_embedder(clips: list[np.ndarray]) -> np.ndarray:
    """Fake embedder: dominant-tone frequency → a unit vector (voice space)."""
    angles = {FREQ_A: 0.0, FREQ_B: math.pi / 2, FREQ_C: math.pi}
    vectors = []
    for clip in clips:
        window = clip[: min(clip.size, 8192)].astype(np.float64) * np.hanning(
            min(clip.size, 8192)
        )
        spectrum = np.abs(np.fft.rfft(window))
        freq = float(np.fft.rfftfreq(window.size, 1 / RATE)[int(np.argmax(spectrum))])
        nearest = min(angles, key=lambda known: abs(known - freq))
        angle = angles[nearest]
        vectors.append([math.cos(angle), math.sin(angle)])
    return np.array(vectors)


def test_stitch_merges_one_voice_across_chunks() -> None:
    """Один голос в чанках 0 и 2 — один спикер; голос из чанка 1 — другой.

    Фальсификация: с merge_sim=2.0 (слияние выключено) метки чанков
    остаются локальными и равенство меток чанка 0 и 2 не выполняется.
    """
    stitcher = SpeakerStitcher(embedder=freq_embedder)
    stitcher.add_chunk(
        0,
        [(0.5, 2.0), (3.0, 5.0)],
        layout(8.0, [(0.5, 2.0, FREQ_A), (3.0, 5.0, FREQ_B)]),
    )
    stitcher.add_chunk(1, [(1.0, 3.0)], layout(5.0, [(1.0, 3.0, FREQ_B)]))
    stitcher.add_chunk(2, [(0.4, 2.6)], layout(4.0, [(0.4, 2.6, FREQ_A)]))

    mapping = stitcher.resolve()
    assert mapping is not None
    assert mapping[(0, 0)] == mapping[(2, 0)]  # голос A в разных чанках — один спикер
    assert mapping[(0, 1)] == mapping[(1, 0)]  # голос B — тоже
    assert mapping[(0, 0)] != mapping[(0, 1)]


def test_stitch_keeps_different_voices_apart() -> None:
    """Разные голоса не сливаются, даже если говорят в одном отрезке."""
    stitcher = SpeakerStitcher(embedder=freq_embedder)
    stitcher.add_chunk(
        0,
        [(0.5, 2.0), (2.5, 4.0)],
        layout(6.0, [(0.5, 2.0, FREQ_A), (2.5, 4.0, FREQ_C)]),
    )

    mapping = stitcher.resolve()
    assert mapping is not None
    assert mapping[(0, 0)] != mapping[(0, 1)]


def test_agglomerative_average_linkage_does_not_chain() -> None:
    """Average-linkage: A(0°)≈M(50°), M≈B(105°), но B не прилипает к A.

    Попарно соседи близки (как в цепочке), но после слияния A и M средняя
    дистанция до B слишком велика — single-linkage бы всё стянул в один.
    """
    angles = np.deg2rad([0.0, 50.0, 105.0])
    vectors = np.stack([[np.cos(a), np.sin(a)] for a in angles])
    labels = agglomerative_labels(vectors, threshold=0.5)
    assert labels[0] == labels[1]
    assert labels[2] not in (labels[0], labels[1])


def test_agglomerative_renumbers_by_input_order() -> None:
    """Кластеры нумеруются по первому появлению вектора в порядке массива."""
    far = np.array([1.0, 0.0])
    near = np.array([0.99, 0.1])
    other = np.array([-1.0, 0.0])
    vectors = np.stack([other, far, near])
    labels = agglomerative_labels(vectors, threshold=0.5)
    assert labels[0] != labels[1]
    assert labels[1] == labels[2]
    assert labels[1] == 1  # не 0: первым появился другой кластер


def test_stabilizer_glues_a_fragment_to_the_similar_cluster() -> None:
    """Осколок из одного слова прилипает к похожему кластеру (не плодит спикера).

    Основной голос — 12 векторов у 0°; осколок — 75° (cos≈0.26 к основному:
    ниже порога кластеризации 0.35, но выше пола приклейки 0.2); третий
    голос — 160°. После стабилизации остаётся 2 спикера.
    """
    base = np.deg2rad([0, 2, -2, 3, -3, 1, -1, 4, -4, 2, -2, 0])
    main = np.stack([[np.cos(a), np.sin(a)] for a in base])
    fragment = np.array([[np.cos(np.deg2rad(75)), np.sin(np.deg2rad(75))]])
    other = np.array([[np.cos(np.deg2rad(160)), np.sin(np.deg2rad(160))]])
    vectors = np.vstack([main, fragment, other])
    seconds = [5.0] * len(main) + [2.0, 30.0]

    labels = agglomerative_labels(vectors, 0.35)
    assert len(set(labels)) == 3  # до стабилизации осколок — отдельный список
    stable = stabilize_labels(vectors, labels, seconds)
    assert len(set(stable)) == 2
    assert stable[12] == stable[0]  # осколок с основным голосом


def test_stabilizer_keeps_an_unrelated_fragment_alone() -> None:
    """Осколок, не похожий ни на кого (ниже пола), остаётся своим кластером."""
    main = np.stack([[np.cos(a), np.sin(a)] for a in np.deg2rad([0, 2, -2])])
    fragment = np.array([[np.cos(np.deg2rad(120)), np.sin(np.deg2rad(120))]])
    vectors = np.vstack([main, fragment])
    seconds = [5.0, 5.0, 5.0, 2.0]
    labels = agglomerative_labels(vectors, 0.35)
    stable = stabilize_labels(vectors, labels, seconds)
    assert len(set(stable)) == 2
    assert stable[3] != stable[0]


def test_short_segments_inherit_the_nearest_label() -> None:
    """Короткий (<MIN) сегмент получает метку ближайшего по времени длинного."""
    stitcher = SpeakerStitcher(embedder=freq_embedder)
    stitcher.add_chunk(
        0,
        [(0.0, 0.3), (0.5, 2.5), (2.6, 2.9), (3.5, 6.0)],
        layout(8.0, [(0.5, 2.5, FREQ_A), (3.5, 6.0, FREQ_B)]),
    )
    mapping = stitcher.resolve()
    assert mapping is not None
    assert mapping[(0, 0)] == mapping[(0, 1)]  # до A
    assert mapping[(0, 2)] == mapping[(0, 1)]  # сразу после A — ближе A, чем B
    assert mapping[(0, 3)] != mapping[(0, 1)]  # длинный B — другой спикер


def test_silent_segments_are_not_clustered_but_still_labelled() -> None:
    """Тихий сегмент не тратит эмбеддинг, но метку соседа получает."""
    stitcher = SpeakerStitcher(embedder=freq_embedder)
    pcm = layout(
        8.0,
        [(0.5, 2.5, FREQ_A), (4.5, 6.5, FREQ_B)],
    )
    pcm[int(2.8 * RATE) : int(4.0 * RATE)] = 0.0001  # почти тишина
    stitcher.add_chunk(0, [(0.5, 2.5), (2.8, 4.0), (4.5, 6.5)], pcm)
    assert (0, 1) not in stitcher._clips
    mapping = stitcher.resolve()
    assert mapping is not None
    assert mapping[(0, 1)] == mapping[(0, 0)]  # тишина ближе к A
    assert mapping[(0, 2)] != mapping[(0, 0)]


def test_embedder_failure_returns_none() -> None:
    """Недоступный эмбеддер — не исключение, а None (метки чанков остаются)."""

    def broken(clips: list[np.ndarray]) -> np.ndarray:
        raise RuntimeError("модель недоступна")

    stitcher = SpeakerStitcher(embedder=broken)
    stitcher.add_chunk(
        0,
        [(0.5, 2.0), (3.0, 5.0)],
        layout(6.0, [(0.5, 2.0, FREQ_A), (3.0, 5.0, FREQ_B)]),
    )
    assert stitcher.resolve() is None


def test_clip_cap_keeps_the_longest() -> None:
    """При превышении MAX_CLIPS остаются самые длинные клипы."""
    stitcher = SpeakerStitcher(embedder=freq_embedder)
    segments = [(0.0, 1.0 + index * 0.5) for index in range(4)]
    freq = [FREQ_A, FREQ_B, FREQ_A, FREQ_B]
    pcm = layout(12.0, [(s, e, f) for (s, e), f in zip(segments, freq, strict=True)])
    stitcher.add_chunk(0, segments, pcm)
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(speaker_stitch, "MAX_CLIPS", 2)
    try:
        assert len(stitcher._clips) == 4
        mapping = stitcher.resolve()
    finally:
        monkeypatch.undo()
    assert mapping is not None
    # Все сегменты получили метку; самые короткие — от соседей.
    assert set(mapping) == {(0, 0), (0, 1), (0, 2), (0, 3)}


def test_engine_stitches_labels_across_chunks(monkeypatch, tmp_path) -> None:
    """Сквозной тест движка: локальные метки чанков становятся глобальными.

    Чанк 0: голос A (MOSS пометил S02) и голос B (S01). Чанк 1: голос A
    снова (S01). После склейки A из чанка 0 и «S01» из чанка 1 — один
    спикер, хотя локально они были в разных метках.
    """
    import wave as wave_module

    import moss_transcribe_diarize
    import moss_transcribe_diarize.inference_utils as moss_utils

    from stenograph.engines import moss as moss_module
    from stenograph.engines.base import TranscribeOptions

    # Два чанка по 4 секунды с известными голосами.
    chunks_dir = tmp_path / "chunks"
    chunks_dir.mkdir()
    chunk0 = chunks_dir / "chunk0.wav"
    chunk1 = chunks_dir / "chunk1.wav"
    for path, audio in (
        (chunk0, layout(4.0, [(0.4, 2.0, FREQ_A), (2.2, 3.8, FREQ_B)])),
        (chunk1, layout(4.0, [(0.5, 3.0, FREQ_A)])),
    ):
        with wave_module.open(str(path), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(RATE)
            handle.writeframes((np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes())

    class Parsed:
        def __init__(self, start: float, end: float, text: str, speaker: str) -> None:
            self.start, self.end, self.text, self.speaker = start, end, text, speaker

    scripts = {
        "chunk0": [Parsed(0.4, 2.0, "раз", "S02"), Parsed(2.2, 3.8, "два", "S01")],
        "chunk1": [Parsed(0.5, 3.0, "три", "S01")],
    }
    order = ["chunk0", "chunk1"]

    def fake_parse(text: str) -> list[Parsed]:
        return scripts[text]

    def fake_build(audio_path, prompt=""):  # noqa: ANN001, ANN202 — подмена пакета
        return {"audio": str(audio_path)}

    def fake_generate(model, processor, messages, **kwargs):  # noqa: ANN001, ANN202
        return {"text": order.pop(0)}

    class _Params:
        device = "cpu"
        dtype = "float32"

    class _Model:
        def parameters(self):  # noqa: ANN201 — как у настоящей модели, итератор
            return iter([_Params()])

    class _Cuda:
        @staticmethod
        def is_available() -> bool:
            return False

    class _Torch:
        cuda = _Cuda()

    monkeypatch.setattr(
        moss_module.MossEngine, "_load", lambda self: (_Torch(), _Model(), object())
    )
    monkeypatch.setattr(
        moss_module.MossEngine, "_probe_duration", lambda self, path: 700.0
    )
    monkeypatch.setattr(
        moss_module.MossEngine,
        "_split_chunks",
        lambda self, path, plan: ([chunk0, chunk1], chunks_dir),
    )
    monkeypatch.setattr(moss_transcribe_diarize, "parse_transcript", fake_parse)
    monkeypatch.setattr(moss_utils, "build_transcription_messages", fake_build)
    monkeypatch.setattr(moss_utils, "generate_transcription", fake_generate)
    monkeypatch.setattr(
        moss_module,
        "SpeakerStitcher",
        partial(SpeakerStitcher, embedder=freq_embedder),
    )
    monkeypatch.setattr(moss_module, "plan_chunks", lambda *args: [(0.0, 302.0), (298.0, 402.0)])

    engine = moss_module.MossEngine(models_dir=None)
    result = engine.transcribe(chunk0, TranscribeOptions())

    labels = [segment.speaker for segment in result.segments]
    assert len(labels) == 3
    # Голос A из чанка 0 и голос A из чанка 1 — один глобальный спикер.
    assert labels[0] == labels[2]
    # Голос B — отдельный.
    assert labels[1] != labels[0]
    # Метки — глобальная нумерация SPEAKER_xx без утечек служебных ключей.
    assert all(label and label.startswith("SPEAKER_") for label in labels)
