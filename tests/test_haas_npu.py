"""scripts/haas_npu.py と asr_engine の NPU 切り替えの単体テスト"""
import json
import os
import sys
import threading
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

pytest.importorskip("onnxruntime")
pytest.importorskip("sentencepiece")
pytest.importorskip("openvino")

import asr_engine as ae  # noqa: E402
import haas_npu as hn  # noqa: E402


def test_window_holds_the_longest_live_segment():
    longest_live_samples = int((12.0 + 1.0) * 16000)
    assert (hn.FRAMES - 16) * hn._HOP >= longest_live_samples
    assert hn.FRAMES % 16 == 0


def test_check_input_fits_the_fixed_encoder():
    inputs = hn._check_input()
    assert inputs["audio_signal"].shape == (1, 128, hn.FRAMES)
    assert 0 < inputs["length"][0] <= hn.FRAMES
    assert np.isfinite(inputs["audio_signal"]).all()


def test_agreement_separates_matching_and_unrelated_output():
    rng = np.random.default_rng(0)
    a = rng.normal(size=(1, 1024, 100))
    assert hn.agreement(a, a + rng.normal(0, 0.1, a.shape)) > hn.MIN_AGREEMENT
    assert hn.agreement(a, rng.normal(size=a.shape)) < hn.MIN_AGREEMENT


def test_prepared_marker_must_match_this_environment(tmp_path):
    (tmp_path / hn.BLOB_FILENAME).write_bytes(b"x")
    assert hn.is_prepared(str(tmp_path)) is False
    with patch.object(hn, "_fingerprint", return_value={"format": hn.FORMAT_VERSION, "driver": "new"}):
        (tmp_path / hn.MARKER_FILENAME).write_text(json.dumps({"format": hn.FORMAT_VERSION, "driver": "old"}), encoding="utf-8")
        assert hn.is_prepared(str(tmp_path)) is False
        (tmp_path / hn.MARKER_FILENAME).write_text(json.dumps({"format": hn.FORMAT_VERSION, "driver": "new"}), encoding="utf-8")
        assert hn.is_prepared(str(tmp_path)) is True


def test_failed_child_reports_false():
    with patch.object(hn.subprocess, "run", return_value=MagicMock(returncode=3, stderr="boom")):
        assert hn.prepare_in_subprocess("m", "o") is False


def _engine():
    engine = ae.RoutedASR.__new__(ae.RoutedASR)
    engine._closed = False
    engine.device = "npu"
    engine.device_name = "CPU"
    engine._models = {}
    engine._unavailable = {"haas_npu"}
    engine._preparing = set()
    engine._fallback_warned = {"haas_npu"}
    engine._model_locks = {"haas_npu": threading.Lock()}
    engine._spawn_bg = MagicMock()
    engine._emit = MagicMock()
    return engine


def _npu(ready=True, prepared=True):
    return MagicMock(has_npu=MagicMock(return_value=True), is_prepared=MagicMock(return_value=ready),
                     prepare_in_subprocess=MagicMock(return_value=prepared))


def test_prepared_model_is_used_directly():
    engine, npu = _engine(), _npu()
    with patch.object(ae, "_haas_npu_module", return_value=npu):
        assert engine._build_haas_npu() is npu.HaasNpuRecognizer.return_value
    assert engine.device_name == "NPU"


def test_first_start_prepares_in_background_and_falls_back_meanwhile():
    engine, npu = _engine(), _npu(ready=False)
    with patch.object(ae, "_haas_npu_module", return_value=npu), pytest.raises(RuntimeError):
        engine._build_haas_npu()
    engine._spawn_bg.assert_called_once()


def test_finished_preparation_switches_to_haas():
    engine, npu = _engine(), _npu(ready=False)
    engine._prepare_haas_npu_and_switch(npu, "out")
    assert engine._models["haas_npu"] is npu.HaasNpuRecognizer.return_value
    assert "haas_npu" not in engine._unavailable
    assert "haas_npu" not in engine._fallback_warned
    assert engine.device_name == "NPU"


@pytest.mark.parametrize("break_it", ["prepare", "load"])
def test_failed_preparation_stays_on_reazonspeech_and_warns(break_it):
    engine, npu = _engine(), _npu(ready=False, prepared=break_it != "prepare")
    if break_it == "load":
        npu.HaasNpuRecognizer.side_effect = RuntimeError("bad blob")
    engine._prepare_haas_npu_and_switch(npu, "out")
    assert "haas_npu" not in engine._models
    assert "haas_npu" in engine._unavailable
    assert engine._emit.call_args.args[0]["code"] == "npu_unavailable"


def test_npu_haas_falls_back_to_reazonspeech():
    assert ae._TIER_FALLBACK["haas_npu"] == ("rz",)


def _ready_engine(tier):
    engine = _engine()
    engine._ja_model = tier
    engine._unavailable = set()
    engine._preparing = set()
    engine._loading = set()
    return engine


def test_selected_model_is_not_ready_while_it_loads():
    assert _ready_engine("haas").selected_model_ready is False


def test_selected_model_is_ready_once_loaded():
    engine = _ready_engine("haas")
    engine._models["haas"] = object()
    assert engine.selected_model_ready is True


def test_selected_model_is_ready_when_it_cannot_be_used():
    # ReazonSpeech is running, so the Mic page must not wait forever.
    engine = _ready_engine("haas")
    engine._unavailable.add("haas")
    assert engine.selected_model_ready is True


@pytest.mark.parametrize("tier", ["haas_npu", "rz"])
def test_selected_model_is_not_ready_while_preparing(tier):
    engine = _ready_engine(tier)
    engine._models["rz"] = object()
    engine._unavailable.add("haas_npu")
    engine._preparing.add(tier)
    assert engine.selected_model_ready is False


def test_haas_prepared_while_reazonspeech_still_prepares_is_ready():
    engine = _ready_engine("haas_npu")
    engine._models["haas_npu"] = object()
    engine._preparing.add("rz")
    assert engine.selected_model_ready is True


@pytest.mark.parametrize("fail", [False, True])
def test_preparing_ends_even_when_preparation_raises(fail):
    engine = _ready_engine("haas_npu")
    engine._preparing.add("haas_npu")
    prepare = MagicMock(side_effect=RuntimeError("boom") if fail else None)
    if fail:
        with pytest.raises(RuntimeError):
            engine._finish_preparing("haas_npu", prepare, "npu", "out")
    else:
        engine._finish_preparing("haas_npu", prepare, "npu", "out")
    assert engine._preparing == set()


def test_launcher_waits_for_the_selected_model():
    text = open("scripts/launcher_gui.ps1", encoding="utf-8-sig").read()
    assert "$cfgJson.asr_ready -eq $false" in text
    assert "$isHttpReady -and $isWsReady -and $isAsrReady" in text


def test_selected_model_is_not_ready_while_it_loads_in_the_background():
    engine = _ready_engine("haas")
    engine._models["haas"] = object()
    engine._loading.add("haas")
    assert engine.selected_model_ready is False


@pytest.mark.parametrize("fail", [False, True])
def test_loading_ends_even_when_the_load_fails(fail):
    engine = _ready_engine("haas")
    engine._loading.add("haas")
    effect = ae.ModelUnavailable("haas") if fail else None
    with patch.object(ae.RoutedASR, "_get_with_fallback", side_effect=effect):
        if fail:
            with pytest.raises(ae.ModelUnavailable):
                engine._load_ja_model("haas")
        else:
            engine._load_ja_model("haas")
    assert engine._loading == set()


@pytest.mark.parametrize("loaded, active", [(True, "haas_npu"), (False, "rz")])
def test_active_tier_reports_what_really_runs(loaded, active):
    engine = _ready_engine("haas_npu")
    if loaded:
        engine._models["haas_npu"] = object()
    assert engine.active_ja_tier == active
