"""Runtime settings: the registry, the /api/settings API and persistence.

The «Настройки» tab edits `Settings` live: an override mutates the running
object (consumers read `settings.<key>` at use time), persists to
data/settings_overrides.json and is re-applied on the next start.
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from stenograph import model_budget
from stenograph.api.app import create_app
from stenograph.config import Settings
from stenograph.settings_service import SettingsService


def _app(tmp_path: Path) -> tuple[Settings, FastAPI, TestClient]:
    settings = Settings(data_dir=tmp_path)
    app = create_app(settings)
    return settings, app, TestClient(app)


def _find(snapshot: dict, key: str) -> dict:
    for group in snapshot["groups"]:
        for item in group["items"]:
            if item["key"] == key:
                return item
    raise AssertionError(f"{key} отсутствует в снапшоте настроек")


def test_settings_endpoint_lists_groups_and_items(tmp_path: Path) -> None:
    """GET /api/settings отдаёт сгруппированный реестр с метаданными."""
    settings, _app_obj, client = _app(tmp_path)
    response = client.get("/api/settings")
    assert response.status_code == 200, response.text
    snapshot = response.json()
    groups = {group["id"] for group in snapshot["groups"]}
    assert {"processing", "live", "bridge", "load", "remote", "llm", "misc"} <= groups

    engine = _find(snapshot, "engine")
    assert engine["value"] == settings.engine
    assert engine["editable"] is True
    assert engine["applies"] == "restart"
    assert engine["value"] in engine["choices"]

    drop = _find(snapshot, "drop_realtime_on_finalize")
    assert drop["value"] is False  # по умолчанию — прежнее поведение
    assert drop["editable"] is True

    secret = _find(snapshot, "llm_api_key")
    assert secret["value"] is None  # секреты не утекают в UI
    assert secret["editable"] is False

    readonly = _find(snapshot, "data_dir")
    assert readonly["editable"] is False


def test_settings_patch_applies_live_and_persists(tmp_path: Path) -> None:
    """PATCH меняет живой Settings, пишет файл и переживает рестарт."""
    settings, _app_obj, client = _app(tmp_path)
    response = client.patch(
        "/api/settings",
        json={"values": {"bridge_pending_max_sec": 77.5, "drop_realtime_on_finalize": False}},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["errors"] == {}
    assert body["applied"] == {"bridge_pending_max_sec": 77.5, "drop_realtime_on_finalize": False}

    # Живой объект настроек изменился — этим и живёт рантайм.
    assert settings.bridge_pending_max_sec == 77.5
    assert settings.drop_realtime_on_finalize is False

    snapshot = body["settings"]
    assert _find(snapshot, "bridge_pending_max_sec")["source"] == "runtime"

    overrides_path = tmp_path / "settings_overrides.json"
    saved = json.loads(overrides_path.read_text(encoding="utf-8"))
    assert saved["bridge_pending_max_sec"] == 77.5
    assert saved["drop_realtime_on_finalize"] is False

    # Рестарт: новый Settings + сервис поднимают сохранённые значения.
    fresh = Settings(data_dir=tmp_path)
    SettingsService(fresh)
    assert fresh.bridge_pending_max_sec == 77.5
    assert fresh.drop_realtime_on_finalize is False


def test_settings_patch_validates_each_key(tmp_path: Path) -> None:
    """Неверные значения и нередактируемые ключи не применяются."""
    settings, _app_obj, client = _app(tmp_path)
    before = settings.file_workers
    response = client.patch(
        "/api/settings",
        json={"values": {"file_workers": 999, "nope": 1, "data_dir": "/x"}},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["applied"] == {}
    assert set(body["errors"]) == {"file_workers", "nope", "data_dir"}
    assert settings.file_workers == before


def test_settings_reset_returns_the_default(tmp_path: Path) -> None:
    """POST /api/settings/reset возвращает значение из окружения."""
    settings, _app_obj, client = _app(tmp_path)
    client.patch("/api/settings", json={"values": {"jitsi_idle_stop_sec": 42.0}})
    assert settings.jitsi_idle_stop_sec == 42.0

    response = client.post("/api/settings/reset", json={"key": "jitsi_idle_stop_sec"})
    assert response.status_code == 200, response.text
    assert response.json()["applied"] == {"jitsi_idle_stop_sec": 600.0}
    assert settings.jitsi_idle_stop_sec == 600.0
    assert _find(response.json()["settings"], "jitsi_idle_stop_sec")["source"] == "default"
    saved = json.loads((tmp_path / "settings_overrides.json").read_text(encoding="utf-8"))
    assert "jitsi_idle_stop_sec" not in saved


def test_settings_appliers_reach_live_components(tmp_path: Path) -> None:
    """Побочные эффекты смены настроек применяются к живым компонентам."""
    settings, app, client = _app(tmp_path)

    response = client.patch("/api/settings", json={"values": {"live_auto_reprocess": False}})
    assert response.json()["errors"] == {}
    assert app.state.live._auto_reprocess is False
    assert app.state.bridge._auto_reprocess is False

    response = client.patch("/api/settings", json={"values": {"model_idle_unload_sec": 55.0}})
    assert response.json()["errors"] == {}
    assert model_budget._IDLE_SEC == 55.0

    response = client.patch("/api/settings", json={"values": {"bridge_finalize_concurrency": 3}})
    assert response.json()["errors"] == {}
    assert app.state.bridge._finalize_gate._value == 3


def test_settings_startup_applies_saved_overrides(tmp_path: Path) -> None:
    """Сохранённые изменения поднимаются при старте приложения."""
    (tmp_path / "settings_overrides.json").write_text(
        json.dumps({"jitsi_idle_stop_sec": 123.0, "unknown_key": 1}),
        encoding="utf-8",
    )
    settings = Settings(data_dir=tmp_path)
    create_app(settings)
    assert settings.jitsi_idle_stop_sec == 123.0
