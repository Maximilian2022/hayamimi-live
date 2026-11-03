"""
tests/test_whisper_cuda.py
Whisper CUDA ストリーミング推論エンジンの単体テスト
"""

import os
from unittest.mock import MagicMock, patch
import numpy as np
import pytest

from scripts.whisper_cuda_engine import WhisperCUDAEngine


def test_whisper_cuda_engine_init_mocked():
    """モックを用いた WhisperCUDAEngine の初期化テスト"""
    mock_model = MagicMock()
    with patch("scripts.whisper_cuda_engine.HAVE_FASTER_WHISPER", True), \
         patch("scripts.whisper_cuda_engine.WhisperModel", return_value=mock_model):
        engine = WhisperCUDAEngine(
            model_size_or_path="test-model",
            device="cuda",
            initial_prompt="ゲーム用語, ヴァンピール",
            forced_lang="ja",
        )
        assert engine.forced_lang == "ja"
        assert engine.initial_prompt == "ゲーム用語, ヴァンピール"
        assert engine.engine_name == "whisper_cuda"
        assert engine.dual_confirm is False
        assert engine._punctuate is True


def test_whisper_cuda_engine_transcribe_mocked():
    """transcribe が RoutedASR 互換フォーマットを返却すること"""
    mock_model = MagicMock()
    mock_seg = MagicMock()
    mock_seg.text = "こんにちは世界"
    mock_model.transcribe.return_value = ([mock_seg], None)

    with patch("scripts.whisper_cuda_engine.HAVE_FASTER_WHISPER", True), \
         patch("scripts.whisper_cuda_engine.WhisperModel", return_value=mock_model):
        engine = WhisperCUDAEngine()
        engine.set_replacements([("世界", "早耳Live")])

        samples = np.zeros(16000, dtype=np.float32)
        res = engine.transcribe(samples)

        assert res["text"] == "こんにちは早耳Live"
        assert res["lang"] == "ja"
        assert res["tier"] == "cuda_whisper"
        assert "decode_ms" in res
        assert res["decode_ms"] >= 0.0


def test_whisper_cuda_engine_partial_mocked():
    """partial が高速デコード結果を返却すること"""
    mock_model = MagicMock()
    mock_seg = MagicMock()
    mock_seg.text = "現在発話中"
    mock_model.transcribe.return_value = ([mock_seg], None)

    with patch("scripts.whisper_cuda_engine.HAVE_FASTER_WHISPER", True), \
         patch("scripts.whisper_cuda_engine.WhisperModel", return_value=mock_model):
        engine = WhisperCUDAEngine()
        engine.set_replacements([("発話中", "テスト中")])

        # 0.5 秒分の音声 (lang_hint 付き呼び出しテスト)
        samples = np.zeros(8000, dtype=np.float32)
        text = engine.partial(samples, lang_hint="ja")
        assert text == "現在テスト中"


def test_whisper_cuda_compatibility_layer():
    """RoutedASR との完全互換性 (属性・メソッド群) を検証"""
    mock_model = MagicMock()
    with patch("scripts.whisper_cuda_engine.HAVE_FASTER_WHISPER", True), \
         patch("scripts.whisper_cuda_engine.WhisperModel", return_value=mock_model):
        engine = WhisperCUDAEngine()

        # 属性チェック
        assert hasattr(engine, "dual_confirm")
        assert hasattr(engine, "_punctuate")
        assert hasattr(engine, "lid_switch_confirm")
        assert hasattr(engine, "min_switch_s")
        assert hasattr(engine, "forced_lang")
        assert hasattr(engine, "last_lang")
        assert hasattr(engine, "_replacements")
        assert hasattr(engine, "_itn_overrides")

        # メソッド実行チェック
        engine.set_forced_lang("en")
        assert engine.forced_lang == "en"

        engine.set_dual_confirm(True)
        assert engine.dual_confirm is True

        engine.set_punctuate(False)
        assert engine._punctuate is False

        engine.set_lid_switch_confirm(3)
        assert engine.lid_switch_confirm == 3

        engine.set_min_switch_s(1.5)
        assert engine.min_switch_s == 1.5

        engine.set_dynamic_comment_replacements([("A", "B")])
        assert ("A", "B") in engine._replacements

        engine.reset_session()
        assert engine._last_partial_text == ""

        stats, last_hits = engine.rule_hit_stats()
        assert isinstance(stats, dict)
        assert isinstance(last_hits, dict)


def test_whisper_cuda_close():
    """close でモデルが解放されること"""
    mock_model = MagicMock()
    with patch("scripts.whisper_cuda_engine.WhisperModel", return_value=mock_model):
        engine = WhisperCUDAEngine()
        engine.close()
        assert engine._is_closed is True
        assert engine.model is None

        with pytest.raises(RuntimeError):
            engine.transcribe(np.zeros(16000, dtype=np.float32))


@pytest.mark.parametrize(
    "current_format, disabled, uses_lora",
    [(True, False, True), (True, True, False), (False, False, False)],
)
def test_active_speaker_lora_on_by_default(tmp_path, current_format, disabled, uses_lora):
    """新形式の LoRA は既定で使い、無効化されたものと過去の形式のものは使わない"""
    from scripts.lora_state import set_lora_enabled, write_lora_meta

    ct2_dir = tmp_path / "profiles" / "spk_lora" / "ctranslate2"
    ct2_dir.mkdir(parents=True)
    if current_format:
        write_lora_meta(str(ct2_dir.parent))
    if disabled:
        set_lora_enabled(str(ct2_dir.parent), False)

    with patch("scripts.whisper_cuda_engine.HAVE_FASTER_WHISPER", True), \
         patch("scripts.whisper_cuda_engine.WhisperModel", return_value=MagicMock()), \
         patch("scripts.config_store.get_storage_dir", return_value=str(tmp_path)), \
         patch("scripts.config_store.get_saved_active_speaker_id", return_value="spk"):
        engine = WhisperCUDAEngine(model_size_or_path="base-model")

    assert (engine.model_path == str(ct2_dir)) is uses_lora


def test_explicit_lora_dir_is_honored(tmp_path):
    """--lora-dir の明示指定は実験用に従来どおり読み込む"""
    ct2_dir = tmp_path / "ctranslate2"
    ct2_dir.mkdir()
    with patch("scripts.whisper_cuda_engine.HAVE_FASTER_WHISPER", True), \
         patch("scripts.whisper_cuda_engine.WhisperModel", return_value=MagicMock()):
        engine = WhisperCUDAEngine(lora_dir=str(tmp_path))
    assert engine.model_path == str(ct2_dir)
