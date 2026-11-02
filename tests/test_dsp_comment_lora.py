# -*- coding: utf-8 -*-
"""Tests for Broadcast Audio DSP and OneComme / Comment Sync."""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import urllib.request
import wave
import numpy as np
import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
if os.path.join(_ROOT, "scripts") not in sys.path:
    sys.path.insert(0, os.path.join(_ROOT, "scripts"))

from scripts.audio_dsp import BiquadFilter, BroadcastAudioDSP, get_global_dsp
from scripts.comment_sync import CommentHotwordExtractor, CommentSynchronizer, get_global_comment_sync
from tools.profile_builder.profile_model import SpeakerProfile


# =====================================================================
# Phase 2: Broadcast Audio DSP Tests
# =====================================================================

def test_dsp_highpass_removes_sub_80hz_rumble():
    """Verify 80Hz high-pass filter attenuates 25Hz desk rumble while preserving 1kHz speech."""
    sr = 16000
    t = np.linspace(0, 1.0, sr, endpoint=False, dtype=np.float32)
    rumble_25hz = 0.5 * np.sin(2.0 * np.pi * 25.0 * t)
    voice_1000hz = 0.5 * np.sin(2.0 * np.pi * 1000.0 * t)

    hp = BiquadFilter.highpass(sample_rate=sr, cutoff_hz=80.0)
    out_rumble = hp.process(rumble_25hz)
    hp.reset()
    out_voice = hp.process(voice_1000hz)

    # Ignore first 0.1s transient settling
    rumble_rms_after = float(np.sqrt(np.mean(out_rumble[1600:] ** 2)))
    voice_rms_after = float(np.sqrt(np.mean(out_voice[1600:] ** 2)))
    orig_rms = float(np.sqrt(np.mean(voice_1000hz[1600:] ** 2)))

    # 25Hz should be heavily attenuated (< 20% of original)
    assert rumble_rms_after < orig_rms * 0.20
    # 1000Hz should pass through almost untouched (> 95% of original)
    assert voice_rms_after > orig_rms * 0.95


def test_dsp_agc_boosts_whispers_and_limits_screams():
    """Verify BroadcastAudioDSP boosts quiet speech and prevents clipping on loud screams."""
    dsp = BroadcastAudioDSP(
        enabled=True,
        highpass_enabled=False,
        noise_suppress_enabled=False,
        deesser_enabled=False,
        agc_compressor_enabled=True,
    )
    sr = 16000
    t = np.linspace(0, 0.5, sr // 2, endpoint=False, dtype=np.float32)

    # 1. Quiet whisper (RMS ~ 0.018)
    whisper = (0.025 * np.sin(2.0 * np.pi * 440.0 * t)).astype(np.float32)
    whisper_in_rms = float(np.sqrt(np.mean(whisper ** 2)))
    _ = dsp.process(whisper)
    whisper_out = dsp.process(whisper)
    whisper_out_rms = float(np.sqrt(np.mean(whisper_out ** 2)))
    assert whisper_out_rms > whisper_in_rms * 1.4

    # 2. Loud scream exceeding 0dBFS (amplitude 1.6)
    dsp.reset_state()
    scream = (1.6 * np.sin(2.0 * np.pi * 440.0 * t)).astype(np.float32)
    scream_out = dsp.process(scream)
    # Must never clip above 0.99
    assert float(np.max(np.abs(scream_out))) <= 0.99


def test_dsp_full_chain_performance_and_bypass():
    """Verify full 4-stage DSP chain runs cleanly and respects enabled=False bypass."""
    dsp = BroadcastAudioDSP()
    sr = 16000
    rng = np.random.default_rng(123)
    chunk = (0.05 * rng.standard_normal(sr)).astype(np.float32)

    t0 = time.perf_counter()
    processed = dsp.process(chunk)
    elapsed_ms = (time.perf_counter() - t0) * 1000.0

    assert processed.shape == chunk.shape
    assert processed.dtype == np.float32
    assert not np.allclose(processed, chunk)
    assert elapsed_ms < 50.0

    # Bypass check
    dsp.set_config({"enabled": False})
    bypassed = dsp.process(chunk)
    assert np.array_equal(bypassed, chunk)


# =====================================================================
# Phase 3: OneComme / Comment Sync Tests & Safety Guards (H-1)
# =====================================================================

def test_comment_hotword_extractor_safety_guards_h1():
    """Verify safety guards (H-1): common everyday nouns are never extracted or replaced."""
    extractor = CommentHotwordExtractor()

    # Poisonous comments containing everyday Japanese words
    poison_comments = [
        "死体", "視聴", "機械", "時間ですよ", "意思", "期間",
        "今日は勉強したいんです", "時間がありません", "普通の話",
    ]
    for c in poison_comments:
        extracted = extractor.extract_terms_from_text(c)
        # None of these common words should be extracted
        for surf, reading in extracted:
            assert surf not in ("死体", "視聴", "機械", "時間", "意思", "期間", "勉強", "普通", "話")
            # If any term is extracted, its reading must NOT conflict with common words
            candidates = extractor.build_replacement_candidates(surf, reading)
            assert "したい" not in candidates
            assert "じかん" not in candidates
            assert "きかい" not in candidates

    # Legitimate streamer and game terms MUST still be extracted
    legit_comments = ["渋谷ハル", "葛葉", "クレーバー強い！", "ボセック引いた"]
    extracted_surfaces = []
    for c in legit_comments:
        pairs = extractor.extract_terms_from_text(c)
        extracted_surfaces.extend([p[0] for p in pairs])

    assert any("クレーバー" in s for s in extracted_surfaces)
    if extractor._tokenizer is not None:
        assert any("葛葉" in s or "渋谷" in s or "ハル" in s for s in extracted_surfaces)
    else:
        assert any("ボセック" in s for s in extracted_surfaces)


def test_comment_sync_sliding_window_ttl():
    """Verify terms expire and are purged after their TTL elapsed."""
    syncer = CommentSynchronizer(enabled=True, word_ttl_sec=0.2)
    syncer.ingest_comments(["クレーバー"])

    active_before = syncer.get_dynamic_replacements()
    assert len(active_before) > 0

    time.sleep(0.35)
    active_after = syncer.get_dynamic_replacements()
    assert len(active_after) == 0


def test_subtitle_server_dsp_and_comment_apis_and_singleton_sync_h2(monkeypatch, tmp_path):
    """Verify GET/POST /api/dsp/config and /api/comments/config endpoints sync to global singletons (H-2)."""
    from scripts.subtitle_server import SubtitleServer
    import scripts.audio_dsp as adsp
    import scripts.comment_sync as csync

    monkeypatch.setattr(adsp, "_DSP_CONFIG_PATH", str(tmp_path / "dsp_config.json"))
    monkeypatch.setattr(csync, "_COMMENT_CONFIG_PATH", str(tmp_path / "comment_sync_config.json"))

    srv = SubtitleServer(host="127.0.0.1", port=0)
    srv.start()
    port = srv._httpd.server_address[1]
    try:
        # 1. Update DSP config via API
        req_dsp = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/dsp/config",
            data=json.dumps({"enabled": False, "deesser_enabled": False}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req_dsp, timeout=5) as resp:
            assert resp.status == 200
            d_data = json.loads(resp.read().decode("utf-8"))
            assert d_data["ok"] is True
            assert d_data["enabled"] is False

        # Verify global DSP singleton matches immediately (H-2)
        global_dsp = get_global_dsp()
        assert global_dsp.enabled is False

        # 2. Update Comment Sync config via API
        req_com = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/comments/config",
            data=json.dumps({"enabled": False, "word_ttl_sec": 180}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req_com, timeout=5) as resp:
            assert resp.status == 200
            c_data = json.loads(resp.read().decode("utf-8"))
            assert c_data["ok"] is True
            assert c_data["enabled"] is False

        # Verify global CommentSync singleton matches immediately (H-2)
        global_syncer = get_global_comment_sync()
        assert global_syncer.enabled is False
    finally:
        if srv._httpd:
            srv._httpd.shutdown()
