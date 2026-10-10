"""Idle-model eviction: idle engines get unloaded, busy ones never do.

The module is exercised through its public surface: ``configure``,
``tracked``, ``mark_busy``/``mark_idle``, ``tick`` and ``evict_idle``.
A model that is ABOUT to load evicts idle ones first — that wiring is
tested through the real ``_load`` paths of both engines (fakes for
transformers/faster_whisper, real code).
"""

from __future__ import annotations

import sys
import time
import types

from stenograph import model_budget


class FakeEngine:
    """Unloadable engine double with a busy probe inside ``transcribe``."""

    def __init__(self, name: str = "fake", model: str = "model-x") -> None:
        self.name = name
        self.model_id = model
        self.unloaded = 0

    def unload(self) -> None:
        """Record the eviction."""
        self.unloaded += 1

    @model_budget.tracked
    def transcribe(self) -> str:
        """A tracked call: must count as busy while it runs."""
        assert model_budget.busy_count(self) == 1
        time.sleep(0.05)
        return "ok"


def test_idle_engine_is_evicted_after_the_timeout() -> None:
    """An engine idle past the threshold is unloaded, not before."""
    engine = FakeEngine()
    model_budget.configure(600)
    assert engine.transcribe() == "ok"

    assert model_budget.tick() == []  # idle, but not long enough
    model_budget.configure(0.01)
    time.sleep(0.05)
    assert model_budget.tick() == ["fake:model-x"]
    assert engine.unloaded == 1
    assert model_budget.tick() == []  # already gone from the registry


def test_busy_engine_is_never_evicted() -> None:
    """A transcribe in flight protects the engine from eviction."""
    engine = FakeEngine()
    model_budget.configure(0.01)
    model_budget.mark_busy(engine)
    time.sleep(0.05)
    assert model_budget.tick() == []
    assert engine.unloaded == 0

    model_budget.mark_idle(engine)
    time.sleep(0.05)
    assert model_budget.tick() == ["fake:model-x"]
    assert engine.unloaded == 1


def test_disabled_feature_only_allows_the_forced_path() -> None:
    """0 turns automatic eviction off; the manual (force) path still works."""
    engine = FakeEngine()
    engine.transcribe()
    model_budget.configure(0)
    assert model_budget.tick() == []
    assert model_budget.evict_idle() == []
    assert model_budget.evict_idle(force=True) == ["fake:model-x"]
    assert engine.unloaded == 1


def test_eviction_excludes_the_loading_engine() -> None:
    """The engine that is loading right now is never a victim."""
    loading = FakeEngine("whisper", "large-v3")
    idle = FakeEngine("moss", "moss-model")
    model_budget.configure(600)
    model_budget.mark_busy(loading)
    idle.transcribe()

    assert model_budget.evict_idle(exclude=loading) == ["moss:moss-model"]
    assert idle.unloaded == 1
    assert loading.unloaded == 0
    assert model_budget.busy_count(loading) == 1


def test_tracked_counts_busy_only_during_the_call() -> None:
    """The tracked decorator bumps and drops the busy counter."""
    engine = FakeEngine()
    model_budget.configure(600)
    assert model_budget.busy_count(engine) == 0
    assert engine.transcribe() == "ok"
    assert model_budget.busy_count(engine) == 0


def test_real_engines_are_wrapped_with_tracked() -> None:
    """Every real transcribe entry point carries the tracked decorator."""
    from stenograph.engines.moss import MossEngine
    from stenograph.engines.whisper import FasterWhisperEngine

    for method in (
        FasterWhisperEngine.transcribe,
        FasterWhisperEngine.transcribe_window,
        FasterWhisperEngine.transcribe_batch,
        MossEngine.transcribe,
    ):
        assert hasattr(method, "__wrapped__"), method


def test_whisper_load_evicts_idle_models_and_unload_frees(monkeypatch) -> None:
    """Whisper's _load evicts idle models first; unload drops the model."""
    from stenograph.engines.whisper import FasterWhisperEngine

    fake_faster_whisper = types.SimpleNamespace(
        WhisperModel=lambda ref, device, compute_type: {"ref": ref}
    )
    monkeypatch.setitem(sys.modules, "faster_whisper", fake_faster_whisper)
    monkeypatch.setattr("stenograph.engines.whisper.note_model_loaded", lambda *a, **k: None)
    recorded: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "stenograph.engines.whisper.note_model_unloaded",
        lambda engine, model: recorded.append((engine, model)),
    )
    model_budget.configure(600)

    bystander = FakeEngine("moss", "moss-idle")
    bystander.transcribe()  # registered + idle

    engine = FasterWhisperEngine(model="tiny", models_dir=None)
    assert engine._model is None
    engine._load()
    assert bystander.unloaded == 1  # idle models freed before the load
    assert engine._model is not None

    engine.unload()
    assert engine._model is None
    assert recorded == [("whisper", "tiny")]


def test_busy_models_lists_engines_in_a_transcribe() -> None:
    """busy_models() drives the monitor's «в работе» badge."""
    engine = FakeEngine("moss", "moss-model")
    assert model_budget.busy_models() == set()
    model_budget.mark_busy(engine)
    assert model_budget.busy_models() == {("moss", "moss-model")}
    model_budget.mark_idle(engine)
    assert model_budget.busy_models() == set()


def test_moss_touch_metrics_is_throttled(monkeypatch) -> None:
    """The MOSS token callback refreshes metrics at most once per 10 s."""
    from stenograph.engines import moss as moss_module
    from stenograph.engines.moss import MossEngine

    calls: list[object] = []
    monkeypatch.setattr(moss_module, "touch_engine", calls.append)
    engine = MossEngine(model_id="org/model", models_dir=None)

    engine._touch_metrics()
    engine._touch_metrics()  # заглушено троттлингом
    assert len(calls) == 1 and calls[0] is engine

    engine._last_touch = 0.0  # окно прошло
    engine._touch_metrics()
    assert len(calls) == 2
