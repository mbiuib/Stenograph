"""Cross-chunk speaker stitching: global diarization over MOSS segment audio.

MOSS labels speakers locally to each 300 s chunk, so on long recordings one
voice can be split across labels and several voices can share one. After all
chunks are decoded, the engine embeds every substantial segment with
ECAPA-TDNN (speechbrain, CPU) and clusters the embeddings with average-linkage
agglomerative clustering — each cluster becomes one global speaker.

The embedder is injectable: production uses :class:`EcapaEmbedder`, tests a
deterministic fake. Any failure (dependency or model missing, download
offline) degrades gracefully: the engine keeps MOSS's local labels.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

#: Косинусная близость эмбеддингов, при которой сегменты считаются одним голосом.
MERGE_SIM = 0.35
#: Кластер с речью короче — «осколок»: приклеивается к ближайшему похожему.
MIN_CLUSTER_SEC = 10.0
#: Минимальная близость для приклейки осколка (ниже — оставляем как есть).
FRAGMENT_FLOOR = 0.2
#: Сегменты короче — не кластеризуем (шумно); их метку берут у соседа по времени.
MIN_SEG_SEC = 0.8
#: Больше клипов — дороже; длинные сегменты информативнее коротких.
MAX_CLIPS = 400
#: Тишина тише этого RMS не тратится на эмбеддинг.
MIN_RMS = 0.002
EMBED_BATCH = 16
RATE = 16000

Embedder = Callable[[list[np.ndarray]], np.ndarray]


def agglomerative_labels(vectors: np.ndarray, threshold: float) -> list[int]:
    """Average-linkage clustering of L2-normalized vectors (cosine, numpy).

    Returns a cluster index per vector; cluster pairs closer than
    ``threshold`` cosine similarity are merged. Deterministic: the closest
    pair wins, ties by index order.
    """
    count = vectors.shape[0]
    if count == 0:
        return []
    if count == 1:
        return [0]
    distance = np.clip(1.0 - vectors @ vectors.T, 0.0, 2.0)
    np.fill_diagonal(distance, np.inf)
    active = np.ones(count, dtype=bool)
    sizes = np.ones(count, dtype=np.float64)
    labels = np.arange(count)
    cutoff = 1.0 - threshold
    while True:
        masked = np.where(active[:, None] & active[None, :], distance, np.inf)
        flat = int(np.argmin(masked))
        left, right = sorted(divmod(flat, count))
        if masked[left, right] > cutoff:
            break
        total = sizes[left] + sizes[right]
        merged = (
            distance[left, :] * sizes[left] + distance[right, :] * sizes[right]
        ) / total
        distance[left, :] = merged
        distance[:, left] = merged
        np.fill_diagonal(distance, np.inf)
        sizes[left] = total
        labels[labels == labels[right]] = labels[left]
        active[right] = False
    return [int(label) for label in labels]


def stabilize_labels(
    vectors: np.ndarray,
    labels: list[int],
    seconds: list[float],
    *,
    min_sec: float = MIN_CLUSTER_SEC,
    floor: float = FRAGMENT_FLOOR,
) -> list[int]:
    """Приклеивает «осколки» (кластеры с речью короче ``min_sec``) к похожим.

    Короткие сегменты дают шумные эмбеддинги и легко откалываются в кластеры
    из одного-двух слов; лечится слиянием с ближайшим кластером, если
    близость не ниже ``floor`` (иначе осколок остаётся своим — лучше, чем
    склеить два разных голоса). Детерминированно: первым берётся самый
    маленький осколок.
    """
    labels = list(labels)
    frozen: set[int] = set()
    while True:
        groups: dict[int, list[int]] = {}
        for index, label in enumerate(labels):
            groups.setdefault(label, []).append(index)
        if len(groups) <= 1:
            break
        fragments = sorted(
            (
                (sum(seconds[index] for index in members), label)
                for label, members in groups.items()
                if sum(seconds[index] for index in members) < min_sec
                and label not in frozen
            )
        )
        if not fragments:
            break
        _, victim = fragments[0]
        victim_members = groups[victim]
        victim_centroid = vectors[victim_members].mean(axis=0)
        victim_centroid = victim_centroid / (np.linalg.norm(victim_centroid) + 1e-9)
        best_label: int | None = None
        best_similarity = -2.0
        for label, members in groups.items():
            if label == victim:
                continue
            centroid = vectors[members].mean(axis=0)
            centroid = centroid / (np.linalg.norm(centroid) + 1e-9)
            similarity = float(victim_centroid @ centroid)
            if similarity > best_similarity:
                best_similarity = similarity
                best_label = label
        if best_label is not None and best_similarity >= floor:
            for index in victim_members:
                labels[index] = best_label
        else:
            frozen.add(victim)
    return labels


class EcapaEmbedder:
    """ECAPA-TDNN speaker embeddings via speechbrain (lazy, CPU by default)."""

    def __init__(self, models_dir: Path | None = None, device: str = "cpu") -> None:
        self.models_dir = models_dir
        self.device = device
        self._model: object | None = None

    def _load(self) -> object:
        if self._model is not None:
            return self._model
        from speechbrain.inference.speaker import SpeakerRecognition  # type: ignore[import-untyped]
        from speechbrain.utils.fetching import LocalStrategy  # type: ignore[import-untyped]

        savedir = None
        if self.models_dir is not None:
            savedir = str(self.models_dir / "speechbrain" / "spkrec-ecapa-voxceleb")
        self._model = SpeakerRecognition.from_hparams(
            source="speechbrain/spkrec-ecapa-voxceleb",
            savedir=savedir,
            run_opts={"device": self.device},
            local_strategy=LocalStrategy.COPY,
        )
        return self._model

    def __call__(self, clips: list[np.ndarray]) -> np.ndarray:
        """Embed clips (float32 mono 16 kHz) → ``[n, 192]`` vectors."""
        import torch

        model = self._load()
        order = sorted(range(len(clips)), key=lambda index: clips[index].size)
        vectors: list[tuple[int, np.ndarray]] = []
        with torch.no_grad():
            for offset in range(0, len(order), EMBED_BATCH):
                batch = [clips[index] for index in order[offset : offset + EMBED_BATCH]]
                width = max(clip.size for clip in batch)
                padded = torch.stack(
                    [
                        torch.nn.functional.pad(
                            torch.from_numpy(clip), (0, width - clip.size)
                        )
                        for clip in batch
                    ]
                )
                lengths = torch.tensor([clip.size / width for clip in batch])
                output = model.encode_batch(  # type: ignore[attr-defined]
                    padded, wav_lens=lengths
                )
                chunk = output.squeeze(1).cpu().numpy()
                for local, index in enumerate(order[offset : offset + EMBED_BATCH]):
                    vectors.append((index, chunk[local].copy()))
        vectors.sort(key=lambda item: item[0])
        return np.stack([vector for _, vector in vectors])


class SpeakerStitcher:
    """Collects chunk segments and resolves global speaker labels for them.

    Usage: ``add_chunk`` per decoded chunk (with that chunk's PCM), then
    ``resolve`` once — it returns ``{(chunk, seq): "SPEAKER_xx"}`` for every
    added segment, or ``None`` when stitching is impossible (then the engine
    keeps MOSS's local labels).
    """

    def __init__(
        self,
        embedder: Embedder | None = None,
        *,
        merge_sim: float = MERGE_SIM,
        models_dir: Path | None = None,
        device: str = "cpu",
    ) -> None:
        self._embedder = embedder
        self._merge_sim = merge_sim
        self._models_dir = models_dir
        self._device = device
        self._clips: dict[tuple[int, int], np.ndarray] = {}
        self._starts: dict[tuple[int, int], float] = {}
        self._ends: dict[tuple[int, int], float] = {}

    def add_chunk(
        self, index: int, segments: list[tuple[float, float]], pcm: np.ndarray
    ) -> None:
        """Register one chunk's segments; ``pcm`` is float32 [-1..1] at 16 kHz."""
        for seq, (start, end) in enumerate(segments):
            self._starts[(index, seq)] = float(start)
            self._ends[(index, seq)] = float(end)
            if end - start < MIN_SEG_SEC:
                continue
            a = max(0, int(start * RATE))
            b = min(pcm.size, int(end * RATE))
            if b - a < int(MIN_SEG_SEC * RATE):
                continue
            clip = pcm[a:b].astype(np.float32, copy=True)
            if float(np.sqrt(np.mean(np.square(clip, dtype=np.float64)))) < MIN_RMS:
                continue
            self._clips[(index, seq)] = clip

    def resolve(self) -> dict[tuple[int, int], str] | None:
        """Embed and cluster the collected segments into global speakers.

        Returns a label for EVERY registered segment (short ones inherit the
        nearest resolved neighbour), or ``None`` when embedding is not
        possible — the caller then keeps MOSS's local labels.
        """
        if not self._starts:
            return None
        candidates = sorted(self._clips.items(), key=lambda item: -item[1].size)[
            :MAX_CLIPS
        ]
        if len(candidates) < 2:
            return None
        keys = [key for key, _ in candidates]
        clips = [clip for _, clip in candidates]
        embed = self._embedder
        if embed is None:
            embed = EcapaEmbedder(models_dir=self._models_dir, device=self._device)
        try:
            vectors = np.asarray(embed(clips), dtype=np.float64)
        except Exception as exc:  # noqa: BLE001 — стяжка не должна ронять транскрибацию
            log.warning("MOSS: склейка спикеров недоступна (%s) — метки чанков остаются", exc)
            return None
        vectors /= np.linalg.norm(vectors, axis=1, keepdims=True) + 1e-9
        labels = agglomerative_labels(vectors, self._merge_sim)
        labels = stabilize_labels(
            vectors, labels, [clip.size / RATE for clip in clips]
        )
        assigned: dict[tuple[int, int], int] = dict(zip(keys, labels, strict=True))

        # Короткие/тихие сегменты наследуют метку ближайшего по времени
        # (расстояние до интервала решённого сегмента, а не до его начала).
        intervals = sorted(
            (self._starts[key], self._ends[key], label)
            for key, label in assigned.items()
        )
        for key in self._starts:
            if key in assigned:
                continue
            start = self._starts[key]
            end = self._ends[key]
            best_label = 0
            best_distance = float("inf")
            for seg_start, seg_end, label in intervals:
                if seg_end < start:
                    distance = start - seg_end
                elif seg_start > end:
                    distance = seg_start - end
                else:
                    distance = 0.0
                if distance < best_distance:
                    best_distance = distance
                    best_label = label
            assigned[key] = best_label

        # Глобальные метки — по порядку первого появления в записи.
        order: dict[int, int] = {}
        result: dict[tuple[int, int], str] = {}
        for key in sorted(assigned, key=lambda item: (self._starts[item], item)):
            label = assigned[key]
            if label not in order:
                order[label] = len(order)
            result[key] = f"SPEAKER_{order[label]:02d}"
        return result
