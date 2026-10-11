"""Кнопка «Перезапустить сервер»: эндпоинт, режимы и защита от спама."""

from __future__ import annotations

import signal
import time

from fastapi.testclient import TestClient

from stenograph import server_control
from stenograph.api.app import create_app
from stenograph.config import Settings


def test_restart_endpoint_schedules_and_reports(tmp_path, monkeypatch) -> None:
    """POST /api/server/restart отвечает сразу и не плодит перезапуски."""
    calls: list[float] = []

    def fake_schedule(delay: float = server_control.RESTART_DELAY_SEC) -> bool:
        calls.append(delay)
        return len(calls) == 1

    monkeypatch.setattr(server_control, "schedule_restart", fake_schedule)
    client = TestClient(create_app(Settings(data_dir=tmp_path)))
    first = client.post("/api/server/restart")
    assert first.status_code == 200, first.text
    assert first.json() == {"ok": True, "scheduled": True}
    second = client.post("/api/server/restart")
    assert second.json() == {"ok": True, "scheduled": False}
    assert len(calls) == 2


def test_restart_inside_container_is_a_sigterm(monkeypatch) -> None:
    """В докере перезапуск — SIGTERM себе: контейнер поднимет политика."""
    monkeypatch.setattr(server_control, "in_container", lambda: True)
    killed: list[int] = []
    monkeypatch.setattr(server_control, "_kill_self", killed.append)
    server_control.restart_now()
    assert killed == [signal.SIGTERM]


def test_restart_native_reexecs_the_same_command(monkeypatch) -> None:
    """Нативно перезапуск — re-exec той же командной строки."""
    monkeypatch.setattr(server_control, "in_container", lambda: False)
    argv = ["C:/venv/python.exe", "-m", "uvicorn", "stenograph.api.app:create_app", "--factory"]
    monkeypatch.setattr(server_control, "restart_argv", lambda: argv)
    calls: list[list[str]] = []
    monkeypatch.setattr(server_control, "_exec", calls.append)
    server_control.restart_now()
    assert calls == [argv]


def test_schedule_restart_fires_exactly_once(monkeypatch) -> None:
    """Спам кнопкой не плодит таймеры: перезапуск уходит ровно один раз."""
    fired: list[int] = []
    monkeypatch.setattr(server_control, "restart_now", lambda: fired.append(1))
    server_control._scheduled.clear()
    try:
        assert server_control.schedule_restart(0.01) is True
        assert server_control.schedule_restart(0.01) is False
        deadline = time.monotonic() + 3.0
        while not fired and time.monotonic() < deadline:
            time.sleep(0.02)
        assert fired == [1]
    finally:
        server_control._scheduled.clear()


def test_restart_argv_falls_back_to_sys_argv(monkeypatch) -> None:
    """Если интерпретатор не дал orig_argv — берём sys.argv как есть."""
    monkeypatch.setattr(server_control.sys, "orig_argv", [], raising=False)
    monkeypatch.setattr(server_control.sys, "executable", "C:/venv/python.exe", raising=False)
    monkeypatch.setattr(
        server_control.sys, "argv", ["uvicorn", "stenograph.api.app:create_app"], raising=False
    )
    assert server_control.restart_argv() == [
        "C:/venv/python.exe",
        "uvicorn",
        "stenograph.api.app:create_app",
    ]
