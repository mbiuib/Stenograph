"""Device tag parsing for the live start handshake (clientinfo)."""

from __future__ import annotations

from stenograph.clientinfo import short_device_tag

UA_CHROME = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)
UA_YANDEX = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 YaBrowser/24.6.0.0 Safari/537.36"
)
UA_EDGE = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36 Edg/130.0.0.0"
)
UA_FIREFOX_MAC = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:131.0) Gecko/20100101 Firefox/131.0"
)
UA_ANDROID = (
    "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36"
)


def test_chrome_on_windows() -> None:
    """Chrome's UA maps to «Chrome N · Windows»."""
    assert short_device_tag({"user_agent": UA_CHROME}) == "Chrome 141 · Windows"


def test_yandex_browser_wins_over_chrome_token() -> None:
    """Яндекс Браузер ships a Chrome token too — its own token must win."""
    assert short_device_tag({"user_agent": UA_YANDEX}) == "Яндекс Браузер 24 · Windows"


def test_edge_wins_over_chrome_token() -> None:
    """Edge also carries the Chrome token; Edg/ must be checked first."""
    assert short_device_tag({"user_agent": UA_EDGE}) == "Edge 130 · Windows"


def test_firefox_on_macos() -> None:
    """Firefox on macOS maps cleanly."""
    assert short_device_tag({"user_agent": UA_FIREFOX_MAC}) == "Firefox 131 · macOS"


def test_android_chrome() -> None:
    """Android is detected ahead of the generic Linux token."""
    assert short_device_tag({"user_agent": UA_ANDROID}) == "Chrome 120 · Android"


def test_platform_fallback_without_user_agent() -> None:
    """Without a UA the platform string is used; empty reports give None."""
    assert short_device_tag({"platform": "Win32"}) == "Win32"
    assert short_device_tag({}) is None
    assert short_device_tag(None) is None
