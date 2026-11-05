"""日本語（Haas-Ja）の選択と推論処理の単体テスト"""
import os
import pathlib
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

pytest.importorskip("onnxruntime")
pytest.importorskip("sentencepiece")

import asr_engine as ae  # noqa: E402
import haas_ja  # noqa: E402
import subtitle_server  # noqa: E402


def test_log_mel_shape_and_padding():
    mel, valid = haas_ja.log_mel(np.random.default_rng(0).normal(0, 0.1, 16000).astype(np.float32))
    assert valid == 100
    assert mel.shape[:2] == (1, 128)
    assert mel.shape[2] % 16 == 0
    assert np.all(mel[0, :, valid + 1 :] == 0)


def test_filterbank_covers_128_bins():
    assert haas_ja._FILTERBANK.shape == (128, 257)
    assert haas_ja._FILTERBANK.min() >= 0
    assert np.all(haas_ja._FILTERBANK.sum(axis=1) > 0)


def test_greedy_tdt_emits_tokens_then_advances():
    plan = iter([(5, 0), (7, 2), (haas_ja._BLANK, 3)])
    seen = []

    def step(frame, last, s1, s2):
        token, duration = next(plan)
        seen.append(last)
        logits = np.zeros(haas_ja._BLANK + 1 + haas_ja._DURATIONS)
        logits[token] = 1.0
        logits[haas_ja._BLANK + 1 + duration] = 1.0
        return logits, s1, s2

    assert haas_ja.greedy_tdt(step, np.zeros((1, 1024, 5), np.float32), 5) == [5, 7]
    assert seen == [haas_ja._BLANK, 5, 7]


def test_long_audio_is_split_without_losing_samples():
    audio = np.ones(16000 * 70, dtype=np.float32)
    parts = haas_ja.split_at_quiet_points(audio, 16000 * 29)
    assert sum(map(len, parts)) == len(audio)
    assert all(len(p) <= 16000 * 29 for p in parts)


def _engine(device="cpu"):
    engine = ae.RoutedASR.__new__(ae.RoutedASR)
    engine._closed = False
    engine.forced_lang = None
    engine.device = device
    engine._ja_model = "rz"
    engine._models = {}
    engine._spawn_bg = MagicMock()
    engine.reset_session = MagicMock()
    return engine


def test_selecting_haas_keeps_japanese_and_preloads():
    engine = _engine()
    engine.set_forced_lang(ae.LANG_JA_HAAS)
    assert engine.forced_lang == "ja"
    assert engine.selected_lang == ae.LANG_JA_HAAS
    engine._spawn_bg.assert_called_once()
    with patch.object(ae.RoutedASR, "_get_with_fallback", return_value=("rec", "haas")) as get:
        assert engine._route("ja") == ("rec", "haas")
    get.assert_called_once_with("haas")


@pytest.mark.parametrize("lang, forced", [("ja", "ja"), (None, None), ("en", "en")])
def test_other_selections_do_not_use_haas(lang, forced):
    engine = _engine()
    engine.set_forced_lang(ae.LANG_JA_HAAS)
    engine.set_forced_lang(lang)
    assert engine.forced_lang == forced
    assert engine.selected_lang == forced
    assert engine._ja_model == "rz"


def test_unknown_language_is_rejected():
    with pytest.raises(ValueError):
        _engine().set_forced_lang("ja-unknown")


def test_mic_page_offers_both_japanese_models():
    html = pathlib.Path("scripts/mic_page.html").read_text(encoding="utf-8")
    assert '<option value="ja">日本語（ReazonSpeech）</option>' in html
    assert '<option value="ja-haas">日本語（Haas-Ja）</option>' in html


def test_mic_page_shows_haas_as_the_active_engine():
    html = pathlib.Path("scripts/mic_page.html").read_text(encoding="utf-8")
    assert html.count('"CPU (Haas-Ja)"') == 2


def test_haas_is_not_available_in_gpu_mode():
    engine = _engine("gpu")
    engine.set_forced_lang(ae.LANG_JA_HAAS)
    assert engine.selected_lang == "ja"
    assert engine._ja_model == "rz"
    engine._spawn_bg.assert_not_called()


def test_npu_mode_uses_its_own_haas_tier():
    engine = _engine("npu")
    engine.set_forced_lang(ae.LANG_JA_HAAS)
    assert engine.forced_lang == "ja"
    assert engine.selected_lang == ae.LANG_JA_HAAS
    assert engine._ja_model == "haas_npu"
    engine._spawn_bg.assert_called_once_with(ae.RoutedASR._get_with_fallback.__get__(engine), "haas_npu")
    engine.set_forced_lang("ja")
    assert engine._ja_model == "rz"
    assert engine.selected_lang == "ja"


def test_tokenizer_is_loaded_without_passing_its_path_to_sentencepiece(tmp_path):
    # SentencePiece fails on a non-ASCII path on Windows.
    (tmp_path / "tokenizer.model").write_bytes(b"model bytes")
    with patch.object(haas_ja.ort, "InferenceSession"), patch.object(haas_ja.spm, "SentencePieceProcessor") as processor:
        haas_ja.HaasJaRecognizer(str(tmp_path))
    processor.assert_called_once_with(model_proto=b"model bytes")


@pytest.mark.parametrize("saved, expected", [
    ({}, "ja-haas"),
    ({"punctuate": True}, "ja-haas"),
    ({"lang": "ja"}, "ja-haas"),
    ({"lang": None}, "ja-haas"),
    ({"lang": "en"}, "en"),
])
def test_first_cpu_start_moves_japanese_to_haas_once(saved, expected):
    result = subtitle_server._config_to_restore(saved, SimpleNamespace(haas_available=True))
    assert result["lang"] == expected
    assert result["ja_haas_switched"] is True


@pytest.mark.parametrize("lang", ["ja", None, "ja-haas"])
def test_choice_saved_after_the_switch_is_kept(lang):
    saved = {"lang": lang, "ja_haas_switched": True}
    assert subtitle_server._config_to_restore(saved, SimpleNamespace(haas_available=True)) == saved


def test_gpu_start_does_not_use_up_the_switch():
    saved = {"lang": "ja"}
    assert subtitle_server._config_to_restore(saved, SimpleNamespace(haas_available=False)) == saved


def test_whisper_engine_has_no_haas_default():
    assert subtitle_server._config_to_restore({}, SimpleNamespace()) == {}


@pytest.mark.parametrize("posted, saved, available, expected", [
    ("ja", "ja-haas", False, "ja-haas"),
    ("ja", "ja", False, "ja"),
    ("ja", None, False, "ja-haas"),
    ("en", "ja-haas", False, "en"),
    (None, "ja-haas", False, None),
    ("ja", "ja-haas", True, "ja"),
    ("ja-haas", "ja", True, "ja-haas"),
])
def test_saving_outside_cpu_mode_keeps_the_cpu_choice(posted, saved, available, expected):
    asr = SimpleNamespace(haas_available=available)
    with patch.object(subtitle_server, "_get_saved_asr_config", return_value={"lang": saved}):
        assert subtitle_server._config_to_save({"lang": posted}, asr)["lang"] == expected


@pytest.mark.parametrize("asr", [SimpleNamespace(haas_available=False, set_forced_lang=MagicMock()),
                                 SimpleNamespace(set_forced_lang=MagicMock())], ids=["gpu", "whisper"])
def test_restoring_haas_outside_cpu_mode_applies_japanese(asr):
    subtitle_server.RuntimeControls(asr=asr).apply_config({"lang": "ja-haas"})
    asr.set_forced_lang.assert_called_once_with("ja")


def test_get_config_reports_where_haas_is_offered():
    for asr, expected in ((SimpleNamespace(haas_available=True), True), (SimpleNamespace(), False)):
        asr.forced_lang, asr.dual_confirm, asr._punctuate = "ja", False, True
        asr.lid_switch_confirm, asr.min_switch_s = 2, 2.0
        assert subtitle_server.RuntimeControls(asr=asr).get_config()["haas_available"] is expected


def test_mic_page_hides_haas_unless_the_server_offers_it():
    html = pathlib.Path("scripts/mic_page.html").read_text(encoding="utf-8")
    assert "if (!cfg.haas_available)" in html
    assert 'isWhisper ? "日本語（Whisper）" : "日本語（ReazonSpeech）"' in html
