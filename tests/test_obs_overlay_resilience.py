"""Tests for OBS overlay resilience, cache elimination, and OBS WebSocket browser source refresh."""

import os
import re
import urllib.request
import pytest
from scripts.subtitle_server import SubtitleServer, OVERLAY_HTML

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MIC_PAGE_PATH = os.path.join(REPO_ROOT, "scripts", "mic_page.html")


@pytest.fixture
def running_server(tmp_path, monkeypatch):
    """Start an in-process SubtitleServer on an ephemeral port."""
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "1")
    monkeypatch.setattr("scripts.subtitle_server._get_storage_dir", lambda: str(tmp_path))

    server = SubtitleServer(port=0, host="127.0.0.1")
    server.start()
    port = server._httpd.server_address[1]

    yield server, port

    if server._httpd is not None:
        server._httpd.shutdown()


def test_overlay_html_self_healing_and_transparent():
    """Verify OVERLAY_HTML has self-healing EventSource and keeps 100% transparent (no visual indicators)."""
    assert "connectEventSource" in OVERLAY_HTML
    assert "heartbeatTimer" in OVERLAY_HTML
    assert "resetHeartbeat" in OVERLAY_HTML
    assert "scheduleReconnect" in OVERLAY_HTML
    assert "visibilitychange" in OVERLAY_HTML
    assert "fetch(\"/overlay-style?_t=" in OVERLAY_HTML

    # Safety: ensure no stealth indicators or visible alert elements were added to overlay body
    assert "🔴" not in OVERLAY_HTML
    assert "未接続" not in OVERLAY_HTML
    assert '<div id="indicator"' not in OVERLAY_HTML


def test_server_no_cache_headers(running_server):
    """Verify /, /events, and /overlay-style enforce strict no-cache headers."""
    server, port = running_server

    # 1. GET / (OVERLAY_HTML)
    req = urllib.request.Request(f"http://127.0.0.1:{port}/")
    with urllib.request.urlopen(req) as resp:
        assert resp.status == 200
        headers = dict(resp.headers)
        cc = headers.get("Cache-Control", "")
        assert "no-store" in cc
        assert "no-cache" in cc
        assert "must-revalidate" in cc
        assert headers.get("Pragma") == "no-cache"
        assert headers.get("Expires") == "0"

    # 2. GET /overlay-style
    req_style = urllib.request.Request(f"http://127.0.0.1:{port}/overlay-style")
    with urllib.request.urlopen(req_style) as resp:
        assert resp.status == 200
        headers = dict(resp.headers)
        cc = headers.get("Cache-Control", "")
        assert "no-store" in cc
        assert "no-cache" in cc

    # 3. GET /events
    req_events = urllib.request.Request(f"http://127.0.0.1:{port}/events")
    with urllib.request.urlopen(req_events) as resp:
        assert resp.status == 200
        headers = dict(resp.headers)
        cc = headers.get("Cache-Control", "")
        assert "no-store" in cc
        assert "no-cache" in cc
        assert headers.get("Content-Type") == "text/event-stream"
        resp.close()


def test_mic_page_obs_websocket_refresh_integration():
    """Verify mic_page.html implements OBS WebSocket browser source auto-detection and cache-clearing refresh."""
    with open(MIC_PAGE_PATH, "r", encoding="utf-8") as f:
        html = f.read()

    # UI Action Button
    assert 'id="btn-obs-refresh-overlay"' in html
    assert "OBS字幕オーバーレイを強制リフレッシュ" in html

    # JS Implementation in ObsMuteSync
    assert "refreshBrowserSources" in html
    assert "PressInputPropertiesButton" in html
    assert "refreshnocache" in html
    assert "GetInputSettings" in html
    assert "browser_source" in html
    assert "8833" in html

    # Auto refresh trigger on startRecording
    assert "window.obsMuteSync.refreshBrowserSources(false)" in html
