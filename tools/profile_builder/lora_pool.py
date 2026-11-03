"""
tools/profile_builder/lora_pool.py
LoRA の学習ラベルと音声を動画ごとに保存し、複数の動画から学習し直せるようにする。

1 本ごとの追加学習では前の動画の内容を少しずつ忘れるため、毎回プール全体で最初から学習する。
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Sequence

import numpy as np
import soundfile as sf

from tools.profile_builder.local_transcribe import TranscribedSegment

logger = logging.getLogger("lora_pool")

POOL_DIRNAME = "label_pool"
# 1 本あたり FLAC で約 10MB。話し方の変化に追従するよう古い動画から消す
MAX_POOL_VIDEOS = 6
# 本人の声がこれより少ない動画はコラボの可能性が高く、Gemini に送らない
MIN_SPEAKER_RATIO = 0.5
# これより少ないラベルで作ったモデルは字幕を崩しかねないので、たまるまで学習しない
MIN_TRAINING_SEGMENTS = 25
_UNSAFE_ID = re.compile(r"[^A-Za-z0-9_-]")


def speaker_ratio(ranges: Sequence[tuple[float, float]], duration_sec: float) -> float:
    if duration_sec <= 0:
        return 0.0
    return sum(end - start for start, end in ranges) / duration_sec


def _pool_dir(lora_dir: str) -> str:
    return os.path.join(lora_dir, POOL_DIRNAME)


def _saved_labels(lora_dir: str) -> list[str]:
    pool = _pool_dir(lora_dir)
    if not os.path.isdir(pool):
        return []
    paths = [os.path.join(pool, n) for n in os.listdir(pool) if n.endswith(".json")]
    return sorted(paths, key=os.path.getmtime)


def add_video(
    lora_dir: str,
    video_id: str,
    audio: np.ndarray,
    sr: int,
    labelled: Sequence[TranscribedSegment],
) -> None:
    """Save the labelled parts of one video, replacing an earlier run of the same video."""
    pool = _pool_dir(lora_dir)
    os.makedirs(pool, exist_ok=True)
    stem = os.path.join(pool, _UNSAFE_ID.sub("_", video_id))

    parts, saved, cursor = [], [], 0.0
    for seg in labelled:
        chunk = audio[int(seg.start_sec * sr) : int(seg.end_sec * sr)]
        if len(chunk) == 0:
            continue
        parts.append(chunk)
        length = len(chunk) / sr
        saved.append({"start": cursor, "end": cursor + length, "text": seg.text})
        cursor += length
    if not parts:
        return

    sf.write(stem + ".flac", np.concatenate(parts), sr)
    # JSON があるものだけを読むので、音声の後に書けば途中で止まっても壊れない
    with open(stem + ".json", "w", encoding="utf-8") as f:
        json.dump({"sr": sr, "segments": saved}, f, ensure_ascii=False)

    for old in _saved_labels(lora_dir)[:-MAX_POOL_VIDEOS]:
        for path in (old, old[: -len(".json")] + ".flac"):
            if os.path.exists(path):
                os.remove(path)


def video_count(lora_dir: str) -> int:
    return len(_saved_labels(lora_dir))


def load_pool(lora_dir: str, target_sr: int = 16000) -> tuple[np.ndarray, list[TranscribedSegment]]:
    """Return all pooled audio joined together and segments pointing into it."""
    parts: list[np.ndarray] = []
    segments: list[TranscribedSegment] = []
    offset = 0.0
    for label_path in _saved_labels(lora_dir):
        try:
            with open(label_path, encoding="utf-8") as f:
                saved = json.load(f)
            audio, sr = sf.read(label_path[: -len(".json")] + ".flac", dtype="float32")
            if sr != target_sr:
                raise ValueError(f"unexpected sample rate {sr}")
            items = [TranscribedSegment(offset + s["start"], offset + s["end"], s["text"]) for s in saved["segments"]]
        except (OSError, ValueError, KeyError, TypeError, RuntimeError) as e:
            logger.warning("Skipping unreadable LoRA pool entry %s: %s", label_path, e)
            continue
        parts.append(audio)
        segments += items
        offset += len(audio) / sr
    if not parts:
        return np.zeros(0, dtype=np.float32), []
    return np.concatenate(parts), segments
