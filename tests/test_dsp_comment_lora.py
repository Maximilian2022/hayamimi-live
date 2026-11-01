# -*- coding: utf-8 -*-
"""Tests for Phase 2 (Broadcast Audio DSP), Phase 3 (OneComme / Comment Sync), and Phase 4 (Speaker LoRA Trainer)."""

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

from scripts.audio_dsp import BiquadFilter, BroadcastAudioDSP
from scripts.comment_sync import CommentHotwordExtractor, CommentSynchronizer
from tools.profile_builder.lora_trainer import SpeakerLoRATrainer, extract_fbank_features
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
    # Run two chunks so smooth gain converges
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
    # 1 second of 16kHz audio should process in well under 50ms even on slow CI
    assert elapsed_ms < 50.0

    # Bypass when disabled
    dsp.set_config({"enabled": False})
    bypassed = dsp.process(chunk)
    assert np.array_equal(bypassed, chunk)


# =====================================================================
# Phase 3: OneComme & Comment Sync Tests
# =====================================================================

def test_comment_hotword_extractor_filters_greetings_and_builds_candidates():
    """Verify CommentHotwordExtractor extracts nouns/katakana and ignores stream greetings."""
    ext = CommentHotwordExtractor()
    terms = ext.extract_terms_from_text("わこつです！今日のクレーバーとストグラ楽しみｗｗｗ 88888")
    surfaces = [s for s, _ in terms]

    assert "クレーバー" in surfaces
    assert "わこつ" not in surfaces
    assert "ｗｗｗ" not in surfaces

    # Check vowel-shortened and hiragana candidates for "クレーバー"
    cands = ext.build_replacement_candidates("クレーバー", "クレーバー")
    assert cands.get("クレバー") == "クレーバー"
    assert cands.get("くれーばー") == "クレーバー"


def test_comment_synchronizer_file_and_asr_integration(tmp_path):
    """Verify CommentSynchronizer reads comment.txt and updates RoutedASR without overwriting user rules."""
    from scripts.asr_engine import RoutedASR

    comment_file = tmp_path / "comment.txt"
    comment_file.write_text("初見です！\nアルティメット強い！\nクレーバーうまい！\n", encoding="utf-8")

    engine = RoutedASR.__new__(RoutedASR)
    engine._rule_hit_counts = {}
    engine._rule_last_hit = {}
    engine._stats_lock = threading.Lock()
    # User profile rule takes priority
    engine.set_replacements({"ウルト": "アルティメットスキル"})

    syncer = CommentSynchronizer(
        enabled=True,
        onecomme_url="http://127.0.0.1:59999/invalid",
        comment_file_path=str(comment_file),
        word_ttl_sec=60.0,
        on_dictionary_updated=lambda m: engine.set_dynamic_comment_replacements(m),
    )

    count = syncer.poll_once()
    assert count > 0
    status = syncer.get_status()
    assert status["connected_source"] == "comment_file"
    assert "クレーバー" in status["active_words"]

    # Verify ASR applies both user rule AND dynamic comment rule
    out = engine._replace("ウルトでクレバーを撃つ")
    assert "アルティメットスキル" in out
    assert "クレーバー" in out


# =====================================================================
# Phase 4: Speaker LoRA Trainer & Continual Growth Tests
# =====================================================================

def _create_dummy_speech_wav(path: str, freq_hz: float = 220.0, duration_sec: float = 1.5):
    sr = 16000
    t = np.linspace(0, duration_sec, int(sr * duration_sec), endpoint=False)
    # Multi-harmonic speech-like signal
    sig = 0.25 * np.sin(2 * np.pi * freq_hz * t) + 0.15 * np.sin(2 * np.pi * (freq_hz * 2.5) * t)
    pcm16 = np.clip(sig * 32767, -32768, 32767).astype(np.int16)
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(pcm16.tobytes())


def test_speaker_lora_trainer_initial_and_continual_growth(tmp_path):
    """Verify SpeakerLoRATrainer creates an adapter on first run and warm-start grows it on subsequent runs."""
    wav1 = str(tmp_path / "stream1.wav")
    wav2 = str(tmp_path / "stream2.wav")
    _create_dummy_speech_wav(wav1, freq_hz=200.0)
    _create_dummy_speech_wav(wav2, freq_hz=320.0)

    trainer = SpeakerLoRATrainer(rank=8, alpha=16.0, storage_dir=str(tmp_path / "lora"))

    # 1. Initial creation (Video 1)
    adapter_path, count1 = trainer.train_or_grow(
        speaker_id="val_streamer",
        wav_path=wav1,
        voiceprint=[0.05] * 512,
        existing_video_count=0,
    )
    assert os.path.exists(adapter_path)
    assert count1 == 1

    with np.load(adapter_path) as data1:
        assert data1["lora_A"].shape == (80, 8)
        assert data1["lora_B"].shape == (8, 80)
        assert int(data1["trained_video_count"]) == 1
        first_A = data1["lora_A"].copy()

    # 2. Continual growth / maintenance (Video 2)
    adapter_path2, count2 = trainer.train_or_grow(
        speaker_id="val_streamer",
        wav_path=wav2,
        voiceprint=[0.05] * 512,
        existing_adapter_path=adapter_path,
        existing_video_count=count1,
    )
    assert adapter_path2 == adapter_path
    assert count2 == 2

    with np.load(adapter_path2) as data2:
        assert int(data2["trained_video_count"]) == 2
        second_A = data2["lora_A"].copy()
        # Weights should have evolved via warm-start blending
        assert not np.allclose(first_A, second_A)


def test_subtitle_server_dsp_and_comment_apis(monkeypatch, tmp_path):
    """Verify GET/POST /api/dsp/config and /api/comments/config endpoints on SubtitleServer."""
    from scripts.subtitle_server import SubtitleServer
    import scripts.audio_dsp as adsp
    import scripts.comment_sync as csync

    monkeypatch.setattr(adsp, "_DSP_CONFIG_PATH", str(tmp_path / "dsp_config.json"))
    monkeypatch.setattr(csync, "_COMMENT_CONFIG_PATH", str(tmp_path / "comment_sync_config.json"))

    srv = SubtitleServer(host="127.0.0.1", port=0)
    srv.start()
    port = srv._httpd.server_address[1]
    try:
        # 1. Update DSP config
        req_dsp = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/dsp/config",
            data=json.dumps({"enabled": True, "deesser_enabled": False}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req_dsp, timeout=5) as resp:
            assert resp.status == 200
            d_data = json.loads(resp.read().decode("utf-8"))
            assert d_data["ok"] is True
            assert d_data["deesser_enabled"] is False

        # 2. Update Comment Sync config
        req_com = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/comments/config",
            data=json.dumps({"enabled": True, "word_ttl_sec": 180}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req_com, timeout=5) as resp:
            assert resp.status == 200
            c_data = json.loads(resp.read().decode("utf-8"))
            assert c_data["ok"] is True
            assert c_data["enabled"] is True
            assert c_data["word_ttl_sec"] == 180.0
    finally:
        if srv._httpd:
            srv._httpd.shutdown()
