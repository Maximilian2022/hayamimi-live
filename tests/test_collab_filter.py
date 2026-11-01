# -*- coding: utf-8 -*-
"""Tests for collaboration / guest archive exclusion filter (Phase 1)."""

from __future__ import annotations

import json
import urllib.request
import pytest

from tools.profile_builder.profile_model import DEFAULT_COLLAB_KEYWORDS, SpeakerProfile
from tools.profile_builder.youtube_fetcher import (
    VideoMetadata,
    YouTubeFetcher,
    is_collab_or_guest_video,
    is_collab_title,
)
from tools.profile_builder.pipeline import CollabVideoSkippedError, ProfileBuilderPipeline


def test_speaker_profile_default_collab_settings():
    """Verify new SpeakerProfile has collab_filter_enabled=True and default keywords populated."""
    prof = SpeakerProfile(speaker_id="test_spk", display_name="Test Speaker")
    assert prof.collab_filter_enabled is True
    assert isinstance(prof.collab_exclude_keywords, list)
    assert "コラボ" in prof.collab_exclude_keywords
    assert "collab" in prof.collab_exclude_keywords
    assert "w/" in prof.collab_exclude_keywords
    assert "参加型" in prof.collab_exclude_keywords
    assert len(prof.collab_exclude_keywords) == len(DEFAULT_COLLAB_KEYWORDS)


def test_is_collab_title_detection_and_boundary_safety():
    """Verify title collab detection catches collab keywords without false positives on English substrings."""
    # Positive cases
    assert is_collab_title("【コラボ】APEXやるぞ！")[0] is True
    assert is_collab_title("Valorant w/ 葛葉さん")[0] is True
    assert is_collab_title("雑談 with 〇〇")[0] is True
    assert is_collab_title("【凸待ち】誕生日記念配信")[0] is True
    assert is_collab_title("カスタム大会本番！")[0] is True
    assert is_collab_title("リスナー参加型マリオカート")[0] is True

    # Negative cases (Solo streams and English word boundary safety)
    assert is_collab_title("【ソロ】ランクマッチ修行 #ApexLegends")[0] is False
    assert is_collab_title("Playing without HUD challenge")[0] is False
    assert is_collab_title("Nintendo Switch 新作ゲーム遊ぶ")[0] is False
    assert is_collab_title("Within the dark forest - horror solo")[0] is False

    # Disabled filter
    assert is_collab_title("【コラボ】APEXやるぞ！", filter_enabled=False)[0] is False


def test_user_custom_keywords_add_and_remove():
    """Verify user can remove default keywords (e.g. '参加型') or add custom keywords."""
    # Default rejects '参加型'
    assert is_collab_title("【参加型】モンハンやる")[0] is True

    # User removes '参加型' and adds '企画'
    custom_kws = [k for k in DEFAULT_COLLAB_KEYWORDS if k != "参加型"] + ["企画"]
    assert is_collab_title("【参加型】モンハンやる", exclude_keywords=custom_kws)[0] is False
    assert is_collab_title("【特別企画】耐久配信", exclude_keywords=custom_kws)[0] is True


def test_is_collab_or_guest_video_mentions_and_whitelist():
    """Verify description mentions, headers, hashtags, and self/credit whitelist."""
    # 1. Solo stream with own channel link and illustrator credit -> Should NOT be excluded
    solo_meta = VideoMetadata(
        video_id="solo1234567",
        title="【雑談】最近の出来事話す #雑談",
        channel="ヴァル閣下",
        channel_url="https://www.youtube.com/@ValChannel",
        description=(
            "今日もソロで雑談します！\n"
            "チャンネル登録よろしく: https://www.youtube.com/@ValChannel\n"
            "Twitter: @ValChannel_Sub\n"
            "ママ / イラストレーター: @illust_mama_sensei\n"
            "連絡先: contact@example.com\n"
        ),
        duration_sec=3600.0,
        tags=["雑談"],
    )
    is_c, reason = is_collab_or_guest_video(
        solo_meta,
        own_channel_url="https://www.youtube.com/@ValChannel",
        own_channel_name="ヴァル閣下",
    )
    assert is_c is False, f"Unexpected exclusion: {reason}"

    # 2. Stream with another streamer's @mention in description -> Should be excluded
    mention_meta = VideoMetadata(
        video_id="ment1234567",
        title="深夜のAPEXランク",
        channel="ヴァル閣下",
        channel_url="https://www.youtube.com/@ValChannel",
        description="今日は @OtherStreamer_JP さんといっしょに回ります！",
        duration_sec=3600.0,
        tags=[],
    )
    is_c2, reason2 = is_collab_or_guest_video(
        mention_meta,
        own_channel_url="https://www.youtube.com/@ValChannel",
        own_channel_name="ヴァル閣下",
    )
    assert is_c2 is True
    assert "@OtherStreamer_JP" in reason2

    # 3. Stream with participant section header -> Should be excluded
    header_meta = VideoMetadata(
        video_id="head1234567",
        title="マイクラ肝試し",
        channel="ヴァル閣下",
        description="【参加者】\n・〇〇\n・△△",
        duration_sec=3600.0,
        tags=[],
    )
    is_c3, reason3 = is_collab_or_guest_video(header_meta)
    assert is_c3 is True
    assert "参加者" in reason3

    # 4. Stream with official game/publisher/music channels in description -> Should NOT be excluded
    game_promo_meta = VideoMetadata(
        video_id="game_promo_001",
        title="【 #ヴァンピール 】2000人登録記念！ガチャ配信 #18 【 #vampir 】",
        channel="ヴァル閣下",
        channel_url="https://www.youtube.com/@ValChannel",
        description=(
            "クラス：アカシャ\n"
            "サーバー：フランツ01\n"
            "プロモーション：ヴァンピール(VAMPIR)\n"
            "▼公式YouTubeチャンネル\n"
            "https://www.youtube.com/@VAMPIRofficial_JP\n"
            "▼公式X\n"
            "https://x.com/VAMPIR_JP\n"
            "楽曲提供元: TheFatRat / @thefatrat\n"
            "#ヴァンピール #VAMPIR #PR\n"
        ),
        duration_sec=3600.0,
        tags=["ヴァンピール", "VAMPIR"],
    )
    is_c4, reason4 = is_collab_or_guest_video(
        game_promo_meta,
        own_channel_url="https://www.youtube.com/@ValChannel",
        own_channel_name="ヴァル閣下",
    )
    assert is_c4 is False, f"Game official channel should NOT be excluded: {reason4}"


def test_fetch_channel_videos_skips_collab_titles(monkeypatch):
    """Verify fetch_channel_videos filters out collab titles and records skipped IDs."""
    fetcher = YouTubeFetcher()

    fake_entries = [
        {"id": "vid_solo_001", "title": "【ソロ】スト6 ランクマ", "duration": 1800, "view_count": 1000},
        {"id": "vid_col_002", "title": "【コラボ】スト6 対決 w/ 〇〇", "duration": 2400, "view_count": 5000},
        {"id": "vid_solo_003", "title": "雑談配信 #ストグラ", "duration": 3600, "view_count": 2000},
    ]

    class FakeProc:
        returncode = 0
        stdout = "\n".join(json.dumps(e) for e in fake_entries)
        stderr = ""

    monkeypatch.setattr(fetcher, "_run_ytdl_command", lambda *a, **kw: FakeProc())

    skipped_out = []
    videos = fetcher.fetch_channel_videos(
        channel_url="https://www.youtube.com/@ValChannel",
        limit=5,
        filter_collab=True,
        skipped_video_ids_out=skipped_out,
    )
    assert len(videos) == 2
    assert [v["id"] for v in videos] == ["vid_solo_003", "vid_solo_001"]
    assert skipped_out == ["vid_col_002"]


def test_pipeline_skips_collab_video_in_batch(monkeypatch, tmp_path):
    """Verify pipeline skips video detected as collab in metadata phase without aborting batch."""
    import tools.profile_builder.profile_exporter as pe
    monkeypatch.setattr(pe, "DEFAULT_PROFILES_DIR", str(tmp_path))
    monkeypatch.setattr("tools.profile_builder.pipeline.save_profile", lambda p: pe.save_profile(p, str(tmp_path)))
    monkeypatch.setattr("tools.profile_builder.pipeline.load_profile", lambda sid: pe.load_profile(sid, str(tmp_path)))

    pipeline = ProfileBuilderPipeline(api_key="fake-key")

    monkeypatch.setattr(
        pipeline.fetcher,
        "fetch_channel_videos",
        lambda *a, **kw: [
            {"id": "v_collab", "url": "https://www.youtube.com/watch?v=v_collab111", "title": "APEXランク", "channel": "Val"},
            {"id": "v_solo", "url": "https://www.youtube.com/watch?v=v_solo22222", "title": "ソロ雑談", "channel": "Val"},
        ],
    )

    def mock_fetch_meta(url, *a, **kw):
        if "v_collab" in url:
            # Title looks normal, but description has another streamer's mention
            return VideoMetadata(
                video_id="v_collab111",
                title="APEXランク",
                channel="Val",
                description="今日は @GuestPlayer99 とデュオ！",
                duration_sec=1800.0,
                tags=[],
            )
        return VideoMetadata(
            video_id="v_solo22222",
            title="ソロ雑談",
            channel="Val",
            description="ひとりでゆっくり話します",
            duration_sec=1800.0,
            tags=["#雑談"],
        )

    monkeypatch.setattr(pipeline.fetcher, "fetch_metadata", mock_fetch_meta)
    monkeypatch.setattr(pipeline.fetcher, "fetch_subtitles", lambda *a, **kw: [])
    monkeypatch.setattr(pipeline.fetcher, "find_best_speech_section", lambda *a, **kw: (0.0, 60.0, 1.0))
    monkeypatch.setattr(pipeline.fetcher, "download_audio_section", lambda *a, **kw: None)

    class FakeTransRes:
        full_text = "テスト発話"
        segments = ["テスト発話"]

    monkeypatch.setattr(
        "tools.profile_builder.pipeline.LocalTranscriber.transcribe_file",
        lambda self, **kw: FakeTransRes(),
    )
    monkeypatch.setattr(
        "tools.profile_builder.pipeline.VoiceprintExtractor.extract_voiceprint",
        lambda self, **kw: ([0.1] * 512, 5),
    )

    def mock_analyze(self, **kw):
        return SpeakerProfile(
            speaker_id=kw["speaker_id"],
            display_name=kw["display_name"],
            source_video_url=kw["metadata"].url,
            processed_video_ids=[kw["metadata"].video_id],
        )

    monkeypatch.setattr("tools.profile_builder.pipeline.GeminiProfileAnalyzer.analyze", mock_analyze)

    prof = pipeline.run(
        youtube_url="https://www.youtube.com/@ValChannel",
        speaker_id="val_spk",
        display_name="Val",
    )
    assert prof is not None
    assert "v_solo22222" in prof.processed_video_ids
    assert "v_collab111" not in prof.processed_video_ids


def test_subtitle_server_update_collab_settings_api(monkeypatch, tmp_path):
    """Verify POST /api/profiles/update-collab-settings updates and persists collab settings."""
    from scripts.subtitle_server import SubtitleServer
    from tools.profile_builder.profile_exporter import load_profile, save_profile

    monkeypatch.setattr("scripts.subtitle_server._get_profiles_dir", lambda: str(tmp_path))

    prof = SpeakerProfile(speaker_id="test_col", display_name="Test Collab")
    save_profile(prof, str(tmp_path))

    srv = SubtitleServer(host="127.0.0.1", port=0)
    srv.start()
    port = srv._httpd.server_address[1]
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/profiles/update-collab-settings",
            data=json.dumps({
                "speaker_id": "test_col",
                "collab_filter_enabled": False,
                "collab_exclude_keywords": ["コラボ", "カスタム枠"],
            }).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            assert resp.status == 200
            data = json.loads(resp.read().decode("utf-8"))
            assert data["ok"] is True
            assert data["collab_filter_enabled"] is False
            assert data["collab_exclude_keywords"] == ["コラボ", "カスタム枠"]

        reloaded = load_profile("test_col", str(tmp_path))
        assert reloaded.collab_filter_enabled is False
        assert reloaded.collab_exclude_keywords == ["コラボ", "カスタム枠"]
    finally:
        if srv._httpd:
            srv._httpd.shutdown()
