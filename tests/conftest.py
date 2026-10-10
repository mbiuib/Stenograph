"""Test-wide environment defaults.

The production default is record-only (MEETSCRIBE_REALTIME_TRANSCRIBE=false):
recordings are decoded later through the queue. The suite exercises the
realtime decoding paths, so it opts back in here; tests for the record-only
mode pass ``realtime_transcribe=False`` explicitly.
"""

from __future__ import annotations

import os

os.environ.setdefault("MEETSCRIBE_REALTIME_TRANSCRIBE", "true")

import pytest  # noqa: E402

from stenograph import model_budget  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_model_budget():
    """Reset the model-budget registry around every test.

    The registry is process-global; leftovers between tests would make
    eviction behaviour depend on test order.
    """
    model_budget.reset()
    yield
    model_budget.reset()
