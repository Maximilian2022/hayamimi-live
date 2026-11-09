"""
cloud/engine.py
省メモリ・高効率 ReazonSpeech マルチテナント ASR エンジン
1 つの ReazonSpeech モデルをメモリ上で共有し、ユーザーごとの独立した VAD で並行発話を処理します。
"""

import glob
import logging
import os
import sys
import threading
import time
from typing import Callable, Optional

# scripts をインポートパスに追加
_SCRIPTS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

import numpy as np
import sherpa_onnx

from cloud.config import MODELS_DIR

logger = logging.getLogger("hayamimi.cloud.engine")

SAMPLE_RATE = 16000
VAD_MODEL_PATH = os.path.join(MODELS_DIR, "silero_vad.onnx")
REAZON_DIR = os.path.join(MODELS_DIR, "sherpa-onnx-zipformer-ja-en-reazonspeech-2025-01-17")
PUNCT_DIR = os.path.join(MODELS_DIR, "mojicast-punct-onnx")


class SharedPunctuator:
    """Mojicast tohoku-nlp による日本語句読点復元器 (遅延・共有ロード)"""

    def __init__(self, model_dir: str = PUNCT_DIR):
        self.model_dir = model_dir
        self._punctuator = None
        self._failed = False
        self._lock = threading.Lock()

    def restore(self, text: str) -> str:
        if not text or self._failed:
            return text
        if self._punctuator is None:
            with self._lock:
                if self._punctuator is None and not self._failed:
                    try:
                        from pathlib import Path
                        from punct_ja import PunctuatorJa
                        if not os.path.isdir(self.model_dir):
                            logger.info("Punctuation model dir not found: %s", self.model_dir)
                            self._failed = True
                            return text
                        logger.info("Loading shared Mojicast punctuation model...")
                        t0 = time.perf_counter()
                        self._punctuator = PunctuatorJa(model_dir=Path(self.model_dir))
                        logger.info("✔ Punctuation model loaded in %.2fs", time.perf_counter() - t0)
                    except Exception as e:
                        logger.warning("Could not load punctuation model: %s", e)
                        self._failed = True
                        return text

        if self._punctuator is not None:
            try:
                return self._punctuator.restore(text)
            except Exception as e:
                logger.debug("Punctuation restore error: %s", e)
        return text


def _find(model_dir: str, pattern: str) -> str:
    hits = glob.glob(os.path.join(model_dir, pattern))
    return hits[0] if hits else ""


class SharedAsrEngine:
    """ReazonSpeech モデルを共有するスレッドセーフな ASR エンジン"""

    def __init__(self, models_dir: str = MODELS_DIR, threads: int = 2):
        self.models_dir = models_dir
        self.threads = threads
        self.recognizer: Optional[sherpa_onnx.OfflineRecognizer] = None
        self._lock = threading.Lock()
        self._init_recognizer()

    def _init_recognizer(self):
        encoder_path = _find(REAZON_DIR, "encoder-*.int8.onnx") or _find(REAZON_DIR, "encoder-*.onnx")
        decoder_path = _find(REAZON_DIR, "decoder-*.int8.onnx") or _find(REAZON_DIR, "decoder-*.onnx")
        joiner_path = _find(REAZON_DIR, "joiner-*.int8.onnx") or _find(REAZON_DIR, "joiner-*.onnx")
        tokens_path = os.path.join(REAZON_DIR, "tokens.txt")

        if not (encoder_path and decoder_path and joiner_path and os.path.isfile(tokens_path)):
            logger.error("ReazonSpeech model files missing in %s", REAZON_DIR)
            raise FileNotFoundError(f"ReazonSpeech model files missing in {REAZON_DIR}")

        logger.info("Loading shared ReazonSpeech model (threads=%d)...", self.threads)
        logger.info("  encoder: %s", os.path.basename(encoder_path))
        logger.info("  decoder: %s", os.path.basename(decoder_path))
        logger.info("  joiner:  %s", os.path.basename(joiner_path))
        t0 = time.perf_counter()
        self.recognizer = sherpa_onnx.OfflineRecognizer.from_transducer(
            encoder=encoder_path,
            decoder=decoder_path,
            joiner=joiner_path,
            tokens=tokens_path,
            num_threads=self.threads,
            model_type="zipformer",
            decoding_method="modified_beam_search",
            modeling_unit="cjkchar",
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
        self.audio_pos = 0.0
        self.last_partial_pos = 0.0
        self.last_draft_text = ""

    def process_audio_chunk(self, chunk: np.ndarray) -> None:
        """ブラウザから届いた 16kHz float32 PCM 音声チャンクを処理する"""
        if len(chunk) == 0:
            return

        self.vad.accept_waveform(chunk)
        self.audio_pos += len(chunk) / SAMPLE_RATE

        # 1. 発話中のリアルタイム追随字幕 (ドラフト認識: 0.4秒間隔)
        if self.vad.is_speech_detected() and (self.audio_pos - self.last_partial_pos) >= 0.40:
            try:
                cur_segment = self.vad.current_segment
                cur_samples = np.array(cur_segment.samples, dtype=np.float32)
                if len(cur_samples) > int(8.0 * SAMPLE_RATE):
                    cur_samples = cur_samples[-int(8.0 * SAMPLE_RATE):]

                if len(cur_samples) >= int(0.25 * SAMPLE_RATE):
                    draft_text = self.asr_engine.transcribe(cur_samples, SAMPLE_RATE)
                    if draft_text and draft_text != self.last_draft_text:
                        self.last_draft_text = draft_text
                        self.on_transcript(draft_text, False)
                    self.last_partial_pos = self.audio_pos
            except Exception as e:
                logger.debug("Partial transcription error: %s", e)

        # 2. 完了した確定音声セグメントを取り出して ASR
        while not self.vad.empty():
            segment = self.vad.front  # プロパティ参照 (呼び出し不可)
            samples = np.array(segment.samples, dtype=np.float32)
            self.vad.pop()
            self.last_draft_text = ""

            if len(samples) >= int(SAMPLE_RATE * 0.2):  # 0.2秒以上の音声
                text = self.asr_engine.transcribe(samples, SAMPLE_RATE)
                if text:
                    self.on_transcript(text, True)

    def reset(self):
        try:
            while not self.vad.empty():
                self.vad.pop()
            self.vad.reset()
            self.last_draft_text = ""
            self.last_partial_pos = 0.0
            self.audio_pos = 0.0
        except Exception:
            pass


shared_punctuator = SharedPunctuator()
