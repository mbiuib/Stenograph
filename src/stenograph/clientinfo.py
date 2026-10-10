"""Client (browser) info reported by the /ws/live start handshake.

The page sends its user agent and platform once per session; the server
turns that into a short device tag for the job name («Chrome · Windows»)
and keeps the raw data in the job metadata.
"""

from __future__ import annotations

import re

_BROWSERS: tuple[tuple[str, str], ...] = (
    ("YaBrowser/", "Яндекс Браузер"),
    ("Edg/", "Edge"),
    ("OPR/", "Opera"),
    ("Firefox/", "Firefox"),
    ("Chrome/", "Chrome"),
    ("Version/", "Safari"),
)

_OS: tuple[tuple[str, str], ...] = (
    ("Windows", "Windows"),
    ("Android", "Android"),
    ("iPhone", "iOS"),
    ("iPad", "iOS"),
    ("Mac OS X", "macOS"),
    ("Linux", "Linux"),
)


def _browser_label(user_agent: str) -> str | None:
    """Browser name plus major version when the UA carries it."""
    for token, label in _BROWSERS:
        if token not in user_agent:
            continue
        if label == "Safari" and "Chrome" in user_agent:
            continue  # Chrome's UA also contains «Safari/…»
        match = re.search(re.escape(token) + r"(\d+)", user_agent)
        return f"{label} {match.group(1)}" if match else label
    return None


def short_device_tag(client: dict | None) -> str | None:
    """«Chrome 141 · Windows»-style tag from the handshake data; None when unknown."""
    data = client or {}
    user_agent = str(data.get("user_agent") or "")
    if not user_agent:
        platform = str(data.get("platform") or "").strip()
        return platform or None
    browser = _browser_label(user_agent)
    system = next((label for token, label in _OS if token in user_agent), None)
    parts = [part for part in (browser, system) if part]
    return " · ".join(parts) if parts else None
