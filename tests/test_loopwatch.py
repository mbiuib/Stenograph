"""Loop-lag watchdog: a blocked event loop must be seen, not just felt."""

from __future__ import annotations

import asyncio
import contextlib
import time

from stenograph import loopwatch


def test_monitor_records_and_warns_with_cooldown() -> None:
    """Lag above the threshold warns once, then respects the cooldown."""
    meter = loopwatch.LoopLagMonitor()
    assert meter.note(0.1) is None
    assert meter.last_sec == 0.1
    assert meter.max_sec == 0.1
    message = meter.note(loopwatch.WARN_LAG_SEC + 0.5)
    assert message is not None and "отставал" in message
    assert meter.note(loopwatch.WARN_LAG_SEC + 1.0) is None  # still cooling down


def test_watchdog_flags_a_blocked_loop() -> None:
    """A synchronous block on the loop shows up in the lag monitor."""

    async def scenario() -> float:
        meter = loopwatch.LoopLagMonitor()
        task = asyncio.create_task(loopwatch.run(interval=0.05, meter=meter))
        await asyncio.sleep(0.1)
        time.sleep(0.5)  # the very bug class: a sync call holding the loop
        await asyncio.sleep(0.15)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        return meter.max_sec

    lag = asyncio.run(scenario())
    assert lag >= 0.3, f"blocked loop not detected (max lag {lag:.2f}s)"
