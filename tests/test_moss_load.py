"""MOSS lazy loading: parallel engine instances must serialize their loads.

transformers' ``from_pretrained`` mutates global torch state (the meta-device
init context), so two overlapping loads corrupt each other ("Cannot copy out
of meta tensor") — exactly what two parallel queue workers did live.
"""

from __future__ import annotations

import sys
import threading
import time
import types

from stenograph import model_budget
from stenograph.engines.moss import MossEngine


class _FakeModel:
    """Stands in for the loaded transformers model (chainable ``.to/.eval``)."""

    def to(self, *args, **kwargs):
        """Pretend to move the model; slow enough to overlap without a lock."""
        time.sleep(0.2)
        return self

    def eval(self):
        """No-op eval (chainable)."""
        return self


class _FakeProcessor:
    """Minimal processor stand-in."""

    chat_template = None


def _install_fake_transformers(monkeypatch, state: dict) -> None:
    """Patch torch/transformers so _load can run without a real model."""

    def from_pretrained(source, **kwargs):
        state["live"] += 1
        state["max_live"] = max(state["max_live"], state["live"])
        state["calls"] += 1
        try:
            time.sleep(0.3)
            return _FakeModel()
        finally:
            state["live"] -= 1

    fake_torch = types.SimpleNamespace(
        cuda=types.SimpleNamespace(is_available=lambda: False),
        float32="float32",
        bfloat16="bfloat16",
    )

    fake_transformers = types.SimpleNamespace(
        AutoModelForCausalLM=types.SimpleNamespace(from_pretrained=from_pretrained),
        AutoProcessor=types.SimpleNamespace(
            from_pretrained=lambda source, **kwargs: _FakeProcessor()
        ),
    )

    monkeypatch.setitem(sys.modules, "torch", fake_torch)
    monkeypatch.setitem(sys.modules, "transformers", fake_transformers)
    monkeypatch.setattr("stenograph.engines.moss.note_model_loaded", lambda *a, **k: None)


def test_moss_loads_serialize_across_instances(monkeypatch) -> None:
    """Two engines loading in parallel threads never load at the same time."""
    state = {"live": 0, "max_live": 0, "calls": 0}
    _install_fake_transformers(monkeypatch, state)
    engines = [MossEngine(model_id="org/model", models_dir=None) for _ in range(2)]
    threads = [threading.Thread(target=engine._load) for engine in engines]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert state["calls"] == 2
    assert state["max_live"] == 1  # the class-level load lock serialized them


def test_same_instance_loads_once(monkeypatch) -> None:
    """A second _load on the same engine reuses the model (no reload)."""
    state = {"live": 0, "max_live": 0, "calls": 0}
    _install_fake_transformers(monkeypatch, state)
    engine = MossEngine(model_id="org/model", models_dir=None)
    first = engine._load()
    second = engine._load()
    assert state["calls"] == 1
    assert first[1] is second[1]


class _BystanderEngine:
    """Idle unloadable double used to observe evict-on-load."""

    name = "whisper"
    model_id = "turbo"

    def __init__(self) -> None:
        self.unloaded = 0

    def unload(self) -> None:
        """Record the eviction."""
        self.unloaded += 1


def test_moss_load_evicts_idle_models_first(monkeypatch) -> None:
    """The loading engine takes the memory of models sitting idle."""
    state = {"live": 0, "max_live": 0, "calls": 0}
    _install_fake_transformers(monkeypatch, state)
    model_budget.configure(600)
    bystander = _BystanderEngine()
    model_budget.mark_busy(bystander)
    model_budget.mark_idle(bystander)  # registered, idle

    engine = MossEngine(model_id="org/model", models_dir=None)
    engine._load()

    assert bystander.unloaded == 1
