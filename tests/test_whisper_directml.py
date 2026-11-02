"""
tests/test_whisper_directml.py
Whisper DirectML 推論エンジンの単体テスト (内蔵 GPU / iGPU 専用)
"""

import os
from unittest.mock import MagicMock, patch
import numpy as np
import pytest

from scripts.whisper_dml_engine import WhisperDirectMLEngine


def test_whisper_dml_engine_init_mocked():
    """モックを用いた WhisperDirectMLEngine の初期化テスト"""
    with patch("scripts.whisper_dml_engine.resolve_igpu_directml_device_id", return_value=1), \
         patch("scripts.whisper_dml_engine.AutoProcessor") as mock_proc, \
         patch("scripts.whisper_dml_engine.ORTModelForSpeechSeq2Seq") as mock_ort, \
         patch("scripts.whisper_dml_engine.GenerationConfig"):
        
        engine = WhisperDirectMLEngine(
            device_id=None,
            initial_prompt="AmongUs, MOD",
        )
        assert engine.device_id == 1
        assert engine.provider == "DmlExecutionProvider"
        assert engine.initial_prompt == "AmongUs, MOD"
        assert engine.last_lang == "ja"


def test_whisper_dml_engine_cpu_fallback():
    """iGPU が存在しない環境で CPUExecutionProvider へ自動フォールバックすること"""
    with patch("scripts.whisper_dml_engine.resolve_igpu_directml_device_id", return_value=None), \
         patch("scripts.whisper_dml_engine.AutoProcessor"), \
         patch("scripts.whisper_dml_engine.ORTModelForSpeechSeq2Seq"), \
         patch("scripts.whisper_dml_engine.GenerationConfig"):
        
        engine = WhisperDirectMLEngine(device_id=None)
        assert engine.device_id is None
        assert engine.provider == "CPUExecutionProvider"


def test_whisper_dml_engine_transcribe_mocked():
    """transcribe が RoutedASR と互換性のある辞書を返却すること"""
    with patch("scripts.whisper_dml_engine.resolve_igpu_directml_device_id", return_value=1), \
         patch("scripts.whisper_dml_engine.AutoProcessor") as mock_proc_cls, \
         patch("scripts.whisper_dml_engine.ORTModelForSpeechSeq2Seq") as mock_ort_cls, \
         patch("scripts.whisper_dml_engine.GenerationConfig"):
        
        mock_proc = MagicMock()
        mock_proc.return_value.input_features = MagicMock()
        mock_proc.batch_decode.return_value = ["テスト音声認識結果です"]
        mock_proc_cls.from_pretrained.return_value = mock_proc

        mock_model = MagicMock()
        mock_model.generate.return_value = [[1, 2, 3]]
        mock_ort_cls.from_pretrained.return_value = mock_model

        engine = WhisperDirectMLEngine()
        samples = np.zeros(16000, dtype=np.float32)
        res = engine.transcribe(samples, sample_rate=16000)

        assert res["text"] == "テスト音声認識結果です"
        assert res["lang"] == "ja"
        assert res["tier"] == "dml_whisper"
        assert "decode_ms" in res
        assert res["decode_ms"] >= 0.0


def test_whisper_dml_partial_mocked():
    """partial が高速に暫定テキストを返却すること"""
    with patch("scripts.whisper_dml_engine.resolve_igpu_directml_device_id", return_value=1), \
         patch("scripts.whisper_dml_engine.AutoProcessor") as mock_proc_cls, \
         patch("scripts.whisper_dml_engine.ORTModelForSpeechSeq2Seq") as mock_ort_cls, \
         patch("scripts.whisper_dml_engine.GenerationConfig"):
        
        mock_proc = MagicMock()
        mock_proc.return_value.input_features = MagicMock()
        mock_proc.batch_decode.return_value = ["テスト中..."]
        mock_proc_cls.from_pretrained.return_value = mock_proc

        mock_model = MagicMock()
        mock_model.generate.return_value = [[1, 2]]
        mock_ort_cls.from_pretrained.return_value = mock_model

        engine = WhisperDirectMLEngine()
        samples = np.zeros(16000, dtype=np.float32)
        partial_text = engine.partial(samples, sample_rate=16000)
        assert partial_text == "テスト中..."


def test_whisper_dml_replacements():
    """置換辞書および ITN の動作テスト"""
    with patch("scripts.whisper_dml_engine.resolve_igpu_directml_device_id", return_value=1), \
         patch("scripts.whisper_dml_engine.AutoProcessor"), \
         patch("scripts.whisper_dml_engine.ORTModelForSpeechSeq2Seq"), \
         patch("scripts.whisper_dml_engine.GenerationConfig"):
        
        engine = WhisperDirectMLEngine()
        engine.set_replacements({"アマンガス": "AmongUs", "草": "笑"})
        replaced = engine._replace("アマンガス草")
        assert replaced == "AmongUs笑"


def test_whisper_dml_compatibility_layer():
    """RoutedASR 互換性メソッドが例外なく動作すること"""
    with patch("scripts.whisper_dml_engine.resolve_igpu_directml_device_id", return_value=1), \
         patch("scripts.whisper_dml_engine.AutoProcessor"), \
         patch("scripts.whisper_dml_engine.ORTModelForSpeechSeq2Seq"), \
         patch("scripts.whisper_dml_engine.GenerationConfig"):
        
        engine = WhisperDirectMLEngine()
        engine.set_dual_confirm(True)
        assert engine.dual_confirm is True
        engine.set_punctuate(True)
        engine.set_lid_switch_confirm(2)
        assert engine.lid_switch_confirm == 2
        engine.set_min_switch_s(1.0)
        assert engine.min_switch_s == 1.0
        engine.reset_session()
        assert engine.identify(np.zeros(16000, dtype=np.float32), 16000) == "ja"


def test_whisper_dml_real_inference():
    """実機 DirectML 環境が存在する場合のみ実モデル推論テスト"""
    import onnxruntime as ort
    if "DmlExecutionProvider" not in ort.get_available_providers():
        pytest.skip("DirectML が利用できない環境のためスキップ")

    from scripts.hardware_probe import resolve_igpu_directml_device_id
    dev_id = resolve_igpu_directml_device_id()
    if dev_id is None:
        pytest.skip("利用可能な iGPU が見つからないためスキップ")

    engine = WhisperDirectMLEngine()
    samples = np.sin(2 * np.pi * 440 * np.linspace(0, 1, 16000)).astype(np.float32)
    res = engine.transcribe(samples, sample_rate=16000)
    assert res is not None
    assert "text" in res
    assert res["lang"] == "ja"
    assert res["tier"] == "dml_whisper"
    assert res["decode_ms"] > 0
