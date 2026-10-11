"""Перезапуск сервера по кнопке из «Настроек».

Две среды — два способа:

- докер (стек): процесс и есть контейнер, поэтому шлём SIGTERM сами себе —
  uvicorn гасится по-хорошему, а политика рестарта compose
  (``restart: unless-stopped``) поднимает контейнер заново;
- нативный запуск: там перезапускать нас некому, поэтому заменяем процесс
  re-exec'ом той же командной строки (``sys.orig_argv``).

Ответ на HTTP-запрос уходит раньше остановки: перезапуск запускается
таймером через ``RESTART_DELAY_SEC``. Прерванные задачи при старте поднимет
настройка «Поднимать задачи после рестарта» — кнопка ничего не отменяет.
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import threading

log = logging.getLogger(__name__)

RESTART_DELAY_SEC = 0.6  # столько ждём, чтобы ответ успел уйти в сокет
_scheduled = threading.Event()


def in_container() -> bool:
    """True, когда мы в докере: перезапуск сделает политика рестарта."""
    return os.path.exists("/.dockerenv")


def restart_argv() -> list[str]:
    """Команда перезапуска — та же, но через ``sys.executable``.

    У uv-venv на Windows ``orig_argv[0]`` — БАЗОВЫЙ python без контекста
    venv (шим подменяет argv при запуске), и re-exec им теряет пакеты
    (``ModuleNotFoundError: stenograph``). ``sys.executable`` в этом случае —
    сам шим, который восстанавливает site-packages и editable-пакеты.
    """
    argv = [str(part) for part in (getattr(sys, "orig_argv", None) or [])]
    if argv:
        argv[0] = sys.executable
        return argv
    return [sys.executable, *sys.argv]


def _kill_self(sig: int) -> None:
    """Послать сигнал самим себе (обёртка — её подменяют тесты)."""
    os.kill(os.getpid(), sig)


def _exec(argv: list[str]) -> None:
    """Заменить процесс перезапущенной командой (обёртка — для тестов)."""
    os.execv(argv[0], argv)


def restart_now() -> None:
    """Немедленный перезапуск: сюда доходим через ``RESTART_DELAY_SEC``."""
    if in_container():
        log.warning("перезапуск сервера: SIGTERM себе — docker поднимет контейнер")
        _kill_self(signal.SIGTERM)
        return
    argv = restart_argv()
    log.warning("перезапуск сервера: re-exec %r", argv)
    try:
        _exec(argv)
    except OSError:
        log.exception("перезапуск не удался — завершаюсь, поднимите сервер вручную")
        os._exit(3)


def schedule_restart(delay: float = RESTART_DELAY_SEC) -> bool:
    """Запланировать перезапуск; False — он уже запланирован (защита от спама)."""
    if _scheduled.is_set():
        return False
    _scheduled.set()
    threading.Timer(delay, restart_now).start()
    return True
