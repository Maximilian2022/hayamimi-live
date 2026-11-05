"""scripts/reazon_npu.py と asr_engine の NPU 切り替えの単体テスト"""
import os
import sys
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from scripts import reazon_npu as rn  # noqa: E402


def test_fbank_shape_and_silence_floor():
    features = rn.fbank(np.zeros(16000, dtype=np.float32))
    assert features.shape == (100, 80)
    assert features.max() < -10
    assert rn.fbank(np.zeros(0, dtype=np.float32)).shape == (0, 80)


def test_encoder_out_frames_matches_subsampling():
    assert rn.encoder_out_frames(1000) == 248
    assert rn.encoder_out_frames(3) == 0


def test_decode_tokens_rebuilds_bytes_and_spaces():
    letters = {v: k for k, v in rn._BYTE_OF_CHAR.items()}
    pieces = ["".join(letters[b] for b in "日本語".encode()), "▁hello", "▁world"]
    assert rn.decode_tokens(pieces) == "日本語hello world"


def test_split_long_audio_keeps_every_sample_and_cuts_at_silence():
    audio = np.ones(16000 * 30, dtype=np.float32)
    audio[16000 * 11 : 16000 * 11 + 1600] = 0.0
    parts = rn.split_long_audio(audio, 16000 * 12)
    assert sum(map(len, parts)) == len(audio)
    assert all(len(p) <= 16000 * 12 for p in parts)
    assert abs(len(parts[0]) - 16000 * 11) < 1600


def test_beam_search_starts_from_sherpa_context_and_picks_tokens():
    seen = []

    def decode(y):
        seen.append(y.tolist())
        return np.zeros((len(y), 2), dtype=np.float32)

    # 2 frames: the joiner prefers token 2, then blank
    frames = np.array([[1.0, 0.0], [0.0, 1.0]], dtype=np.float32)

    def join(encoder, decoder):
        logits = np.zeros((len(encoder), 3), dtype=np.float32)
        logits[:, 2] = encoder[:, 0] * 10
        logits[:, rn.BLANK_ID] = encoder[:, 1] * 10
        return logits

    assert rn.beam_search(frames, decode, join) == [2]
    assert seen[0] == [[-1, rn.BLANK_ID]]


def _engine(device):
    import asr_engine as ae

    engine = ae.RoutedASR.__new__(ae.RoutedASR)
    engine.device = device
    engine.device_name = "CPU"
    engine._threads = 1
    engine._hotwords_file = ""
    engine._closed = False
    engine._models = {}
    engine._on_event = None
    engine._model_locks = {"rz": MagicMock()}
    engine._spawn_bg = MagicMock()
    engine._preparing = set()
    return ae, engine


def test_cpu_mode_never_loads_the_npu_module():
    ae, engine = _engine("cpu")
    with patch.object(ae, "_reazon_npu_module") as module, patch.object(ae, "_build_reazon", return_value="cpu") as cpu:
        assert engine._build_ja() == "cpu"
    module.assert_not_called()
    cpu.assert_called_once()


def test_prepared_npu_is_used_and_reported():
    ae, engine = _engine("npu")
    npu = MagicMock()
    npu.has_npu.return_value = True
    npu.is_prepared.return_value = True
    with patch.object(ae, "_reazon_npu_module", return_value=npu), patch.object(ae, "_npu_model_dir", return_value="d"):
        assert engine._build_ja() is npu.ReazonNpuRecognizer.return_value
    assert engine.device_name == "NPU"


def test_unprepared_npu_runs_on_cpu_and_prepares_in_background():
    ae, engine = _engine("npu")
    npu = MagicMock()
    npu.has_npu.return_value = True
    npu.is_prepared.return_value = False
    with patch.object(ae, "_reazon_npu_module", return_value=npu), patch.object(ae, "_npu_model_dir", return_value="d"), \
         patch.object(ae, "_build_reazon", return_value="cpu"):
        assert engine._build_ja() == "cpu"
    assert engine.device_name == "CPU"
    engine._spawn_bg.assert_called_once()

    npu.prepare_in_subprocess.return_value = True
    engine._prepare_npu_and_switch(npu, "d")
    assert engine._models["rz"] is npu.ReazonNpuRecognizer.return_value
    assert engine.device_name == "NPU"


@pytest.mark.parametrize("has_npu, error", [(False, None), (True, RuntimeError("driver"))])
def test_npu_problems_fall_back_to_cpu(has_npu, error):
    ae, engine = _engine("npu")
    npu = MagicMock()
    npu.has_npu.return_value = has_npu
    npu.is_prepared.return_value = True
    npu.ReazonNpuRecognizer.side_effect = error
    with patch.object(ae, "_reazon_npu_module", return_value=npu), patch.object(ae, "_npu_model_dir", return_value="d"), \
         patch.object(ae, "_build_reazon", return_value="cpu"):
        assert engine._build_ja() == "cpu"
    assert engine.device_name == "CPU"


def test_failed_preparation_keeps_cpu():
    ae, engine = _engine("npu")
    engine._models["rz"] = "cpu"
    npu = MagicMock()
    npu.prepare_in_subprocess.return_value = False
    engine._prepare_npu_and_switch(npu, "d")
    assert engine._models["rz"] == "cpu"
    assert engine.device_name == "CPU"
