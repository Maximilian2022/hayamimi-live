# -*- coding: utf-8 -*-
"""Voiceprint extraction module for speaker profiles using CAM++ (sherpa-onnx)."""

import logging
import os
from typing import Callable, List, Optional, Tuple

import numpy as np
import soundfile as sf

logger = logging.getLogger(__name__)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MIN_CALIBRATION_WINDOWS = 8
CALIBRATION_MARGIN = 0.10
MIN_VOICEPRINT_THRESHOLD = 0.45
MAX_VOICEPRINT_THRESHOLD = 0.70
SAME_VOICE_SIMILARITY = 0.5
MAX_SEGMENT_GAP_SEC = 1.0
MIN_LABEL_SEGMENT_SEC = 1.5
MAX_LABEL_SEGMENT_SEC = 12.0
MIN_DOMINANT_GROUP = 3


def dominant_voice_group(embeddings: List[np.ndarray]) -> List[np.ndarray]:
    """Return the largest group of similar embeddings: the main speaker of a one-person stream.

    Comment read-aloud voices and BGM vocals form their own smaller groups.
    """
    groups: List[List[np.ndarray]] = []
    centers: List[np.ndarray] = []
    for emb in embeddings:
        sims = [float(np.dot(emb, c) / np.linalg.norm(c)) for c in centers]
        if sims and max(sims) >= SAME_VOICE_SIMILARITY:
            best = int(np.argmax(sims))
            groups[best].append(emb)
            centers[best] = centers[best] + emb
        else:
            groups.append([emb])
            centers.append(np.array(emb, dtype=np.float32))
    return max(groups, key=len) if groups else []
DEFAULT_CAM_MODEL = os.path.join(REPO_ROOT, "models", "campplus_sv.onnx")


class VoiceprintExtractor:
    """Extracts speaker embedding (voiceprint) from audio files using CAM++."""

    def __init__(self, model_path: str = DEFAULT_CAM_MODEL, num_threads: int = 2):
        self.model_path = model_path
        self.num_threads = num_threads
        self._extractor = None

        if os.path.isfile(self.model_path):
            try:
                import sherpa_onnx
                cfg = sherpa_onnx.SpeakerEmbeddingExtractorConfig(
                    model=self.model_path, num_threads=self.num_threads
                )
                self._extractor = sherpa_onnx.SpeakerEmbeddingExtractor(cfg)
                logger.info("Initialized CAM++ VoiceprintExtractor with %s", self.model_path)
            except Exception as e:
                logger.warning("Failed to initialize sherpa-onnx SpeakerEmbeddingExtractor: %s", e)
        else:
            logger.info("CAM++ model not found at %s. Voiceprint extraction will be skipped.", self.model_path)

    @property
    def is_available(self) -> bool:
        return self._extractor is not None

    def compute_chunk_embedding(self, chunk: np.ndarray, sr: int = 16000) -> Optional[np.ndarray]:
        """Compute L2-normalized 512-dim embedding for a single audio chunk."""
        if not self.is_available or len(chunk) < int(0.5 * sr):
            return None
        try:
            # Resample if needed
            if sr != 16000:
                from scipy import signal
                num_samples = int(len(chunk) * 16000 / sr)
                chunk = signal.resample(chunk, num_samples)
                sr = 16000

            # Cap length to 6s
            max_len = 6 * sr
            if len(chunk) > max_len:
                chunk = chunk[:max_len]

            stream = self._extractor.create_stream()
            stream.accept_waveform(sr, chunk)
            stream.input_finished()
            emb = np.asarray(self._extractor.compute(stream), dtype=np.float32)
            norm = np.linalg.norm(emb)
            if norm > 1e-9:
                return emb / norm
            return None
        except Exception as e:
            logger.debug("Failed to compute embedding for chunk: %s", e)
            return None

    @staticmethod
    def cosine_similarity(v1: np.ndarray | List[float], v2: np.ndarray | List[float]) -> float:
        """Calculate cosine similarity between two L2-normalized vectors."""
        a = np.asarray(v1, dtype=np.float32)
        b = np.asarray(v2, dtype=np.float32)
        norm_a = np.linalg.norm(a)
        norm_b = np.linalg.norm(b)
        if norm_a < 1e-9 or norm_b < 1e-9:
            return 0.0
        return float(np.dot(a, b) / (norm_a * norm_b))

    def calibrate_threshold(self, wav_path: str) -> Optional[float]:
        """Derive a speaker-specific threshold from how similar the speaker is to themselves.

        A centroid from the first half is compared with windows from the second half, so
        the score is not inflated by testing on the same audio. Returns None when the
        audio has too little speech to calibrate.
        """
        if not self.is_available:
            return None
        try:
            audio, sr = sf.read(wav_path, dtype="float32")
        except Exception as e:
            logger.warning("Failed to read audio for threshold calibration: %s", e)
            return None
        if audio.ndim > 1:
            audio = np.mean(audio, axis=1)

        win, hop = int(4.0 * sr), int(3.0 * sr)
        embeddings = []
        for start in range(0, len(audio) - win + 1, hop):
            chunk = audio[start : start + win]
            if np.sqrt(np.mean(chunk**2)) > 0.015:
                emb = self.compute_chunk_embedding(chunk, sr=sr)
                if emb is not None:
                    embeddings.append(emb)
        main_voice = dominant_voice_group(embeddings)
        if len(main_voice) < MIN_CALIBRATION_WINDOWS:
            return None

        half = len(main_voice) // 2
        centroid = np.mean(main_voice[:half], axis=0)
        sims = [self.cosine_similarity(e, centroid) for e in main_voice[half:]]
        threshold = float(np.percentile(sims, 5)) - CALIBRATION_MARGIN
        return float(np.clip(threshold, MIN_VOICEPRINT_THRESHOLD, MAX_VOICEPRINT_THRESHOLD))

    def find_speaker_segments(
        self,
        audio: np.ndarray,
        sr: int,
        voiceprint: List[float],
        threshold: float,
    ) -> List[Tuple[float, float]]:
        """Return (start_sec, end_sec) ranges where the voiceprint's owner is speaking.

        Scores short sliding windows, so a read-aloud voice or BGM vocals between two
        utterances are cut out rather than hidden inside a long transcription segment.
        """
        win, hop = int(3.0 * sr), sr
        marked: List[Tuple[float, float]] = []
        for start in range(0, len(audio) - win + 1, hop):
            chunk = audio[start : start + win]
            if np.sqrt(np.mean(chunk**2)) < 0.015:
                continue
            emb = self.compute_chunk_embedding(chunk, sr=sr)
            if emb is not None and self.cosine_similarity(emb, voiceprint) >= threshold:
                # 窓の端は隣の音声を含むことがあるため、中央だけを本人の区間として扱う
                t = start / sr
                marked.append((t + 0.5, t + 2.5))

        merged: List[List[float]] = []
        for begin, end in marked:
            if merged and begin - merged[-1][1] <= MAX_SEGMENT_GAP_SEC:
                merged[-1][1] = end
            else:
                merged.append([begin, end])

        segments: List[Tuple[float, float]] = []
        for begin, end in merged:
            while end - begin > MAX_LABEL_SEGMENT_SEC:
                cut = self._quietest_point(audio, sr, begin + MAX_LABEL_SEGMENT_SEC / 2, begin + MAX_LABEL_SEGMENT_SEC)
                segments.append((begin, cut))
                begin = cut
            if end - begin >= MIN_LABEL_SEGMENT_SEC:
                segments.append((begin, end))
        return segments

    @staticmethod
    def _quietest_point(audio: np.ndarray, sr: int, begin: float, end: float) -> float:
        """Find the quietest 100 ms between begin and end, so splits avoid cutting words."""
        frame = sr // 10
        first, last = int(begin * sr), int(end * sr)
        best_rms, best_pos = float("inf"), first
        for pos in range(first, max(first + 1, last - frame), frame):
            rms = float(np.sqrt(np.mean(audio[pos : pos + frame] ** 2)))
            if rms < best_rms:
                best_rms, best_pos = rms, pos
        return (best_pos + frame / 2) / sr

    def extract_voiceprint(
        self,
        wav_path: str,
        target_sample_rate: int = 16000,
        max_samples: int = 15,
        min_segment_sec: float = 2.0,
        max_segment_sec: float = 6.0,
        dominant_voice_only: bool = False,
        log_callback: Optional[Callable[[str, str], None]] = None,
    ) -> Tuple[Optional[List[float]], int]:
        """Extract average L2-normalized 512-dim voiceprint vector from audio."""
        if not self.is_available:
            if log_callback:
                log_callback("WARN", "声紋モデル (campplus_sv.onnx) が未配備のため、声紋学習はスキップされました。")
            return None, 0

        if not os.path.isfile(wav_path):
            logger.warning("Audio file does not exist: %s", wav_path)
            return None, 0

        try:
            audio, sr = sf.read(wav_path, dtype="float32")
        except Exception as e:
            logger.error("Failed to read audio file %s: %s", wav_path, e)
            if log_callback:
                log_callback("WARN", f"音声ファイルの読み込みに失敗しました: {e}")
            return None, 0

        # Convert stereo to mono
        if audio.ndim > 1:
            audio = np.mean(audio, axis=1)

        # Resample if needed (standard is 16kHz for CAM++)
        if sr != target_sample_rate:
            try:
                from scipy import signal
                num_samples = int(len(audio) * target_sample_rate / sr)
                audio = signal.resample(audio, num_samples)
                sr = target_sample_rate
            except Exception as e:
                logger.warning("Failed to resample audio with scipy: %s", e)

        # Slice into candidate speech segments with high energy (RMS)
        chunk_len = int(max_segment_sec * sr)
        step_len = int(chunk_len * 0.75)
        candidates = []

        total_samples = len(audio)
        for start_idx in range(0, total_samples - chunk_len, step_len):
            chunk = audio[start_idx : start_idx + chunk_len]
            rms = np.sqrt(np.mean(chunk**2))
            if rms > 0.015:  # Non-silent speech threshold
                candidates.append((rms, chunk))

        if not candidates:
            if log_callback:
                log_callback("WARN", "発話区間の音圧が不足していたため、声紋を抽出できませんでした。")
            return None, 0

        # Sort by energy and take top N segments to avoid low-volume noise
        candidates.sort(key=lambda x: x[0], reverse=True)
        selected_chunks = [c[1] for c in candidates[:max_samples]]

        embeddings = []
        for chunk in selected_chunks:
            try:
                stream = self._extractor.create_stream()
                stream.accept_waveform(sr, chunk)
                stream.input_finished()
                emb = np.asarray(self._extractor.compute(stream), dtype=np.float32)
                norm = np.linalg.norm(emb)
                if norm > 1e-9:
                    emb = emb / norm
                    embeddings.append(emb)
            except Exception as e:
                logger.debug("Failed to extract embedding for chunk: %s", e)

        if not embeddings:
            return None, 0

        if dominant_voice_only:
            main_voice = dominant_voice_group(embeddings)
            if len(main_voice) >= MIN_DOMINANT_GROUP:
                embeddings = main_voice

        # Calculate centroid
        centroid = np.mean(embeddings, axis=0)
        norm = float(np.linalg.norm(centroid))
        if norm > 1e-9:
            centroid = centroid / norm

        if log_callback:
            log_callback(
                "VOICE",
                f"CAM++ 声紋ベクトル抽出完了: {len(embeddings)} 区間の発話から 512 次元の話者特徴量を学習しました。",
            )

        return centroid.tolist(), len(embeddings)
