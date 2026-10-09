"""Event-loop lag watchdog: a blocked loop must be seen, not just felt.

Everything (HTTP, WebSockets, SSE) shares one event loop; any synchronous
block there looks like a frozen server. This watchdog measures how late the
loop wakes up, logs a critical line when it actually stalls, and exposes the
numbers via /api/health.
"""

from __future__ import annotations

import logging
import time

import anyio

log = logging.getLogger(__name__)

CHECK_INTERVAL_SEC = 1.0
WARN_LAG_SEC = 2.0
WARN_COOLDOWN_SEC = 60.0


class LoopLagMonitor:
    """Last/max event-loop lag with a log-warning cooldown."""

    def __init__(self) -> None:
        self.last_sec = 0.0
        self.max_sec = 0.0
        self.warnings = 0
        self._last_warn_at = 0.0

    def note(self, lag_sec: float) -> str | None:
        """Record one lag measurement; return a warning text when notable."""
        lag = max(0.0, lag_sec)
        self.last_sec = lag
        self.max_sec = max(self.max_sec, lag)
        now = time.monotonic()
        if lag < WARN_LAG_SEC or now - self._last_warn_at < WARN_COOLDOWN_SEC:
            return None
        self._last_warn_at = now
        self.warnings += 1
        return f"event loop отставал на {lag:.1f} с — есть синхронная блокировка?"


monitor = LoopLagMonitor()


async def run(interval: float = CHECK_INTERVAL_SEC, meter: LoopLagMonitor | None = None) -> None:
    """Measure loop lag forever; log critical when the loop actually stalls."""
    target = meter or monitor
    while True:
        started = time.monotonic()
        await anyio.sleep(interval)
        message = target.note(time.monotonic() - started - interval)
        if message:
            log.critical("%s", message)
