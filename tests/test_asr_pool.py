"""Пул сервиса: алиасы gigaam/gigaam-live делят один движок."""

from __future__ import annotations

from stenograph.asr_service import EnginePool


def test_service_pool_shares_one_engine_for_gigaam_aliases() -> None:
    """gigaam-live не плодит вторую копию модели в пуле сервиса."""
    built: list[str] = []

    class Stub:
        pass

    def build(name: str) -> Stub:
        built.append(name)
        return Stub()

    pool = EnginePool(build)  # type: ignore[arg-type]
    first, lock_one = pool.get("gigaam-live")
    second, lock_two = pool.get("gigaam")
    assert first is second
    assert lock_one is lock_two
    assert built == ["gigaam"]
