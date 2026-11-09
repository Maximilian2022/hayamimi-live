"""
cloud/engine.py
省メモリ・高効率 ReazonSpeech マルチテナント ASR エンジン
1 つの ReazonSpeech モデルをメモリ上で共有し、ユーザーごとの独立した VAD で並行発話を処理します。
"""

import logging
import os
import sys
import threading
import time
from typing import Callable, Optional

import numpy as np
import sherpa_onnx

from cloud.config import MODELS_DIR

logger = logging.getLogger("hayamimi.cloud.engine")

SAMPLE_RATE = 16000
VAD_MODEL_PATH = os.path.join(MODELS_DIR, "silero_vad.onnx")
REAZON_DIR = os.path.join(MODELS_DIR, "sherpa-onnx-zipformer-ja-en-reazonspeech-2025-01-17")


class SharedAsrEngine:
    """ReazonSpeech モデルを共有するスレッドセーフな ASR エンジン"""

    def __init__(self, models_dir: str = MODELS_DIR, threads: int = 2):
        self.models_dir = models_dir
        self.threads = threads
        self.recognizer: Optional[sherpa_onnx.OfflineRecognizer] = None
        self._lock = threading.Lock()
        self._init_recognizer()

    def _init_recognizer(self):
        encoder_path = os.path.join(REAZON_DIR, "encoder-epoch-99-avg-1.int8.onnx")
        decoder_path = os.path.join(REAZON_DIR, "decoder-epoch-99-avg-1.onnx")
        joiner_path = os.path.join(REAZON_DIR, "joiner-epoch-99-avg-1.int8.onnx")
        tokens_path = os.path.join(REAZON_DIR, "tokens.txt")

        # フォールバック (非 int8)
        if not os.path.isfile(encoder_path):
            encoder_path = os.path.join(REAZON_DIR, "encoder-epoch-99-avg-1.onnx")
        if not os.path.isfile(joiner_path):
            joiner_path = os.path.join(REAZON_DIR, "joiner-epoch-99-avg-1.onnx")

        if not all(os.path.isfile(p) for p in [encoder_path, decoder_path, joiner_path, tokens_path]):
            logger.error("ReazonSpeech model files missing in %s", REAZON_DIR)
            raise FileNotFoundError(f"ReazonSpeech model files missing in {REAZON_DIR}")

        logger.info("Loading shared ReazonSpeech model (threads=%d)...", self.threads)
        t0 = time.perf_counter()
        self.recognizer = sherpa_onnx.OfflineRecognizer.from_transducer(
            encoder=encoder_path,
            decoder=decoder_path,
            joiner=joiner_path,
            tokens=tokens_path,
            num_threads=self.threads,
            model_type="zipformer2",
            decoding_method="greedy_search",
        )
        t_load = time.perf_counter() - t0
        logger.info("✔ Shared ReazonSpeech model loaded successfully in %.2fs", t_load)

    def transcribe(self, samples: np.ndarray, sample_rate: int = 16000) -> str:
        """音声サンプル (float32 numpy array, 16kHz) を認識してテキストを返す"""
        if self.recognizer is None or len(samples) == 0:
            return ""

        with self._lock:
            stream = self.recognizer.create_stream()
            stream.accept_waveform(sample_rate, samples)
            self.recognizer.decode_stream(stream)
            text = stream.result.text.strip()
            return text


class UserVadSession:
    """ユーザーごとの独立した VAD (音声活動検出) およびセグメント収集"""

    def __init__(self, user_id: str, asr_engine: SharedAsrEngine, on_transcript: Callable[[str, bool], None]):
        self.user_id = user_id
        self.asr_engine = asr_engine
        self.on_transcript = on_transcript

        # Silero VAD 初期化
        vad_config = sherpa_onnx.VadModelConfig(
            silero_vad=sherpa_onnx.SileroVadModelConfig(
                model=VAD_MODEL_PATH,
                threshold=0.5,
                min_silence_duration=0.35,
                min_speech_duration=0.15,
                window_size=512,
                max_speech_duration=15.0,
            ),
            sample_rate=SAMPLE_RATE,
            num_threads=1,
        )
        self.vad = sherpa_onnx.VoiceActivityDetector(vad_config, buffer_size_in_seconds=30.0)
        self.is_speech = False

    def process_audio_chunk(self, chunk: np.ndarray) -> None:
        """ブラウザから届いた 16kHz float32 PCM 音声チャンクを処理する"""
        if len(chunk) == 0:
            return

        self.vad.accept_waveform(chunk)

        # 完了した音声セグメントを取り出して ASR
        while not self.vad.empty():
            segment = self.vad.front()
            samples = np.array(segment.samples, dtype=np.float32)
            self.vad.pop()

            if len(samples) >= int(SAMPLE_RATE * 0.2):  # 0.2秒以上の音声
                text = self.asr_engine.transcribe(samples, SAMPLE_RATE)
                if text:
                    self.on_transcript(text, True)

    def reset(self):
        self.vad.clear()
