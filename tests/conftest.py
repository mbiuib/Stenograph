"""Test-wide environment defaults.

The production default is record-only (MEETSCRIBE_REALTIME_TRANSCRIBE=false):
recordings are decoded later through the queue. The suite exercises the
realtime decoding paths, so it opts back in here; tests for the record-only
mode pass ``realtime_transcribe=False`` explicitly.
"""

from __future__ import annotations

import os

os.environ.setdefault("MEETSCRIBE_REALTIME_TRANSCRIBE", "true")
