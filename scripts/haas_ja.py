"""
scripts/haas_ja.py
Haas-ja-v1.0 (Japanese ASR) on the CPU through ONNX Runtime.

The official release needs Linux, NeMo, and CUDA, so this uses the unofficial quantized
ONNX export (Sanami/haas-ja-onnx, Apache-2.0). Pre- and post-processing follow its
published specification. The object mirrors the part of sherpa_onnx.OfflineRecognizer
that asr_engine uses.
"""

from __future__ import annotations

import os
import threading
from types import SimpleNamespace

import numpy as np
import onnxruntime as ort
import sentencepiece as spm

SAMPLE_RATE = 16000
_N_FFT, _WIN, _HOP, _N_MELS, _PAD_TO = 512, 400, 160, 128, 16
_DURATIONS = 5
_MAX_SYMBOLS = 10
_BLANK = 3072
_STATE_SHAPE = (2, 1, 20480)
# The model was trained on utterances up to 30 s.
MAX_SECONDS = 29.0
MIN_SAMPLES = _HOP * 10
_SPLIT_SEARCH_SAMPLES = int(1.5 * SAMPLE_RATE)


def _round_bfloat16(values: np.ndarray) -> np.ndarray:
    # The checkpoint stores its window and filterbank in bfloat16 and computes with those.
    bits = np.asarray(values, np.float32).view(np.uint32)
    rounded = (bits + 0x7FFF + ((bits >> 16) & 1)) & 0xFFFF0000
    return rounded.astype(np.uint32).view(np.float32)


def _hz_to_mel(hz):
    hz = np.asarray(hz, dtype=np.float64)
    high = 15.0 + 27.0 * np.log(np.maximum(hz, 1e-10) / 1000.0) / np.log(6.4)
    return np.where(hz < 1000.0, 3.0 * hz / 200.0, high)


def _mel_to_hz(mel):
    mel = np.asarray(mel, dtype=np.float64)
    return np.where(mel < 15.0, 200.0 * mel / 3.0, 1000.0 * np.exp(np.log(6.4) * (mel - 15.0) / 27.0))


def _slaney_filterbank() -> np.ndarray:
    bins = np.linspace(0, SAMPLE_RATE / 2, _N_FFT // 2 + 1)
    edges = _mel_to_hz(np.linspace(_hz_to_mel(0.0), _hz_to_mel(SAMPLE_RATE / 2), _N_MELS + 2))
    widths = np.diff(edges)
    ramps = edges[:, None] - bins[None, :]
    rising = -ramps[:-2] / widths[:-1, None]
    falling = ramps[2:] / widths[1:, None]
    weights = np.maximum(0.0, np.minimum(rising, falling))
    return (weights * (2.0 / (edges[2:] - edges[:-2]))[:, None]).astype(np.float32)


_FILTERBANK = _round_bfloat16(_slaney_filterbank())
_WINDOW = np.pad(
    _round_bfloat16(np.hanning(_WIN).astype(np.float32)),
    ((_N_FFT - _WIN) // 2, (_N_FFT - _WIN + 1) // 2),
)


def log_mel(samples: np.ndarray) -> tuple[np.ndarray, int]:
    """Return ([1, 128, T], valid frames), normalized per feature as the model expects."""
    audio = np.asarray(samples, np.float32)
    valid = len(audio) // _HOP
    audio = np.concatenate([audio[:1], audio[1:] - np.float32(0.97) * audio[:-1]])
    audio = np.pad(audio, _N_FFT // 2)
    count = 1 + (len(audio) - _N_FFT) // _HOP
    frames = audio[(np.arange(count) * _HOP)[:, None] + np.arange(_N_FFT)[None, :]]
    spectrum = np.fft.rfft(frames * _WINDOW, axis=1)
    power = (spectrum.real**2 + spectrum.imag**2).astype(np.float32).T
    mel = np.log(_FILTERBANK @ power + np.float32(2**-24))
    mean = mel[:, :valid].mean(-1, keepdims=True)
    deviation = ((mel[:, :valid] - mean) ** 2).sum(-1, keepdims=True) / (valid - 1)
    mel = (mel - mean) / (np.sqrt(deviation) + np.float32(1e-5))
    mel[:, valid:] = 0
    if mel.shape[-1] % _PAD_TO:
        mel = np.pad(mel, ((0, 0), (0, _PAD_TO - mel.shape[-1] % _PAD_TO)))
    return mel[None].astype(np.float32), valid


def greedy_tdt(step, encoded: np.ndarray, frames: int) -> list[int]:
    """Greedy TDT decoding. step(frame, last_token, s1, s2) -> (logits, new_s1, new_s2)."""
    state1 = np.zeros(_STATE_SHAPE, np.float32)
    state2 = np.zeros_like(state1)
    last, tokens, t, last_frame, repeats = _BLANK, [], 0, -1, 0
    while t < frames:
        while True:
            logits, next1, next2 = step(encoded[:, :, t : t + 1], last, state1, state2)
            token = int(logits[:-_DURATIONS].argmax())
            advance = int(logits[-_DURATIONS:].argmax())
            if token == _BLANK and advance == 0:
                advance = 1
            frame, t = t, t + advance
            if token != _BLANK or t >= frames:
                break
        if token != _BLANK:
            tokens.append(token)
            repeats = repeats + 1 if frame == last_frame else 1
            last_frame = frame
            state1, state2, last = next1, next2, token
            if repeats >= _MAX_SYMBOLS and last_frame == t:
                t += 1
    return tokens


def split_at_quiet_points(samples: np.ndarray, max_samples: int) -> list[np.ndarray]:
    parts, start = [], 0
    while len(samples) - start > max_samples:
        low = start + max_samples - _SPLIT_SEARCH_SAMPLES
        count = _SPLIT_SEARCH_SAMPLES // _HOP
        energy = np.square(samples[low : low + count * _HOP]).reshape(count, _HOP).mean(axis=1)
        cut = low + int(np.argmin(energy)) * _HOP + _HOP // 2
        parts.append(samples[start:cut])
        start = cut
    parts.append(samples[start:])
    return parts


class HaasJaRecognizer:
    def __init__(self, model_dir: str, threads: int = 4):
        options = ort.SessionOptions()
        options.intra_op_num_threads = threads
        providers = ["CPUExecutionProvider"]
        self._encoder = ort.InferenceSession(os.path.join(model_dir, "encoder.onnx"), options, providers=providers)
        self._decoder = ort.InferenceSession(os.path.join(model_dir, "decoder_joint.onnx"), options, providers=providers)
        # model_proto: SentencePiece cannot open a non-ASCII path on Windows.
        with open(os.path.join(model_dir, "tokenizer.model"), "rb") as f:
            self._tokenizer = spm.SentencePieceProcessor(model_proto=f.read())
        self._lock = threading.Lock()

    def _step(self, frame, last, state1, state2):
        out = self._decoder.run(None, {
            "encoder_outputs": frame,
            "targets": np.array([[last]], np.int32),
            "target_length": np.array([1], np.int32),
            "input_states_1": state1,
            "input_states_2": state2,
        })
        return out[0][0, 0, 0], out[2], out[3]

    def _transcribe(self, samples: np.ndarray) -> str:
        mel, valid = log_mel(samples)
        with self._lock:
            encoded, lengths = self._encoder.run(None, {"audio_signal": mel, "length": np.array([valid], np.int64)})
            ids = greedy_tdt(self._step, encoded.astype(np.float32), int(lengths[0]))
        return self._tokenizer.decode(ids)

    def create_stream(self):
        chunks: list[np.ndarray] = []

        def accept_waveform(sample_rate: int, samples) -> None:
            data = np.asarray(samples, dtype=np.float32).reshape(-1)
            if sample_rate != SAMPLE_RATE:
                count = int(len(data) * SAMPLE_RATE / sample_rate)
                data = np.interp(np.linspace(0, len(data) - 1, count), np.arange(len(data)), data).astype(np.float32)
            chunks.append(data)

        stream = SimpleNamespace(result=SimpleNamespace(text="", tokens=[], lang="ja"))
        stream.accept_waveform = accept_waveform
        stream.waveform = lambda: np.concatenate(chunks) if chunks else np.zeros(0, dtype=np.float32)
        return stream

    def decode_stream(self, stream) -> None:
        parts = split_at_quiet_points(stream.waveform(), int(MAX_SECONDS * SAMPLE_RATE))
        stream.result.text = "".join(self._transcribe(p) for p in parts if len(p) >= MIN_SAMPLES)
