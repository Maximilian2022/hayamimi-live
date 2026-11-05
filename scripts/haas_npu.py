"""
scripts/haas_npu.py
Haas-ja-v1.0 (Japanese ASR) with the encoder on the Intel NPU through OpenVINO.

The small decoder stays on the CPU. The NPU returns wrong results for the dynamically
quantized export that the CPU mode uses, so this uses the weight-only export (int8wo).

The NPU needs fixed input sizes. One 13.3 s encoder covers the live VAD's 12 s maximum plus
its 1 s preroll. Every extra size costs about 0.6 GB of memory.

A failed NPU compile can abort the whole process, so preparation runs in a child process:

    python scripts/haas_npu.py --prepare --model-dir <model> --out <dir>
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import threading

import numpy as np

try:
    from haas_ja import _HOP, MIN_SAMPLES, HaasJaRecognizer, log_mel, split_at_quiet_points
except ImportError:
    from scripts.haas_ja import _HOP, MIN_SAMPLES, HaasJaRecognizer, log_mel, split_at_quiet_points

logger = logging.getLogger("haas_npu")

FRAMES = 1328
FORMAT_VERSION = 1
BLOB_FILENAME = f"encoder_{FRAMES}.blob"
MARKER_FILENAME = "ready.json"
# On _check_input(), a working encoder scored 0.94 against the CPU and a broken one 0.32.
MIN_AGREEMENT = 0.8


def _fingerprint(core) -> dict:
    import openvino as ov

    return {"format": FORMAT_VERSION, "openvino": ov.__version__, "driver": str(core.get_property("NPU", "NPU_DRIVER_VERSION")), "frames": FRAMES}


def is_prepared(out_dir: str) -> bool:
    import openvino as ov

    try:
        with open(os.path.join(out_dir, MARKER_FILENAME), encoding="utf-8") as f:
            marker = json.load(f)
        return marker == _fingerprint(ov.Core()) and os.path.isfile(os.path.join(out_dir, BLOB_FILENAME))
    except (OSError, ValueError, RuntimeError):
        return False


def has_npu() -> bool:
    try:
        import openvino as ov

        return "NPU" in ov.Core().available_devices
    except Exception as e:
        logger.warning("OpenVINO cannot list devices: %s", e)
        return False


def _check_input() -> dict:
    # Speech-like input: band-limited noise with a syllable rhythm, through the real features.
    rng = np.random.default_rng(0)
    seconds = 8
    t = np.arange(seconds * 16000) / 16000
    audio = np.cumsum(rng.normal(0, 0.02, len(t))).astype(np.float32)
    audio = (audio - audio.mean()) * (0.5 + 0.5 * np.sin(2 * np.pi * 3 * t)).astype(np.float32)
    mel, valid = log_mel(audio / (np.abs(audio).max() + 1e-6) * 0.3)
    mel = np.pad(mel, ((0, 0), (0, 0), (0, FRAMES - mel.shape[-1])))
    return {"audio_signal": mel, "length": np.array([valid], np.int64)}


def agreement(a: np.ndarray, b: np.ndarray) -> float:
    a, b = a.ravel(), b.ravel()
    return float(a @ b / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-9))


def _encoded(compiled, inputs: dict) -> np.ndarray:
    request = compiled.create_infer_request()
    request.infer(inputs)
    return request.get_output_tensor(0).data.copy()


def prepare(model_dir: str, out_dir: str) -> None:
    """Compile, check against the CPU, and save. The marker is written last, so a crash leaves it absent."""
    import openvino as ov

    os.makedirs(out_dir, exist_ok=True)
    marker = os.path.join(out_dir, MARKER_FILENAME)
    if os.path.exists(marker):
        os.remove(marker)
    core = ov.Core()
    model = core.read_model(os.path.join(model_dir, "encoder.onnx"))
    model.reshape({"audio_signal": [1, 128, FRAMES], "length": [1]})
    inputs = _check_input()
    expected = _encoded(core.compile_model(model, "CPU"), inputs)
    compiled = core.compile_model(model, "NPU")
    score = agreement(_encoded(compiled, inputs), expected)
    if score < MIN_AGREEMENT:
        raise RuntimeError(f"the NPU returned wrong results (agreement {score:.3f})")
    blob = os.path.join(out_dir, BLOB_FILENAME)
    with open(blob + ".tmp", "wb") as f:
        f.write(compiled.export_model().getvalue())
    os.replace(blob + ".tmp", blob)
    with open(marker, "w", encoding="utf-8") as f:
        json.dump(_fingerprint(core), f)


def prepare_in_subprocess(model_dir: str, out_dir: str, timeout_sec: float = 900.0) -> bool:
    command = [sys.executable, "-B", os.path.abspath(__file__), "--prepare", "--model-dir", model_dir, "--out", out_dir]
    flags = 0x08000000 if sys.platform == "win32" else 0  # CREATE_NO_WINDOW
    try:
        done = subprocess.run(command, capture_output=True, text=True, errors="replace", timeout=timeout_sec, creationflags=flags)
    except (OSError, subprocess.TimeoutExpired) as e:
        logger.warning("Haas-ja NPU preparation failed: %s", e)
        return False
    if done.returncode != 0:
        logger.warning("Haas-ja NPU preparation failed (%s): %s", done.returncode, done.stderr[-400:])
        return False
    return True


class HaasNpuRecognizer(HaasJaRecognizer):
    """Stands in for sherpa_onnx.OfflineRecognizer in asr_engine's ja tier."""

    def __init__(self, model_dir: str, out_dir: str):
        import onnxruntime as ort
        import openvino as ov
        import sentencepiece as spm

        with open(os.path.join(out_dir, BLOB_FILENAME), "rb") as f:
            self._request = ov.Core().import_model(f.read(), "NPU").create_infer_request()
        options = ort.SessionOptions()
        options.intra_op_num_threads = 1
        self._decoder = ort.InferenceSession(os.path.join(model_dir, "decoder_joint.onnx"), options, providers=["CPUExecutionProvider"])
        # model_proto: SentencePiece cannot open a non-ASCII path on Windows.
        with open(os.path.join(model_dir, "tokenizer.model"), "rb") as f:
            self._tokenizer = spm.SentencePieceProcessor(model_proto=f.read())
        self._lock = threading.Lock()
        # The first request is slow, so it should not land on real speech.
        self._transcribe(np.zeros(16000, dtype=np.float32))

    def _encode(self, mel: np.ndarray, valid: int) -> tuple[np.ndarray, int]:
        mel = np.pad(mel, ((0, 0), (0, 0), (0, FRAMES - mel.shape[-1])))
        self._request.infer({"audio_signal": mel, "length": np.array([valid], np.int64)})
        return self._request.get_output_tensor(0).data.copy(), int(self._request.get_output_tensor(1).data[0])

    def decode_stream(self, stream) -> None:
        parts = split_at_quiet_points(stream.waveform(), (FRAMES - 16) * _HOP)
        stream.result.text = "".join(self._transcribe(p) for p in parts if len(p) >= MIN_SAMPLES)


def main() -> None:
    parser = argparse.ArgumentParser(description="Prepare Haas-ja for the Intel NPU")
    parser.add_argument("--prepare", action="store_true")
    parser.add_argument("--model-dir", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    if args.prepare:
        prepare(args.model_dir, args.out)


if __name__ == "__main__":
    main()
