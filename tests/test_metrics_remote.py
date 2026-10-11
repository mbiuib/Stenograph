"""Monitor in remote mode: the server proxies the ASR service snapshot."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from fakes import FakeEngine, PositionTranscriber
from stenograph.api.app import create_app
from stenograph.bridge.manager import BridgeManager
from stenograph.config import Settings
from stenograph.events import EventBus
from stenograph.service import TranscriptionService
from stenograph.storage import JobRepository


def _stack(tmp_path: Path) -> TestClient:
    settings = Settings(
        data_dir=tmp_path, asr_backend="remote", asr_url="http://asr.test:9001"
    )
    settings.ensure_dirs()
    repo = JobRepository(settings.db_path)
    bus = EventBus()
    service = TranscriptionService(
        settings, repo, bus, engine_factory=lambda n, s: FakeEngine()
    )
    bridge = BridgeManager(
        settings, repo, bus, transcriber_factory=lambda language: PositionTranscriber()
    )
    return TestClient(create_app(settings=settings, service=service, bridge=bridge))


def test_metrics_include_the_service_snapshot(tmp_path, monkeypatch) -> None:
    """In remote mode /api/metrics carries the service's models and GPU."""
    calls: list[str] = []

    def fake_fetch(url: str, **kwargs) -> dict:
        calls.append(url)
        return {
            "url": url,
            "ok": True,
            "system": {
                "gpu": {
                    "name": "RTX",
                    "utilization_pct": 12,
                    "vram_total_mb": 16000,
                    "vram_used_mb": 6000,
                    "vram_free_mb": 10000,
                }
            },
            "models": [
                {
                    "engine": "whisper",
                    "model": "large-v3-turbo",
                    "vram_mb": 3000,
                    "ram_mb": 1200,
                    "idle_sec": 5.0,
                    "busy": False,
                    "loaded_at": "",
                    "last_used_at": "",
                    "load_number": 1,
                }
            ],
            "engines_loaded": ["whisper"],
        }

    monkeypatch.setattr("stenograph.api.app.fetch_service_metrics", fake_fetch)
    client = _stack(tmp_path)
    data = client.get("/api/metrics").json()
    assert calls == ["http://asr.test:9001"]
    assert data["asr_service"]["ok"] is True
    assert data["asr_service"]["models"][0]["engine"] == "whisper"
    assert data["asr_service"]["system"]["gpu"]["utilization_pct"] == 12


def test_local_mode_has_no_service_section(tmp_path) -> None:
    """Without remote mode the snapshot keeps its old shape."""
    client = _stack(tmp_path)
    # flip the backend for this app instance
    app_settings = client.app.state.settings  # type: ignore[attr-defined]
    app_settings.asr_backend = "local"
    data = client.get("/api/metrics").json()
    assert "asr_service" not in data


def test_unload_is_proxied_in_remote_mode(tmp_path, monkeypatch) -> None:
    """The monitor button reaches the service that owns the models."""
    monkeypatch.setattr(
        "stenograph.api.app.unload_service_models",
        lambda url, **kw: {"ok": True, "unloaded": ["whisper:large-v3-turbo"]},
    )
    client = _stack(tmp_path)
    data = client.post("/api/models/unload").json()
    assert data["unloaded"] == ["whisper:large-v3-turbo"]
    assert data["service"]["ok"] is True
