"""Tests for mic_page.html and OBS microphone mute sync UI/logic."""

import os
import re

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MIC_PAGE_PATH = os.path.join(REPO_ROOT, "scripts", "mic_page.html")


def test_mic_page_exists():
    assert os.path.exists(MIC_PAGE_PATH), "scripts/mic_page.html does not exist"


def test_obs_mute_sync_ui_elements():
    with open(MIC_PAGE_PATH, "r", encoding="utf-8") as f:
        html = f.read()

    # Card & Header
    assert "obs-sync-card" in html, "OBS sync card missing"
    assert "OBS マイクミュート連動" in html, "OBS sync card title missing"
    assert "OBS WebSocket v5" in html, "OBS WebSocket v5 tag missing"

    # Toggle (Default ON)
    assert 'id="obs-sync-toggle"' in html, "OBS sync toggle missing"
    # Verify checked by default
    toggle_match = re.search(r'<input[^>]+id="obs-sync-toggle"[^>]*>', html)
    assert toggle_match is not None, "Toggle input tag not found"
    assert "checked" in toggle_match.group(0), "OBS sync toggle must be checked by default"

    # Status & Warning banner
    assert 'id="obs-status-badge"' in html, "Status badge missing"
    assert 'id="obs-mute-banner"' in html, "Mute banner missing"
    assert "OBS 側でマイクがミュートされています" in html, "Mute warning text missing"

    # Form inputs
    assert 'id="obs-ws-url"' in html, "WS URL input missing"
    assert 'id="obs-ws-password"' in html, "Password input missing"
    assert 'id="obs-target-source"' in html, "Target source select missing"

    # Buttons
    assert 'id="btn-obs-reconnect"' in html, "Reconnect button missing"
    assert "🔄 OBS に再接続" in html, "Reconnect button text missing"
    assert 'id="btn-obs-save"' in html, "Save button missing"

    # Instruction accordion
    assert "OBS 接続設定 & 設定手順（インストラクション）" in html, "Accordion summary missing"
    assert "OBS Studio 側の設定手順（初回のみ）" in html, "Instruction heading missing"
    assert "4455" in html, "Default port 4455 missing in instructions"


def test_obs_mute_sync_js_logic():
    with open(MIC_PAGE_PATH, "r", encoding="utf-8") as f:
        html = f.read()

    # Class definition and initialization
    assert "class ObsMuteSync" in html, "ObsMuteSync class definition missing"
    assert "window.obsMuteSync = new ObsMuteSync();" in html, "ObsMuteSync instance creation missing"

    # obs-websocket v5 protocol handlers
    assert "GetSpecialInputs" in html, "GetSpecialInputs request missing"
    assert "GetInputList" in html, "GetInputList request missing"
    assert "GetInputMute" in html, "GetInputMute request missing"
    assert "InputMuteStateChanged" in html, "InputMuteStateChanged event handler missing"
    assert "computeAuth" in html, "SHA-256 auth computation missing"

    # Audio gating: resampleAndCollect must check isMuted
    assert "if (window.obsMuteSync && window.obsMuteSync.isMuted) return;" in html, (
        "Audio PCM stream gating check missing in resampleAndCollect"
    )

    # Level meter gating: updateMeter must check isMuted
    assert "levelVal.textContent = \"🔇 ミュート中\";" in html, (
        "Level meter mute indicator missing in updateMeter"
    )

