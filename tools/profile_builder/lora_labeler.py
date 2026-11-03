"""
tools/profile_builder/lora_labeler.py
声紋で切り出した本人の発話区間に、Gemini で LoRA 学習用の正解文を付ける。

音声全体を 1 回で送ると混雑時に 503 になりやすいため、少数の区間ごとに送る。
区間の境界は変えさせない。Gemini の時刻情報はずれるため使わない。
"""

from __future__ import annotations

import logging
import os
import re
import time
import unicodedata
from typing import Callable, Optional, Sequence

import numpy as np
import soundfile as sf

from tools.profile_builder.gemini_analyzer import safe_parse_json
from tools.profile_builder.local_transcribe import TranscribedSegment

logger = logging.getLogger("lora_labeler")

BATCH_SEGMENTS = 10
MAX_OUTPUT_TOKENS = 16384
OVERLOAD_RETRY_DELAY_SEC = 30.0
MAX_CONSECUTIVE_FAILURES = 2
# これより文字起こしと食い違う区間は、読み上げの混入か聞き違いの可能性が高い
MAX_DISAGREEMENT = 0.6
READ_ALOUD_MARKER = re.compile(r"わら|笑|ｗ")

PROMPT_HEADER = (
    "これは日本語の配信者の発話を区間ごとに切り出し、1秒の無音を挟んで連結した音声です。\n"
    "下の一覧は 区間ID、連結音声での開始秒、音声認識の結果(誤りを含む)です。\n"
    "各区間で実際に話された言葉を、音声を聞いて逐語で書いてください。\n"
    "規則:\n"
    "- 区間の分割や順序を変えない。すべての区間IDについて1件ずつ返す。\n"
    "- 要約、言い換え、文法修正をしない。言いよどみ、言い直し、語尾もそのまま書く。\n"
    "- 音声認識の誤りは直す。ただし聞こえない言葉を文脈から補わない。\n"
    "- kind は speech(配信者の発話)、read_aloud(コメント読み上げの合成音声)、"
    "lyrics(BGMの歌)、mixed(複数が重なり書き起こせない)、none(発話なし)のいずれか。\n"
    "- speech 以外は text を空にしてよい。\n"
    '次の JSON だけを出力: {"segments":[{"id":0,"text":"","kind":"speech"}]}\n\n'
    "区間一覧:\n"
)


def _normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).lower()
    return re.sub(r"[\s、。,.!?「」『』…・ー~〜]", "", text)


def disagreement(reference: str, hypothesis: str) -> float:
    """Character error rate of hypothesis against reference, ignoring punctuation."""
    ref, hyp = _normalize(reference), _normalize(hypothesis)
    if not ref:
        return 1.0
    previous = list(range(len(hyp) + 1))
    for i, r in enumerate(ref, 1):
        current = [i]
        for j, h in enumerate(hyp, 1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (r != h)))
        previous = current
    return previous[-1] / len(ref)


def transcribe_ranges(audio: np.ndarray, sr: int, segments: Sequence[tuple[float, float]]) -> list[str]:
    """Transcribe each range on its own with the live Whisper model.

    プロファイル作成時の文字起こしは 12 秒単位で区間と揃わず、前後の別の発話が混ざるため使わない。
    GPU モード専用。使えない環境では空文字を返し、照合なしで続行する。
    """
    empty = ["" for _ in segments]
    try:
        from faster_whisper import WhisperModel
        from scripts.cuda_utils import setup_cuda_dll_path
        from scripts.whisper_cuda_engine import DEFAULT_MODEL_SIZE

        setup_cuda_dll_path()
        model = WhisperModel(DEFAULT_MODEL_SIZE, device="cuda", compute_type="float16")
    except Exception as e:
        logger.warning("Whisper is unavailable for LoRA label hints: %s", e)
        return empty

    hints = []
    try:
        for start, end in segments:
            parts, _ = model.transcribe(
                audio[int(start * sr) : int(end * sr)],
                language="ja",
                beam_size=1,
                vad_filter=False,
                condition_on_previous_text=False,
            )
            hints.append("".join(p.text for p in parts).strip())
    except Exception as e:
        logger.warning("Whisper failed while preparing LoRA label hints: %s", e)
        return empty
    finally:
        del model
    return hints


def is_usable_label(text: str, kind: str, hint: str) -> bool:
    """Keep only segments whose audio can be trusted to match the label."""
    if kind != "speech" or not _normalize(text):
        return False
    if not hint:
        return True
    # 文字起こしだけにコメント調の語がある区間は、読み上げが混ざっている
    if READ_ALOUD_MARKER.search(hint) and not READ_ALOUD_MARKER.search(text):
        return False
    return disagreement(text, hint) <= MAX_DISAGREEMENT


def choose_label_model(analysis_model: str, default_model: str) -> str:
    """Lite models returned misaligned text in testing, so they never write training labels."""
    return default_model if "lite" in analysis_model.lower() else analysis_model


def _is_fatal(exc: Exception) -> bool:
    """Quota and credential errors repeat on every request, so retrying only wastes quota."""
    message = str(exc)
    return any(code in message for code in ("429", "RESOURCE_EXHAUSTED", "401", "403", "API_KEY_INVALID"))


def _request_batch(client, model: str, wav_path: str, prompt: str) -> dict:
    from google.genai import types

    uploaded = client.files.upload(file=wav_path, config={"mime_type": "audio/wav"})
    try:
        while getattr(uploaded.state, "name", str(uploaded.state)) == "PROCESSING":
            time.sleep(2)
            uploaded = client.files.get(name=uploaded.name)
        response = client.models.generate_content(
            model=model,
            contents=[uploaded, prompt],
            config=types.GenerateContentConfig(
                temperature=0.0,
                max_output_tokens=MAX_OUTPUT_TOKENS,
                response_mime_type="application/json",
            ),
        )
        return safe_parse_json(response.text)
    finally:
        try:
            client.files.delete(name=uploaded.name)
        except Exception as e:
            logger.warning("Failed to delete uploaded LoRA label audio: %s", e)


def label_segments_with_gemini(
    client,
    model: str,
    audio: np.ndarray,
    sr: int,
    segments: Sequence[tuple[float, float]],
    hints: Sequence[str],
    work_dir: str,
    log_callback: Optional[Callable[[str, str], None]] = None,
) -> list[TranscribedSegment]:
    """Return labelled segments. Stops early on quota errors and keeps what succeeded."""

    def log(level: str, message: str) -> None:
        if log_callback:
            log_callback(level, message)

    labelled: list[TranscribedSegment] = []
    failures = 0
    gap = np.zeros(sr, dtype=np.float32)

    for first in range(0, len(segments), BATCH_SEGMENTS):
        batch = list(enumerate(segments[first : first + BATCH_SEGMENTS], first))
        parts, lines, cursor = [], [], 0.0
        for seg_id, (start, end) in batch:
            lines.append(f"{seg_id}\t{cursor:.1f}\t{hints[seg_id]}")
            parts += [audio[int(start * sr) : int(end * sr)], gap]
            cursor += (end - start) + 1.0

        wav_path = os.path.join(work_dir, f"lora_label_{first}.wav")
        sf.write(wav_path, np.concatenate(parts), sr)
        prompt = PROMPT_HEADER + "\n".join(lines)

        data = None
        for attempt in range(2):
            try:
                data = _request_batch(client, model, wav_path, prompt)
                break
            except Exception as e:
                logger.warning("LoRA label batch from %d failed: %s", first, e)
                if _is_fatal(e):
                    log("WARN", f"Gemini のラベル作成を中断しました: {str(e)[:120]}")
                    return labelled
                # 混雑や応答の崩れは一時的なので、1 度だけやり直す
                if attempt == 0:
                    time.sleep(OVERLOAD_RETRY_DELAY_SEC)
        if os.path.exists(wav_path):
            os.remove(wav_path)

        if data is None:
            failures += 1
            log("WARN", "Gemini から有効な応答がなかったため、このラベル作成の区間を飛ばしました。")
            if failures >= MAX_CONSECUTIVE_FAILURES:
                log("WARN", "Gemini の失敗が続いたため、ラベル作成を中断しました。")
                return labelled
            continue
        failures = 0

        by_id = {}
        # モデルによっては {"segments": [...]} ではなく配列をそのまま返す
        items = data.get("segments", []) if isinstance(data, dict) else data
        for item in items if isinstance(items, list) else []:
            try:
                by_id[int(item["id"])] = item
            except (KeyError, TypeError, ValueError):
                continue
        for seg_id, (start, end) in batch:
            item = by_id.get(seg_id, {})
            text = str(item.get("text") or "").strip()
            if is_usable_label(text, str(item.get("kind") or ""), hints[seg_id]):
                labelled.append(TranscribedSegment(start, end, text))

    return labelled
