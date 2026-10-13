"""Пул сервиса: алиасы, копии по требованию, VRAM-гейт."""

from __future__ import annotations

import time
from typing import cast

from stenograph import asr_service, metrics
from stenograph.asr_service import EnginePool
from stenograph.engines.base import AsrEngine


class Stub:
    """Движок-заглушка: имя, модель и счётчик фоновой прогрузки."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.model_ref = f"{name}-model"
        self.loaded = 0

    def _load(self) -> None:
        """Считает, что копию прогрели именно фоновым ростом."""
        self.loaded += 1


def _recording_build(built: list[Stub]):  # noqa: ANN202 — тестовая фабрика
    def build(name: str) -> Stub:
        stub = Stub(name)
        built.append(stub)
        return stub

    return build


def test_service_pool_shares_one_engine_for_gigaam_aliases() -> None:
    """gigaam-live не плодит вторую копию модели в пуле сервиса."""
    built: list[Stub] = []
    pool = EnginePool(_recording_build(built))  # type: ignore[arg-type]
    first, lock_one = pool.get("gigaam-live")
    second, lock_two = pool.get("gigaam")
    assert first is second
    assert lock_one is lock_two
    assert [stub.name for stub in built] == ["gigaam"]


def test_pool_grows_and_warms_a_replica_when_every_copy_is_busy(monkeypatch) -> None:
    """Все копии заняты → сервис фоново поднимает и греет ещё одну."""
    monkeypatch.setattr(metrics, "model_vram_mb", lambda name, model: 900)
    built: list[Stub] = []
    pool = EnginePool(
        _recording_build(built),  # type: ignore[arg-type]
        copies=lambda name: 2 if name == "gigaam" else 1,
        can_grow=lambda name, engine: True,
    )
    engine1, lock1 = pool.get("gigaam")
    assert cast(Stub, engine1).loaded == 0  # пул строит лениво — греть здесь не должен
    lock1.acquire()
    try:
        again, _ = pool.get("gigaam")  # конкуренция: первая в очереди + рост
        assert again is engine1
        deadline = time.monotonic() + 5.0
        grown = engine1
        while time.monotonic() < deadline and grown is engine1:
            grown, _ = pool.get("gigaam")
            time.sleep(0.01)
        assert grown is not engine1  # вторая копия уже в пуле
        assert cast(Stub, grown).loaded == 1  # и она прогрета в фоне, а не первым запросом
    finally:
        lock1.release()


def test_pool_does_not_grow_past_the_copy_limit(monkeypatch) -> None:
    """copies=1 — конкурентный запрос терпеливо ждёт, копий не появляется."""
    monkeypatch.setattr(metrics, "model_vram_mb", lambda name, model: 900)
    built: list[Stub] = []
    pool = EnginePool(_recording_build(built))  # type: ignore[arg-type]
    _engine, lock = pool.get("gigaam")
    lock.acquire()
    try:
        pool.get("gigaam")
        time.sleep(0.1)
        assert len(built) == 1
    finally:
        lock.release()


def test_pool_growth_respects_the_vram_gate(monkeypatch) -> None:
    """can_grow=False (места в VRAM нет) — копия не строится."""
    monkeypatch.setattr(metrics, "model_vram_mb", lambda name, model: 900)
    monkeypatch.setattr(metrics, "gpu_info", lambda: {"vram_free_mb": 10})
    built: list[Stub] = []
    pool = EnginePool(
        _recording_build(built),  # type: ignore[arg-type]
        copies=lambda name: 3,
        can_grow=lambda name, engine: False,
    )
    _engine, lock = pool.get("gigaam")
    lock.acquire()
    try:
        pool.get("gigaam")
        time.sleep(0.1)
        assert len(built) == 1
    finally:
        lock.release()


def test_pool_reports_copies_and_limits_for_the_monitor(monkeypatch) -> None:
    """Монитор видит, сколько копий поднято и до какого потолка они растут."""
    monkeypatch.setattr(metrics, "model_vram_mb", lambda name, model: 900)
    built: list[Stub] = []
    pool = EnginePool(
        _recording_build(built),  # type: ignore[arg-type]
        copies=lambda name: 2 if name == "gigaam" else 1,
        can_grow=lambda name, engine: True,
    )
    pool.get("gigaam")
    assert pool.copy_counts() == {"gigaam": 1}
    assert pool.copy_limits() == {"gigaam": 2}

    pool._grow("gigaam")  # в тесте вторую копию поднимаем синхронно

    assert pool.copy_counts() == {"gigaam": 2}


def test_can_grow_engine_reads_free_vram_and_measured_footprint(monkeypatch) -> None:
    """Гейт: под копию нужно место с запасом; без замера — отказ."""
    engine = cast(AsrEngine, Stub("gigaam"))
    monkeypatch.setattr(metrics, "model_vram_mb", lambda name, model: 900)
    monkeypatch.setattr(metrics, "gpu_info", lambda: {"vram_free_mb": 5000})
    assert asr_service.can_grow_engine("gigaam", engine) is True

    monkeypatch.setattr(metrics, "gpu_info", lambda: {"vram_free_mb": 1000})
    assert asr_service.can_grow_engine("gigaam", engine) is False  # 1000 < 900×1.15+256

    monkeypatch.setattr(metrics, "gpu_info", lambda: {"vram_free_mb": 900000})
    monkeypatch.setattr(metrics, "model_vram_mb", lambda name, model: None)
    assert asr_service.can_grow_engine("gigaam", engine) is False  # без замера не растим

    monkeypatch.setattr(metrics, "gpu_info", lambda: None)
    assert asr_service.can_grow_engine("gigaam", engine) is False  # GPU не видно
