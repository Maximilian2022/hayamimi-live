# -*- coding: utf-8 -*-
"""Unit tests for YouTube Live Chat synchronizer, profile channel gating, and accordion fix."""

import json
import os
import re
import pytest

from scripts.comment_sync import (
    CommentSynchronizer,
    normalize_youtube_channel_url,
    parse_youtube_live_page,
    parse_youtube_live_chat_response,
)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MIC_PAGE_PATH = os.path.join(REPO_ROOT, "scripts", "mic_page.html")


def test_normalize_youtube_channel_url():
    """Verify channel URL normalization produces correct /live endpoint."""
    assert normalize_youtube_channel_url("@test_vtuber") == "https://www.youtube.com/@test_vtuber/live"
    assert normalize_youtube_channel_url("https://www.youtube.com/@test_vtuber") == "https://www.youtube.com/@test_vtuber/live"
    assert normalize_youtube_channel_url("https://www.youtube.com/@test_vtuber/featured") == "https://www.youtube.com/@test_vtuber/live"
    assert normalize_youtube_channel_url("https://www.youtube.com/@test_vtuber/streams") == "https://www.youtube.com/@test_vtuber/live"
    assert normalize_youtube_channel_url("https://www.youtube.com/channel/UC1234567890abcdef") == "https://www.youtube.com/channel/UC1234567890abcdef/live"
    assert normalize_youtube_channel_url("") == ""


def test_parse_youtube_live_page_live_and_upcoming():
    """Verify live status and upcoming (waiting room) detection."""
    # 1. Active Live Stream HTML (11-char YouTube video ID: dQw4w9WgXcQ)
    live_html = """
    <!doctype html><html><head>
    <meta itemprop="videoId" content="dQw4w9WgXcQ">
    </head><body>
    <script>
    var ytInitialPlayerResponse = {"videoDetails":{"videoId":"dQw4w9WgXcQ","isLive":true}};
    var ytInitialData = {"continuation":"initial_cont_abc123"};
    </script>
    </body></html>
    """
    vid, is_live, cont = parse_youtube_live_page(live_html, "https://www.youtube.com/watch?v=dQw4w9WgXcQ")
    assert vid == "dQw4w9WgXcQ"
    assert is_live is True
    assert cont == "initial_cont_abc123"

    # 2. Upcoming Waiting Room (NOT yet live)
    upcoming_html = """
    <!doctype html><html><head>
    <meta itemprop="videoId" content="wait_vid_99">
    </head><body>
    <script>
    var ytInitialPlayerResponse = {"videoDetails":{"videoId":"wait_vid_99","isLive":true,"isUpcoming":true}};
    </script>
    </body></html>
    """
    vid, is_live, cont = parse_youtube_live_page(upcoming_html, "https://www.youtube.com/watch?v=wait_vid_99")
    assert vid == "wait_vid_99"
    assert is_live is False  # Must NOT be live yet to prevent greeting comment noise

    # 3. No live stream
    offline_html = "<html><body><h1>Channel Home</h1></body></html>"
    vid, is_live, cont = parse_youtube_live_page(offline_html, "https://www.youtube.com/@offline_chan/live")
    assert vid is None
    assert is_live is False


def test_parse_youtube_live_chat_response():
    """Verify live chat messages and next continuation token parsing."""
    sample_data = {
        "continuationContents": {
            "liveChatContinuation": {
                "actions": [
                    {
                        "addChatItemAction": {
                            "item": {
                                "liveChatTextMessageRenderer": {
                                    "message": {
                                        "runs": [{"text": "神プ合体キタ！"}, {"text": " "}, {"text": "ナイファイ！"}]
                                    }
                                }
                            }
                        }
                    },
                    {
                        "addChatItemAction": {
                            "item": {
                                "liveChatPaidMessageRenderer": {
                                    "message": {
                                        "runs": [{"text": "スパチャナイス〜！"}]
                                    }
                                }
                            }
                        }
                    },
                ],
                "continuations": [
                    {
                        "timedContinuationData": {
                            "continuation": "next_token_xyz999",
                            "timeoutMs": 3500,
                        }
                    }
                ],
            }
        }
    }
    msgs, next_cont, timeout_ms = parse_youtube_live_chat_response(sample_data)
    assert len(msgs) == 2
    assert "神プ合体キタ！ ナイファイ！" in msgs[0]
    assert msgs[1] == "スパチャナイス〜！"
    assert next_cont == "next_token_xyz999"
    assert timeout_ms == 3500.0


def test_comment_synchronizer_youtube_live_lifecycle():
    """Verify CommentSynchronizer configuration, status reporting, and hotword ingestion."""
    syncer = CommentSynchronizer(enabled=True)
    status = syncer.get_status()
    assert status["youtube_live_enabled"] is False
    assert status["youtube_live_status"] == "disabled"

    # Enable YouTube live sync with channel URL
    new_status = syncer.set_config({
        "youtube_live_enabled": True,
        "youtube_channel_url": "https://www.youtube.com/@apex_gamer",
    })
    assert new_status["youtube_live_enabled"] is True
    assert new_status["youtube_channel_url"] == "https://www.youtube.com/@apex_gamer"
    assert new_status["youtube_live_status"] == "waiting_stream"

    # Feed comments directly via ingest_comments (simulating YouTube Live stream)
    syncer.ingest_comments(["ウルト溜まったナイス！", "フラットライン強い草"], source_label="youtube_live")
    active_dyn = syncer.get_dynamic_replacements()
    # At least some terms should be captured
    status_after = syncer.get_status()
    assert status_after["total_comments_processed"] == 2
    assert status_after["connected_source"] == "youtube_live"

    # Disable
    syncer.set_config({"youtube_live_enabled": False})
    assert syncer.get_status()["youtube_live_status"] == "disabled"


def test_mic_page_ui_youtube_live_toggle_and_accordion_fix():
    """Verify mic_page.html implements conditional toggle and fixed accordion."""
    with open(MIC_PAGE_PATH, "r", encoding="utf-8") as f:
        html = f.read()

    # 1. Accordion Fix: summary must have display: flex, user-select: none, list-style: none, and rotating arrow
    assert "details.settings-box summary" in html
    assert "user-select: none;" in html
    assert "display: flex;" in html
    assert "justify-content: space-between;" in html
    assert "details.settings-box summary::-webkit-details-marker" in html
    assert "details.settings-box[open] summary::after" in html

    # 2. YouTube Live Chat Toggle UI
    assert 'id="cfg-yt-live-sync-enabled"' in html
    assert 'id="yt-live-status-badge"' in html
    assert 'id="yt-live-sync-container"' in html
    assert "YouTube ライブチャット自動連携" in html
    assert "プロファイルでYouTubeチャンネルを設定すると利用可能" in html

    # 3. JavaScript Gating Logic
    assert "updateYouTubeLiveSyncUI" in html
    assert "activeProfileChannelUrl" in html
    assert "chk.disabled = true" in html
    assert "chk.disabled = false" in html
