# -*- coding: utf-8 -*-
"""Integration tests for subtitle_server profile and key API endpoints."""

import json
import urllib.request
import urllib.error
import pytest

from scripts.subtitle_server import SubtitleServer
from tools.profile_builder.profile_exporter import save_profile
from tools.profile_builder.profile_model import ReplacementRule, SpeakerProfile


@pytest.fixture
def running_server(tmp_path, monkeypatch):
    """Start an in-process SubtitleServer on an ephemeral port."""
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "1")
    # Redirect storage dir to temp
    monkeypatch.setattr("scripts.subtitle_server._get_storage_dir", lambda: str(tmp_path))

    server = SubtitleServer(port=0, host="127.0.0.1")
    # Attach a mock ASR engine
    class MockASR:
        def __init__(self):
            self._replacements = []
        def set_replacements(self, mapping: dict):
            self._replacements = list(mapping.items())

    server.asr = MockASR()
    server.start()
    port = server._httpd.server_address[1]
    base_url = f"http://127.0.0.1:{port}"

    yield base_url, server

    if server._httpd is not None:
        server._httpd.shutdown()


def test_profile_page_served(running_server):
    base_url, _ = running_server
    req = urllib.request.Request(f"{base_url}/profile")
    with urllib.request.urlopen(req) as res:
        assert res.status == 200
        html = res.read().decode("utf-8")
        assert "話者特化プロファイル管理" in html
        assert "Gemini API キーの取得手順" in html
        assert "apiKeyNoticeBanner" in html
        assert "keyGuardMessage" in html


def test_gemini_key_api(running_server):
    base_url, _ = running_server
    # 1. Initial check (unconfigured)
    with urllib.request.urlopen(f"{base_url}/api/gemini-key") as res:
        data = json.loads(res.read().decode("utf-8"))
        assert data["configured"] is False

    # 2. Save key
    payload = json.dumps({"key": "AIzaSyTestKey1234567890"}).encode("utf-8")
    req = urllib.request.Request(
        f"{base_url}/api/gemini-key",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req) as res:
        assert res.status == 200

    # 3. Verify masked key
    with urllib.request.urlopen(f"{base_url}/api/gemini-key") as res:
        data = json.loads(res.read().decode("utf-8"))
        assert data["configured"] is True
        assert "AIzaSy" in data["masked"]
        assert "****" in data["masked"] or "7890" in data["masked"]

    # 4. Delete key
    del_payload = json.dumps({"key": ""}).encode("utf-8")
    req = urllib.request.Request(
        f"{base_url}/api/gemini-key",
        data=del_payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req) as res:
        assert res.status == 200

    with urllib.request.urlopen(f"{base_url}/api/gemini-key") as res:
        data = json.loads(res.read().decode("utf-8"))
        assert data["configured"] is False

    # 5. Validate key endpoint
    val_payload = json.dumps({"key": ""}).encode("utf-8")
    req_val = urllib.request.Request(
        f"{base_url}/api/gemini-key/validate",
        data=val_payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req_val) as res:
        assert res.status == 200
        val_data = json.loads(res.read().decode("utf-8"))
        assert val_data["valid"] is False


def test_profiles_crud_api(running_server):
    base_url, server = running_server

    # 1. Initially empty
    with urllib.request.urlopen(f"{base_url}/api/profiles") as res:
        data = json.loads(res.read().decode("utf-8"))
        assert data == []

    # 2. Import profile
    prof = SpeakerProfile(
        speaker_id="test_speaker",
        display_name="テスト話者",
        replacements=[
            ReplacementRule(pattern="かてないですね", replacement="勝てないですね", reason="テスト"),
        ],
    )
    import_payload = json.dumps({"json_content": prof.model_dump_json()}).encode("utf-8")
    req = urllib.request.Request(
        f"{base_url}/api/profiles/import",
        data=import_payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req) as res:
        assert res.status == 200
        imported = json.loads(res.read().decode("utf-8"))
        assert imported["speaker_id"] == "test_speaker"

    # 3. List profiles
    with urllib.request.urlopen(f"{base_url}/api/profiles") as res:
        data = json.loads(res.read().decode("utf-8"))
        assert len(data) == 1
        assert data[0]["display_name"] == "テスト話者"

    # 4. Apply profile
    apply_payload = json.dumps({"speaker_id": "test_speaker"}).encode("utf-8")
    req = urllib.request.Request(
        f"{base_url}/api/profiles/apply",
        data=apply_payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req) as res:
        assert res.status == 200
        res_data = json.loads(res.read().decode("utf-8"))
        assert res_data["ok"] is True
        assert res_data["rules_count"] == 1

    # Verify ASR engine received replacements
    assert ("かてないですね", "勝てないですね") in server.asr._replacements

    # 5. Export profile
    with urllib.request.urlopen(f"{base_url}/api/profiles/test_speaker/export") as res:
        assert res.status == 200
        exported = json.loads(res.read().decode("utf-8"))
        assert exported["speaker_id"] == "test_speaker"

    # 6. Delete profile
    del_payload = json.dumps({"speaker_id": "test_speaker"}).encode("utf-8")
    req = urllib.request.Request(
        f"{base_url}/api/profiles/delete",
        data=del_payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req) as res:
        assert res.status == 200
        del_res = json.loads(res.read().decode("utf-8"))
        assert del_res["ok"] is True

    # Confirm list is empty
    with urllib.request.urlopen(f"{base_url}/api/profiles") as res:
        data = json.loads(res.read().decode("utf-8"))
        assert data == []


def test_generate_api_requires_gemini_key(running_server):
    """Verify that /api/profiles/generate returns 400 when Gemini API key is missing."""
    base_url, _ = running_server
    payload = json.dumps({
        "url": "https://www.youtube.com/watch?v=no_key_test",
        "display_name": "キーなしテスト話者",
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{base_url}/api/profiles/generate",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(req)
    assert exc_info.value.code == 400
    err_body = json.loads(exc_info.value.read().decode("utf-8"))
    assert "Gemini API キー" in err_body["error"]


def test_generate_api_with_grow_mode(running_server, monkeypatch):
    base_url, _ = running_server
    monkeypatch.setattr("scripts.subtitle_server._get_gemini_key", lambda: "fake_test_key_12345")

    captured_call = {}

    class MockPipeline:
        def __init__(self, api_key=None):
            captured_call["api_key"] = api_key
        def run(self, **kwargs):
            captured_call.update(kwargs)
            return SpeakerProfile(
                speaker_id=kwargs.get("speaker_id") or "test_grown",
                display_name="テスト話者",
            )

    monkeypatch.setattr("tools.profile_builder.pipeline.ProfileBuilderPipeline", MockPipeline)

    payload = json.dumps({
        "url": "https://www.youtube.com/watch?v=grow_test",
        "existing_profile_id": "test_speaker",
        "display_name": "テスト話者",
    }).encode("utf-8")

    req = urllib.request.Request(
        f"{base_url}/api/profiles/generate",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req) as res:
        assert res.status == 202

    # Give thread a moment
    import time
    time.sleep(0.3)

    assert captured_call.get("youtube_url") == "https://www.youtube.com/watch?v=grow_test"
    assert captured_call.get("existing_profile_id") == "test_speaker"


def test_generate_api_with_channel_url(running_server, monkeypatch):
    base_url, _ = running_server
    monkeypatch.setattr("scripts.subtitle_server._get_gemini_key", lambda: "fake_test_key_12345")

    captured_call = {}

    class MockPipeline:
        def __init__(self, api_key=None):
            captured_call["api_key"] = api_key
        def run(self, **kwargs):
            captured_call.update(kwargs)
            return SpeakerProfile(
                speaker_id="channel_speaker",
                display_name="チャンネル話者",
                processed_video_ids=["vid_1", "vid_2"],
            )

    monkeypatch.setattr("tools.profile_builder.pipeline.ProfileBuilderPipeline", MockPipeline)

    payload = json.dumps({
        "url": "https://www.youtube.com/@ValChannel",
        "max_videos": 5,
        "display_name": "チャンネル話者",
    }).encode("utf-8")

    req = urllib.request.Request(
        f"{base_url}/api/profiles/generate",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req) as res:
        assert res.status == 202

    import time
    time.sleep(0.3)


def test_generate_task_status_metadata(running_server, monkeypatch):
    """Verify that /api/profiles/task-status exposes metadata for F5 / browser session recovery."""
    import time
    import threading
    base_url, _ = running_server
    monkeypatch.setattr("scripts.subtitle_server._get_gemini_key", lambda: "fake_test_key_12345")

    block_event = threading.Event()

    class BlockingPipeline:
        def __init__(self, api_key=None):
            pass
        def run(self, **kwargs):
            log_cb = kwargs.get("log_callback")
            if log_cb:
                log_cb("ASR", "テスト音声認識ログを出力中...")
            # Wait until signaled so we can inspect status while running
            block_event.wait(timeout=2.0)
            return SpeakerProfile(
                speaker_id="recover_speaker",
                display_name="復帰テスト話者",
            )

    monkeypatch.setattr("tools.profile_builder.pipeline.ProfileBuilderPipeline", BlockingPipeline)

    payload = json.dumps({
        "url": "https://www.youtube.com/watch?v=recovery_test",
        "display_name": "復帰テスト話者",
        "speaker_id": "recover_speaker",
        "existing_profile_id": "recover_speaker",
    }).encode("utf-8")

    req = urllib.request.Request(
        f"{base_url}/api/profiles/generate",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req) as res:
        assert res.status == 202

    # Give thread a moment to start
    time.sleep(0.05)

    try:
        # Check task-status during execution
        with urllib.request.urlopen(f"{base_url}/api/profiles/task-status") as res:
            assert res.status == 200
            data = json.loads(res.read().decode("utf-8"))
            assert data["status"] == "running"
            assert data["done"] is False
            assert data["url"] == "https://www.youtube.com/watch?v=recovery_test"
            assert data["mode"] == "grow"
            assert data["display_name"] == "復帰テスト話者"
            assert data["speaker_id"] == "recover_speaker"
            # Verify real-time logs
            assert "logs" in data
            assert isinstance(data["logs"], list)
            assert len(data["logs"]) >= 1
            log_levels = [l["level"] for l in data["logs"]]
            assert "INIT" in log_levels or "ASR" in log_levels
            for log_item in data["logs"]:
                assert "time" in log_item
                assert "level" in log_item
                assert "text" in log_item
    finally:
        block_event.set()
        time.sleep(0.2)


def test_active_profile_and_apply_voiceprint(running_server, monkeypatch, tmp_path):
    """Verify /api/profiles/active and /api/profiles/apply return voiceprint status."""
    import numpy as np
    base_url, _ = running_server

    # 1. Initially inactive
    with urllib.request.urlopen(f"{base_url}/api/profiles/active") as res:
        assert res.status == 200
        data = json.loads(res.read().decode("utf-8"))
        assert data["active"] is False

    # 2. Create a profile with voiceprint in storage
    v = np.zeros(512, dtype=np.float32)
    v[0] = 1.0
    prof = SpeakerProfile(
        speaker_id="vp_test_speaker",
        display_name="声紋テスト話者",
        voiceprint=v.tolist(),
        voiceprint_sample_count=15,
        replacements=[ReplacementRule(pattern="かてないとう", replacement="勝てないそう")],
    )

    pdir = str(tmp_path / "profiles")
    save_profile(prof, storage_dir=pdir)
    monkeypatch.setattr("scripts.subtitle_server._get_profiles_dir", lambda: pdir)

    # 3. Apply profile
    apply_payload = json.dumps({"speaker_id": "vp_test_speaker"}).encode("utf-8")
    req = urllib.request.Request(
        f"{base_url}/api/profiles/apply",
        data=apply_payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req) as res:
        assert res.status == 200
        apply_data = json.loads(res.read().decode("utf-8"))
        assert apply_data["ok"] is True
        assert apply_data["has_voiceprint"] is True
        assert apply_data["voiceprint_samples"] == 15
        assert apply_data["display_name"] == "声紋テスト話者"

    # 4. Check active endpoint now reports active profile
    with urllib.request.urlopen(f"{base_url}/api/profiles/active") as res:
        assert res.status == 200
        active_data = json.loads(res.read().decode("utf-8"))
        assert active_data["active"] is True
        assert active_data["speaker_id"] == "vp_test_speaker"
        assert active_data["display_name"] == "声紋テスト話者"
        assert active_data["has_voiceprint"] is True
        assert active_data["voiceprint_samples"] == 15


def test_get_safe_origin_header_sanitization():
    """Verify that _get_safe_origin sanitizes CRLF sequences and only allows valid origins."""
    from scripts.subtitle_server import _get_safe_origin

    # Valid origins
    assert _get_safe_origin("http://localhost:8833", "localhost:8833") == "http://localhost:8833"
    assert _get_safe_origin("http://127.0.0.1:3000", "127.0.0.1:8833") == "http://127.0.0.1:3000"
    assert _get_safe_origin("http://192.168.1.50:8080", "192.168.1.50") == "http://192.168.1.50:8080"

    # CRLF injection attempts must be strictly blocked (return None)
    assert _get_safe_origin("http://localhost\r\nInjected-Header: evil", "localhost") is None
    assert _get_safe_origin("http://localhost\nInjected-Header: evil", "localhost") is None
    assert _get_safe_origin("http://127.0.0.1\0evil", "127.0.0.1") is None

    # Disallowed external origins
    assert _get_safe_origin("http://evil-attacker.com", "localhost:8833") is None
    assert _get_safe_origin("http://10.evil.example", "localhost:8833") is None
    assert _get_safe_origin("http://192.168.evil.example", "localhost:8833") is None
    assert _get_safe_origin("http://172.16.evil.example", "localhost:8833") is None
    assert _get_safe_origin("javascript:alert(1)", "localhost") is None
    assert _get_safe_origin(None, "localhost") is None


def test_maintenance_status_and_sync_stats_api(running_server, monkeypatch, tmp_path):
    """Verify /api/maintenance/status, /api/profiles/sync-stats, and /api/maintenance/run endpoints."""
    base_url, server = running_server

    # 1. GET /api/maintenance/status
    with urllib.request.urlopen(f"{base_url}/api/maintenance/status") as res:
        assert res.status == 200
        st = json.loads(res.read().decode("utf-8"))
        assert "pending_count" in st
        assert "total_pending_videos" in st
        assert "stale_rules_count" in st
        assert "total_stale_rules" in st
        assert "scanning" in st
        assert "status" in st

    # 2. Setup mock profile and ASR stats
    prof = SpeakerProfile(
        speaker_id="stats_test",
        display_name="Stats Speaker",
        replacements=[
            ReplacementRule(pattern="てすと", replacement="テスト", hit_count=0),
        ],
    )
    pdir = str(tmp_path / "profiles")
    save_profile(prof, storage_dir=pdir)
    monkeypatch.setattr("scripts.subtitle_server._get_profiles_dir", lambda: pdir)

    # Attach profile to server and mock asr get_replacement_stats
    server._active_profile = prof
    class MockASR:
        def get_replacement_stats(self):
            return {"てすと": {"hit_count": 5, "last_hit_at": "2026-10-27T12:00:00Z"}}
    server.asr = MockASR()

    # Call POST /api/profiles/sync-stats
    req = urllib.request.Request(
        f"{base_url}/api/profiles/sync-stats",
        data=b"{}",
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req) as res:
        assert res.status == 200
        data = json.loads(res.read().decode("utf-8"))
        assert data["ok"] is True

    # Check updated profile
    assert server._active_profile.replacements[0].hit_count == 5
    assert server._active_profile.replacements[0].last_hit_at == "2026-10-27T12:00:00Z"

    # 3. Call POST /api/maintenance/run (non-blocking, returns 202)
    req_maint = urllib.request.Request(
        f"{base_url}/api/maintenance/run",
        data=b"{}",
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req_maint) as res:
        assert res.status in (200, 202)
        maint_res = json.loads(res.read().decode("utf-8"))
        assert maint_res["ok"] is True
        assert maint_res.get("status") == "running"

    # Wait for background maintenance to complete
    import time
    for _ in range(20):
        with urllib.request.urlopen(f"{base_url}/api/maintenance/status") as res:
            st = json.loads(res.read().decode("utf-8"))
            if not st.get("running"):
                assert st.get("status") in ("completed", "idle")
                break
        time.sleep(0.1)

def test_scan_channels_for_maintenance_archive_replenish(monkeypatch, tmp_path):
    """Verify _scan_channels_for_maintenance replenishes up to 3 total videos from unperformed archives within 180 days."""
    import datetime
    from tools.profile_builder.profile_model import SpeakerProfile
    from tools.profile_builder.profile_exporter import save_profile
    from tools.profile_builder.youtube_fetcher import VideoMetadata
    from scripts.subtitle_server import _scan_channels_for_maintenance, _maintenance_status, _maintenance_lock

    pdir = str(tmp_path / "profiles")
    prof = SpeakerProfile(
        speaker_id="scan_replenish_spk",
        display_name="Scan Replenish Speaker",
        youtube_channel_url="https://youtube.com/@replenish-ch",
        processed_video_ids=["vid_done"],
    )
    save_profile(prof, storage_dir=pdir)
    monkeypatch.setattr("scripts.subtitle_server._get_profiles_dir", lambda: pdir)

    now = datetime.datetime.now(datetime.timezone.utc)
    date_recent = (now - datetime.timedelta(days=2)).strftime("%Y%m%d")
    date_archive1 = (now - datetime.timedelta(days=20)).strftime("%Y%m%d")
    date_archive2 = (now - datetime.timedelta(days=40)).strftime("%Y%m%d")
    date_old = (now - datetime.timedelta(days=210)).strftime("%Y%m%d")

    # Mock fetch_channel_videos (returns 1 new video)
    def mock_fetch_channel_videos(self, channel_url, limit=3, **kwargs):
        return [{
            "id": "vid_new1", "title": "New Video 1", "url": "https://youtube.com/watch?v=vid_new1",
            "channel": "Ch", "duration": 600, "view_count": 100,
        }]

    # Mock fetch_maintenance_candidates (returns up to target_count archive videos within 180 days)
    def mock_fetch_maintenance_candidates(self, channel_url, target_count=3, max_age_days=180, exclude_video_ids=None, **kwargs):
        assert target_count == 2  # 3 - 1 new video = 2 needed
        assert "vid_new1" in exclude_video_ids
        assert "vid_done" in exclude_video_ids
        return [
            {"id": "vid_arc1", "title": "Archive 1", "url": "https://youtube.com/watch?v=vid_arc1", "channel": "Ch", "is_archive": True},
            {"id": "vid_arc2", "title": "Archive 2", "url": "https://youtube.com/watch?v=vid_arc2", "channel": "Ch", "is_archive": True},
        ]

    from tools.profile_builder.youtube_fetcher import YouTubeFetcher
    monkeypatch.setattr(YouTubeFetcher, "fetch_channel_videos", mock_fetch_channel_videos)
    monkeypatch.setattr(YouTubeFetcher, "fetch_maintenance_candidates", mock_fetch_maintenance_candidates)

    class FakeServer:
        pass

    # Trigger scan
    _scan_channels_for_maintenance(FakeServer())

    # Wait for thread to finish
    import time
    for _ in range(30):
        with _maintenance_lock:
            if not _maintenance_status["scanning"]:
                break
        time.sleep(0.1)

    with _maintenance_lock:
        assert _maintenance_status["pending_count"] == 3
        pending = _maintenance_status["pending_videos"]
        assert len(pending) == 3
        # 1 new video + 2 replenished archives
        assert pending[0]["id"] == "vid_new1"
        assert pending[0].get("is_archive") is False
        assert pending[1]["id"] == "vid_arc1"
        assert pending[1].get("is_archive") is True
        assert pending[2]["id"] == "vid_arc2"
        assert pending[2].get("is_archive") is True



def test_system_mode_api(running_server):
    base_url, _ = running_server
    req = urllib.request.Request(f"{base_url}/api/system/mode")
    with urllib.request.urlopen(req) as res:
        assert res.status == 200
        data = json.loads(res.read().decode("utf-8"))
        assert "device" in data
        assert "is_gpu_mode" in data
        assert "engine" in data


def test_voiceprint_manual_record_api(running_server, monkeypatch):
    base_url, server = running_server
    from tools.profile_builder.profile_exporter import save_profile
    from tools.profile_builder.profile_model import SpeakerProfile
    from scripts.subtitle_server import _get_profiles_dir
    pdir = _get_profiles_dir()
    p = SpeakerProfile(
        speaker_id="rec_spk",
        display_name="録音テスト話者",
        replacements=[],
    )
    save_profile(p, storage_dir=pdir)

    class MockExtractor:
        is_available = True
        def calibrate_threshold(self, wav_path):
            return 0.6

        def extract_voiceprint(self, wav_path):
            return [0.1] * 512, 10

    monkeypatch.setattr("tools.profile_builder.voiceprint_extractor.VoiceprintExtractor", MockExtractor)

    import base64
    dummy_wav_b64 = base64.b64encode(b"RIFF$   WAVEfmt     data    ").decode("utf-8")
    payload = json.dumps({"audio_base64": dummy_wav_b64}).encode("utf-8")
    req = urllib.request.Request(
        f"{base_url}/api/profiles/rec_spk/voiceprint/record",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req) as res:
        assert res.status == 200
        data = json.loads(res.read().decode("utf-8"))
        assert data["ok"] is True
        assert data["sample_count"] == 10
        assert "512次元" in data["message"]

    with urllib.request.urlopen(f"{base_url}/api/profiles") as res:
        profs = json.loads(res.read().decode("utf-8"))
        target = next((x for x in profs if x["speaker_id"] == "rec_spk"), None)
        assert target is not None
        assert target["voiceprint"] is not None
        assert len(target["voiceprint"]) == 512
        assert "has_lora" in target


def test_profile_page_modals_use_defined_css_classes():
    """未定義のクラスだとダイアログが隠れず、ページの下に出続ける"""
    import pathlib
    import re

    html = pathlib.Path("scripts/profile_page.html").read_text(encoding="utf-8")
    style = html[: html.index("</style>")]
    for block in re.findall(r'<div id="\w+Modal" class="([\w-]+)">\s*<div class="([\w-]+)"', html):
        for name in block:
            assert f".{name}" in style, name
