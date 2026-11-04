"""
scripts/reazon_npu.py
ReazonSpeech (Japanese) on the Intel NPU through OpenVINO.

The zipformer encoder runs on the NPU. The small decoder and joiner run on the CPU.
The object mirrors the part of sherpa_onnx.OfflineRecognizer that asr_engine uses, and
its modified beam search follows sherpa-onnx's, so only the device changes.

The NPU needs fixed input sizes, and its time grows with that size, so one encoder is
compiled per window length and the shortest one that fits each utterance is used.

Everything is built from the model already on the PC with OpenVINO and NumPy alone, so
this adds no native dependency for Smart App Control to block. A failed NPU compile can
abort the whole process, so preparation always runs in a child process:

    python scripts/reazon_npu.py --prepare --model-dir <model> --out <dir>
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import re
import subprocess
import sys
import threading
import unicodedata
from types import SimpleNamespace
from typing import Sequence

import numpy as np

logger = logging.getLogger("reazon_npu")

SAMPLE_RATE = 16000
# 10 ms per frame. 1250 covers the 12 s maximum speech length of the live VAD.
WINDOW_FRAMES = (300, 500, 800, 1250)
FORMAT_VERSION = 1
MARKER_FILENAME = "ready.json"
BLANK_ID = 0
CONTEXT_SIZE = 2
MAX_ACTIVE_PATHS = 4
ENCODER_DIM = 512
DECODER_DIM = 512
SPLIT_SEARCH_SEC = 1.5
# sherpa-onnx pads batches with log(1e-10). Silence after the speech keeps the fixed-size
# encoder from cutting off the last sounds, which it did in evaluation.
PADDING_VALUE = -23.025850929940457
TAIL_SILENCE_FRAMES = 30
_EPSILON = float(np.finfo(np.float32).eps)
_NON_ASCII_SPACE = re.compile(r"(?<![!-~]) +| +(?![!-~])")


def _mel_banks(num_bins: int, fft_size: int, low_freq: float, high_freq: float) -> np.ndarray:
    def mel(freq):
        return 1127.0 * np.log(1.0 + freq / 700.0)

    nyquist = 0.5 * SAMPLE_RATE
    if high_freq <= 0.0:
        high_freq += nyquist
    half = fft_size // 2
    freqs = (SAMPLE_RATE / fft_size) * np.arange(half)
    delta = (mel(high_freq) - mel(low_freq)) / (num_bins + 1)
    bins = np.arange(num_bins)[:, None]
    left = mel(low_freq) + bins * delta
    center = left + delta
    right = center + delta
    points = mel(freqs)[None, :]
    rising = (points - left) / (center - left)
    falling = (right - points) / (right - center)
    banks = np.zeros((num_bins, half + 1), dtype=np.float64)
    banks[:, :half] = np.maximum(0.0, np.minimum(rising, falling))
    return banks


_FRAME_SAMPLES = 400
_HOP_SAMPLES = 160
_FFT_SIZE = 512
_WINDOW = (0.5 - 0.5 * np.cos(2 * np.pi * np.arange(_FRAME_SAMPLES) / (_FRAME_SAMPLES - 1))) ** 0.85
_BANKS = _mel_banks(80, _FFT_SIZE, 20.0, -400.0)


def fbank(samples: np.ndarray) -> np.ndarray:
    """80-bin Kaldi fbank with the settings sherpa-onnx uses (snip_edges=False, no dither)."""
    x = np.asarray(samples, dtype=np.float64)
    n = len(x)
    num_frames = (n + _HOP_SAMPLES // 2) // _HOP_SAMPLES
    if num_frames <= 0:
        return np.zeros((0, 80), dtype=np.float32)
    starts = np.arange(num_frames) * _HOP_SAMPLES + _HOP_SAMPLES // 2 - _FRAME_SAMPLES // 2
    index = starts[:, None] + np.arange(_FRAME_SAMPLES)[None, :]
    index = np.where(index < 0, -index - 1, index)
    index = np.where(index >= n, 2 * n - 1 - index, index)
    frames = x[np.clip(index, 0, n - 1)]
    frames = frames - frames.mean(axis=1, keepdims=True)
    previous = np.concatenate([frames[:, :1], frames[:, :-1]], axis=1)
    frames = (frames - 0.97 * previous) * _WINDOW[None, :]
    spectrum = np.fft.rfft(frames, n=_FFT_SIZE, axis=1)
    power = spectrum.real**2 + spectrum.imag**2
    return np.log(np.maximum(power @ _BANKS.T, _EPSILON)).astype(np.float32)


def encoder_out_frames(num_frames: int) -> int:
    return max(((num_frames - 7) // 2 + 1) // 2, 0)


def _byte_table() -> dict[str, int]:
    # The training recipe maps bytes 128..255 to the first NFKC-stable letters from U+0121.
    letters = [
        c for c in range(289, 900)
        if unicodedata.normalize("NFKC", chr(c)) == chr(c) and unicodedata.category(chr(c)).startswith("L")
    ]
    return {chr(c): 128 + i for i, c in enumerate(letters)}


_BYTE_OF_CHAR = _byte_table()


def decode_tokens(pieces: Sequence[str]) -> str:
    raw = bytearray()
    for ch in "".join(pieces).replace("▁", " "):
        if ch in _BYTE_OF_CHAR:
            raw.append(_BYTE_OF_CHAR[ch])
        elif ord(ch) < 128:
            raw.append(ord(ch))
        else:
            raw += ch.encode("utf-8")
    text = re.sub(r" +", " ", raw.decode("utf-8", errors="replace").strip())
    return _NON_ASCII_SPACE.sub("", text)


def split_long_audio(samples: np.ndarray, max_samples: int) -> list[np.ndarray]:
    """Split audio longer than the largest window at the quietest point near each limit."""
    parts, start = [], 0
    search = min(int(SPLIT_SEARCH_SEC * SAMPLE_RATE), max_samples // 3)
    while len(samples) - start > max_samples:
        low = start + max_samples - search
        count = search // _HOP_SAMPLES
        energy = np.square(samples[low : low + count * _HOP_SAMPLES]).reshape(count, _HOP_SAMPLES).mean(axis=1)
        cut = low + int(np.argmin(energy)) * _HOP_SAMPLES + _HOP_SAMPLES // 2
        parts.append(samples[start:cut])
        start = cut
    parts.append(samples[start:])
    return parts


def beam_search(frames: np.ndarray, decode_fn, join_fn, max_paths: int = MAX_ACTIVE_PATHS) -> list[int]:
    """sherpa-onnx's modified beam search for a transducer.

    decode_fn: int64 array (batch, CONTEXT_SIZE) -> decoder output (batch, dim)
    join_fn: encoder (batch, dim), decoder (batch, dim) -> logits (batch, vocab)
    """
    # sherpa-onnx starts from [-1, blank]. The decoder zeroes the embedding of -1.
    start = (-1,) * (CONTEXT_SIZE - 1) + (BLANK_ID,)
    hyps: dict[tuple[int, ...], float] = {start: 0.0}
    decoder_cache: dict[tuple[int, ...], np.ndarray] = {}

    for frame in frames:
        tokens = list(hyps)
        contexts = [t[-CONTEXT_SIZE:] for t in tokens]
        missing = list(dict.fromkeys(c for c in contexts if c not in decoder_cache))
        if missing:
            outputs = decode_fn(np.array(missing, dtype=np.int64))
            decoder_cache.update(zip(missing, outputs))
        decoder_out = np.stack([decoder_cache[c] for c in contexts])
        encoder_out = np.repeat(frame[None, :], len(tokens), axis=0)
        logits = join_fn(encoder_out, decoder_out).astype(np.float64)
        logits -= logits.max(axis=1, keepdims=True)
        log_probs = logits - np.log(np.exp(logits).sum(axis=1, keepdims=True))
        log_probs += np.array([hyps[t] for t in tokens])[:, None]

        flat = log_probs.reshape(-1)
        count = min(max_paths, flat.size)
        best = np.argpartition(flat, -count)[-count:]
        next_hyps: dict[tuple[int, ...], float] = {}
        vocab = log_probs.shape[1]
        for index in best:
            hyp_index, token = divmod(int(index), vocab)
            ys = tokens[hyp_index]
            if token != BLANK_ID:
                ys = ys + (token,)
            score = float(flat[index])
            if ys in next_hyps:
                high, low = max(next_hyps[ys], score), min(next_hyps[ys], score)
                next_hyps[ys] = high + math.log1p(math.exp(low - high))
            else:
                next_hyps[ys] = score
        hyps = next_hyps

    best_ys = max(hyps, key=lambda ys: hyps[ys] / len(ys))
    return list(best_ys[CONTEXT_SIZE:])


def _marker_path(out_dir: str) -> str:
    return os.path.join(out_dir, MARKER_FILENAME)


def is_prepared(out_dir: str) -> bool:
    import openvino as ov

    try:
        with open(_marker_path(out_dir), encoding="utf-8") as f:
            marker = json.load(f)
    except (OSError, ValueError):
        return False
    return marker == {"format": FORMAT_VERSION, "openvino": ov.__version__, "windows": list(WINDOW_FRAMES)}


def has_npu() -> bool:
    try:
        import openvino as ov

        return "NPU" in ov.Core().available_devices
    except Exception as e:
        logger.warning("OpenVINO cannot list devices: %s", e)
        return False


def _fixed_encoder(core_module, source: str, frames: int):
    """Encoder with static shapes. The NPU compiler cannot take x_lens as an input."""
    from openvino import opset13 as ops

    model = core_module.convert_model(source)
    lengths = next(p for p in model.get_parameters() if "x_lens" in p.output(0).get_names())
    constant = ops.constant(np.array([frames], dtype=np.int64))
    for target in list(lengths.output(0).get_target_inputs()):
        target.replace_source_output(constant.output(0))
    model.remove_parameter(lengths)
    model.reshape({"x": [1, frames, 80]})
    return model


def prepare(model_dir: str, out_dir: str) -> None:
    """Build and compile everything. The marker is written last, so a crash leaves it absent."""
    import openvino as ov

    os.makedirs(out_dir, exist_ok=True)
    marker = _marker_path(out_dir)
    if os.path.exists(marker):
        os.remove(marker)
    core = ov.Core()
    config = {"PERFORMANCE_HINT": "LATENCY", "CACHE_DIR": os.path.join(out_dir, "cache")}
    encoder_source = os.path.join(model_dir, "encoder-epoch-35-avg-1.onnx")
    for frames in WINDOW_FRAMES:
        xml = os.path.join(out_dir, f"encoder_{frames}.xml")
        ov.save_model(_fixed_encoder(ov, encoder_source, frames), xml, compress_to_fp16=True)
        core.compile_model(core.read_model(xml), "NPU", config)
    for name in ("decoder", "joiner"):
        model = ov.convert_model(os.path.join(model_dir, f"{name}-epoch-35-avg-1.onnx"))
        ov.save_model(model, os.path.join(out_dir, f"{name}.xml"), compress_to_fp16=False)
    with open(marker, "w", encoding="utf-8") as f:
        json.dump({"format": FORMAT_VERSION, "openvino": ov.__version__, "windows": list(WINDOW_FRAMES)}, f)


def prepare_in_subprocess(model_dir: str, out_dir: str, timeout_sec: float = 900.0) -> bool:
    command = [sys.executable, "-B", os.path.abspath(__file__), "--prepare", "--model-dir", model_dir, "--out", out_dir]
    flags = 0x08000000 if sys.platform == "win32" else 0  # CREATE_NO_WINDOW
    try:
        done = subprocess.run(command, capture_output=True, text=True, errors="replace",
                              timeout=timeout_sec, creationflags=flags)
    except (OSError, subprocess.TimeoutExpired) as e:
        logger.warning("NPU model preparation failed: %s", e)
        return False
    if done.returncode != 0:
        logger.warning("NPU model preparation failed (%s): %s", done.returncode, done.stderr[-400:])
        return False
    return True


class ReazonNpuRecognizer:
    """Stands in for sherpa_onnx.OfflineRecognizer in asr_engine's ja tier."""

    def __init__(self, model_dir: str, out_dir: str):
        import openvino as ov

        self._lock = threading.Lock()
        core = ov.Core()
        config = {"PERFORMANCE_HINT": "LATENCY", "CACHE_DIR": os.path.join(out_dir, "cache")}
        self._encoders = {
            frames: core.compile_model(core.read_model(os.path.join(out_dir, f"encoder_{frames}.xml")), "NPU", config)
            for frames in WINDOW_FRAMES
        }
        cpu = {"PERFORMANCE_HINT": "LATENCY", "INFERENCE_NUM_THREADS": 2}
        self._decoder = core.compile_model(core.read_model(os.path.join(out_dir, "decoder.xml")), "CPU", cpu)
        self._joiner = core.compile_model(core.read_model(os.path.join(out_dir, "joiner.xml")), "CPU", cpu)
        with open(os.path.join(model_dir, "tokens.txt"), encoding="utf-8") as f:
            self._tokens = [line.rsplit(" ", 1)[0] for line in f.read().splitlines()]
        self._decode_silence()

    def _decode_silence(self) -> None:
        # The first request on each encoder is slow, so it should not land on real speech.
        for frames in WINDOW_FRAMES:
            count = frames - TAIL_SILENCE_FRAMES - 1
            self._transcribe_window(np.zeros(count * _HOP_SAMPLES, dtype=np.float32))

    def create_stream(self):
        chunks: list[np.ndarray] = []

        def accept_waveform(sample_rate: int, samples) -> None:
            data = np.asarray(samples, dtype=np.float32).reshape(-1)
            if sample_rate != SAMPLE_RATE:
                count = int(len(data) * SAMPLE_RATE / sample_rate)
                data = np.interp(np.linspace(0, len(data) - 1, count), np.arange(len(data)), data).astype(np.float32)
            chunks.append(data)

        stream = SimpleNamespace(result=SimpleNamespace(text="", tokens=[], lang=""))
        stream.accept_waveform = accept_waveform
        stream.waveform = lambda: np.concatenate(chunks) if chunks else np.zeros(0, dtype=np.float32)
        return stream

    def decode_stream(self, stream) -> None:
        samples = stream.waveform()
        max_samples = (WINDOW_FRAMES[-1] - TAIL_SILENCE_FRAMES - 1) * _HOP_SAMPLES
        texts = []
        for part in split_long_audio(samples, max_samples):
            if len(part) >= _HOP_SAMPLES * 10:
                with self._lock:
                    texts.append(self._transcribe_window(part))
        stream.result.text = "".join(texts)

    def _transcribe_window(self, samples: np.ndarray) -> str:
        features = fbank(np.concatenate([samples, np.zeros(TAIL_SILENCE_FRAMES * _HOP_SAMPLES, dtype=np.float32)]))
        count = len(features)
        frames = next(f for f in WINDOW_FRAMES if count <= f)
        batch = np.full((1, frames, 80), PADDING_VALUE, dtype=np.float32)
        batch[0, :count] = features
        encoded = self._encoders[frames]({"x": batch})[0][0][: encoder_out_frames(count)]
        ids = beam_search(
            encoded,
            lambda y: self._decoder({"y": y})[0],
            lambda e, d: self._joiner({"encoder_out": e, "decoder_out": d})[0],
        )
        return decode_tokens([self._tokens[i] for i in ids])


def main() -> None:
    parser = argparse.ArgumentParser(description="Prepare ReazonSpeech for the Intel NPU")
    parser.add_argument("--prepare", action="store_true")
    parser.add_argument("--model-dir", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    if args.prepare:
        prepare(args.model_dir, args.out)


if __name__ == "__main__":
    main()
