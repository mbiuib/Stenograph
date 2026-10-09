"""Resource metrics: per-model attribution and the /api/metrics endpoint."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from fakes import FakeEngine
from stenograph import metrics
from stenograph.api.app import create_app
from stenograph.config import Settings
from stenograph.events import EventBus
from stenograph.service import TranscriptionService
from stenograph.storage import JobRepository


def _fake_gpu(used_mb: int) -> dict[str, Any]:
    return {
        "name": "FakeGPU",
        "utilization_pct": 3,
        "vram_total_mb": 16303,
        "vram_used_mb": used_mb,
        "vram_free_mb": 16303 - used_mb,
    }


def _isolate(monkeypatch: Any) -> dict[str, float]:
    """Fake hardware probes and a clean model registry."""
    state = {"vram": 1000.0, "rss": 300.0}
    monkeypatch.setattr(metrics, "gpu_info", lambda: _fake_gpu(int(state["vram"])))
    monkeypatch.setattr(metrics, "process_memory_mb", lambda: (state["rss"], state["rss"] + 100))
    monkeypatch.setattr(metrics, "_MODELS", {})
    monkeypatch.setattr(metrics, "_BASELINE", None)
    return state


class _FakeLiveEngine:
    name = "whisper"
    model_id = "large-v3-turbo"


def test_note_model_loaded_attributes_deltas(monkeypatch: Any) -> None:
    """Each model's footprint is the growth since the previous baseline."""
    state = _isolate(monkeypatch)
    metrics.prime()  # baseline: 1000 MB VRAM / 300 MB RSS

    state["vram"] = 1000 + 1650
    state["rss"] = 300.0 + 780
    metrics.note_model_loaded("whisper", "large-v3-turbo")
    entry = metrics._MODELS["whisper:large-v3-turbo"]
    assert entry["vram_mb"] == 1650
    assert entry["ram_mb"] == 780.0
    assert entry["load_number"] == 1

    state["vram"] += 900
    state["rss"] += 400
    metrics.note_model_loaded("moss", "OpenMOSS/MOSS-Transcribe-Diarize")
    second = metrics._MODELS["moss:OpenMOSS/MOSS-Transcribe-Diarize"]
    assert second["vram_mb"] == 900
    assert second["load_number"] == 2


def test_touch_engine_updates_last_used(monkeypatch: Any) -> None:
    """Using an engine refreshes its ``last_used_at`` stamp."""
    _isolate(monkeypatch)
    metrics.prime()
    metrics.note_model_loaded("whisper", "large-v3-turbo")
    key = "whisper:large-v3-turbo"
    metrics._MODELS[key]["last_used_at"] = "2000-01-01T00:00:00+03:00"

    metrics.touch_engine(_FakeLiveEngine())

    assert metrics._MODELS[key]["last_used_at"] != "2000-01-01T00:00:00+03:00"


def test_touch_engine_ignores_unknown_engines(monkeypatch: Any) -> None:
    """Touching an engine that never loaded is a no-op, not an error."""
    _isolate(monkeypatch)
    metrics.touch_engine(_FakeLiveEngine())
    assert metrics._MODELS == {}


def test_snapshot_shape(monkeypatch: Any) -> None:
    """The snapshot carries system data, models and the injected lane state."""
    _isolate(monkeypatch)
    monkeypatch.setattr(
        metrics, "gpu_processes", lambda: [{"pid": 42, "name": "python", "vram_mb": 2500}]
    )
    monkeypatch.setattr(metrics, "llm_status", lambda: {"lm_studio": False})
    metrics.prime()

    snap = metrics.snapshot(
        live={"active": False, "sessions": []},
        queue={"active": None, "waiting": []},
        counts={"done": 3},
    )

    assert snap["system"]["gpu"]["vram_used_mb"] == 1000
    assert snap["system"]["self_process"]["rss_mb"] == 300.0
    assert snap["system"]["gpu_processes"][0]["vram_mb"] == 2500
    assert snap["live"] == {"active": False, "sessions": []}
    assert snap["queue"] == {"active": None, "waiting": []}
    assert snap["jobs"] == {"done": 3}
    assert snap["llm"] == {"lm_studio": False}


def test_metrics_endpoint(tmp_path: Path, monkeypatch: Any) -> None:
    """GET /api/metrics returns the resource snapshot without real hardware."""
    _isolate(monkeypatch)
    monkeypatch.setattr(metrics, "gpu_processes", lambda: [])
    monkeypatch.setattr(metrics, "llm_status", lambda: {"lm_studio": True})

    settings = Settings(data_dir=tmp_path / "data")
    settings.ensure_dirs()
    service = TranscriptionService(
        settings,
        JobRepository(settings.db_path),
        EventBus(),
        engine_factory=lambda name, prepared: FakeEngine(),
    )
    client = TestClient(create_app(settings=settings, service=service))

    response = client.get("/api/metrics")

    assert response.status_code == 200
    body = response.json()
    assert body["system"]["gpu"]["name"] == "FakeGPU"
    assert body["system"]["self_process"]["pid"] > 0
    assert isinstance(body["models"], list)
    assert "live" in body and "queue" in body and "jobs" in body
