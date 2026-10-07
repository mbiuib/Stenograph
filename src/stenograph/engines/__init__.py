"""Engine registry.

Core code never imports concrete engines; it asks this registry for a
factory. Adding a backend (MOSS, GigaAM, DiariZen, ...) means writing a
module that implements the protocols from .base and registering it here.
"""

from __future__ import annotations

from collections.abc import Callable

from ..config import Settings
from .base import AsrEngine

EngineFactory = Callable[[Settings], AsrEngine]

_ASR_FACTORIES: dict[str, EngineFactory] = {}


def register_asr(name: str, factory: EngineFactory) -> None:
    """Register (or replace) an ASR engine factory."""
    _ASR_FACTORIES[name] = factory


def available_asr() -> list[str]:
    """Names of all registered ASR engines."""
    return sorted(_ASR_FACTORIES)


def get_asr(name: str, settings: Settings) -> AsrEngine:
    """Instantiate a registered ASR engine."""
    if name not in _ASR_FACTORIES:
        raise KeyError(f"unknown ASR engine '{name}'; available: {available_asr()}")
    return _ASR_FACTORIES[name](settings)


def _register_builtin() -> None:
    from .whisper import FasterWhisperEngine

    register_asr(
        "whisper",
        lambda s: FasterWhisperEngine(
            model=s.whisper_model,
            models_dir=s.models_dir,
            device=s.device,
            compute_type=s.compute_type,
        ),
    )


_register_builtin()
