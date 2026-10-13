"""Idle-model eviction: release VRAM/RAM of ASR models nobody is using.

Engines load their models lazily and keep them resident forever otherwise —
an idle server would hold gigabytes of VRAM that another model may need.
Two mechanisms:

- the background reaper unloads engines idle longer than
  ``MEETSCRIBE_MODEL_IDLE_UNLOAD_SEC`` (0 disables the automatic behaviour);
- every engine that is ABOUT to load first evicts all idle engines, so a
  model that needs memory takes it from models nobody is using.

Busy tracking is a counter around ``transcribe*`` calls (the :func:`tracked`
decorator) — an engine executing a transcribe is never unloaded. Besides busy,
engines used within the last ``_FRESH_SEC`` seconds are protected too: a live
session's engine is only busy DURING a window batch and looks idle between
them, so a fresh-use window is what keeps «Выгрузить простаивающие» and
pre-load evictions from killing a model mid-meeting. The manual
``POST /api/models/unload`` path works even when the automatic one is off.
"""

from __future__ import annotations

import functools
import logging
import threading
import time
import weakref
from collections.abc import Callable
from typing import Any, Protocol

log = logging.getLogger(__name__)

#: Engines used less than this long ago are never evicted (any path).
_FRESH_SEC = 60.0


class Unloadable(Protocol):
    """The engine surface this module relies on."""

    name: str
    model_id: str

    def unload(self) -> None:
        """Release the loaded model and free its memory."""
        ...


class _Entry:
    """Registry record for one engine instance (weakly referenced)."""

    __slots__ = ("ref", "busy", "last_used")

    def __init__(self, engine: Unloadable) -> None:
        self.ref: weakref.ReferenceType[Unloadable] = weakref.ref(engine)
        self.busy = 0
        self.last_used = time.monotonic()


_LOCK = threading.Lock()
_ENTRIES: dict[int, _Entry] = {}
_IDLE_SEC = 0.0  # 0 = disabled until configure() says otherwise
_REAP_INTERVAL = 30.0
_REAPER: threading.Thread | None = None
_STOP = threading.Event()


def configure(
    idle_sec: float, *, interval: float | None = None, fresh_sec: float | None = None
) -> None:
    """Set the idle threshold (0 disables automatic eviction) and arm the reaper.

    ``fresh_sec`` overrides the fresh-use protection window (engines used more
    recently than that are never unloaded, whatever the caller asked for);
    ``0`` disables the protection (tests).
    """
    global _IDLE_SEC, _REAP_INTERVAL, _FRESH_SEC
    with _LOCK:
        _IDLE_SEC = max(0.0, float(idle_sec))
        if interval is not None:
            _REAP_INTERVAL = max(1.0, float(interval))
        if fresh_sec is not None:
            _FRESH_SEC = max(0.0, float(fresh_sec))
    _ensure_reaper()


def reset() -> None:
    """Drop all registry state (tests; the reaper thread stays but goes quiet)."""
    global _IDLE_SEC, _FRESH_SEC, _REAPER
    with _LOCK:
        _ENTRIES.clear()
        _IDLE_SEC = 0.0
        _FRESH_SEC = 60.0
        _REAPER = None


def _ensure_reaper() -> None:
    global _REAPER
    with _LOCK:
        if _REAPER is not None and _REAPER.is_alive():
            return
        _STOP.clear()
        _REAPER = threading.Thread(
            target=_reap_loop, name="stenograph-model-reaper", daemon=True
        )
        _REAPER.start()


def _reap_loop() -> None:
    while not _STOP.wait(_REAP_INTERVAL):
        try:
            tick()
        except Exception:  # noqa: BLE001 — the reaper must survive anything
            log.exception("model reaper failed")


def tracked[**P, R](method: Callable[P, R]) -> Callable[P, R]:
    """Mark an engine busy for the duration of a transcribe call."""

    @functools.wraps(method)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        engine: Any = args[0] if args else None
        mark_busy(engine)
        try:
            return method(*args, **kwargs)
        finally:
            mark_idle(engine)

    return wrapper


def mark_busy(engine: Unloadable) -> None:
    """Register the engine (if new) and bump its busy counter."""
    with _LOCK:
        entry = _ENTRIES.get(id(engine))
        if entry is None or entry.ref() is not engine:
            _ENTRIES[id(engine)] = entry = _Entry(engine)
        entry.busy += 1
        entry.last_used = time.monotonic()


def mark_idle(engine: Unloadable) -> None:
    """Drop one busy level and stamp the engine as just used."""
    with _LOCK:
        entry = _ENTRIES.get(id(engine))
        if entry is None or entry.ref() is not engine:
            return
        if entry.busy > 0:
            entry.busy -= 1
        entry.last_used = time.monotonic()


def busy_count(engine: Unloadable) -> int:
    """Current busy level of a registered engine (0 = idle)."""
    with _LOCK:
        entry = _ENTRIES.get(id(engine))
        if entry is None or entry.ref() is not engine:
            return 0
        return entry.busy


def busy_models() -> set[tuple[str, str]]:
    """``(engine, model)`` pairs currently executing a transcribe call."""
    result: set[tuple[str, str]] = set()
    with _LOCK:
        for entry in _ENTRIES.values():
            engine = entry.ref()
            if engine is None or entry.busy <= 0:
                continue
            result.add(
                (str(getattr(engine, "name", "?")), str(getattr(engine, "model_id", "?")))
            )
    return result


def evict_idle(
    *,
    exclude: object | None = None,
    older_than: float | None = None,
    force: bool = False,
) -> list[str]:
    """Unload registered engines that are not busy; returns their names.

    ``exclude`` — the engine that is loading right now (never unload it).
    ``older_than`` — only engines idle at least that long (None = the
    fresh-use floor). Engines used less than ``_FRESH_SEC`` ago are never
    victims, whatever the caller asks: a live session's engine looks idle
    between window batches, and unloading it mid-meeting costs a reload.
    Automatic callers respect the master switch (0 = off); ``force`` is the
    manual path and bypasses it.
    """
    with _LOCK:
        if not force and _IDLE_SEC <= 0:
            return []
        now = time.monotonic()
        fresh_floor = max(older_than if older_than is not None else 0.0, _FRESH_SEC)
        victims: list[Unloadable] = []
        for key, entry in list(_ENTRIES.items()):
            engine = entry.ref()
            if engine is None:
                _ENTRIES.pop(key, None)
                continue
            if engine is exclude or entry.busy > 0:
                continue
            if now - entry.last_used < fresh_floor:
                continue
            _ENTRIES.pop(key, None)  # claim: nobody else may unload it
            victims.append(engine)
    unloaded: list[str] = []
    for engine in victims:
        with _LOCK:
            fresh = _ENTRIES.get(id(engine))
            if fresh is not None and fresh.ref() is engine and fresh.busy > 0:
                continue  # it went busy again while we were collecting
            if fresh is not None and fresh.ref() is engine:
                _ENTRIES.pop(id(engine), None)
        try:
            engine.unload()
        except Exception:  # noqa: BLE001 — one bad engine must not stop the rest
            log.exception("model unload failed for %s", getattr(engine, "name", "?"))
            continue
        unloaded.append(f"{getattr(engine, 'name', '?')}:{getattr(engine, 'model_id', '?')}")
    if unloaded:
        log.info("model budget: выгружены простаивающие модели: %s", ", ".join(unloaded))
    return unloaded


def tick() -> list[str]:
    """One reaper pass: unload engines idle beyond the configured threshold."""
    with _LOCK:
        idle_sec = _IDLE_SEC
    if idle_sec <= 0:
        return []
    return evict_idle(older_than=idle_sec)
