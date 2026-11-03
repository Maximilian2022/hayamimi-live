"""
scripts/whisper_cuda_engine.py
Whisper CUDA (Faster-Whisper / CTranslate2 FP16) ストリーミング推論エンジン

メイン GPU (NVIDIA GeForce RTX 等) の CUDA を活用し、
スライディングバッファによる partial (300ms 軽量下書き) と
final (高精度確定・LoRA適応) の二段階ストリーミング ASR を実現します。
RoutedASR と 100% 互換性のあるインターフェースを提供します。
"""

from __future__ import annotations

import datetime
import logging
import os
import re
import sys
import threading
import time
from typing import Callable, Optional

import numpy as np

try:
    from cuda_utils import setup_cuda_dll_path
    setup_cuda_dll_path()
except Exception:
    try:
        from scripts.cuda_utils import setup_cuda_dll_path
        setup_cuda_dll_path()
    except Exception:
        pass

try:
    import itn_cjk
except ImportError:
    itn_cjk = None

try:
    import torch
    HAVE_TORCH = True
except ImportError:
    torch = None
    HAVE_TORCH = False

try:
    from faster_whisper import WhisperModel
    HAVE_FASTER_WHISPER = True
except ImportError:
    WhisperModel = None
    HAVE_FASTER_WHISPER = False

logger = logging.getLogger("whisper_cuda_engine")

# CTranslate2 / Faster-Whisper 形式の最適化済みモデル ID
DEFAULT_MODEL_SIZE = "kotoba-tech/kotoba-whisper-v2.0-faster"
FALLBACK_MODEL_SIZE = "large-v3-turbo"


# 品質判定に合格した LoRA だけが持つファイル。未合格の学習結果を字幕へ自動反映しない


class WhisperCUDAEngine:
    """
    Faster-Whisper (CTranslate2 CUDA FP16) を活用した超低遅延・高精度 ASR エンジン。
    スライディングバッファにより 300ms 周期の partial と VAD 連動の final を提供。
    """

    def __init__(
        self,
        model_size_or_path: str | None = None,
        device: str = "cuda",
        compute_type: str = "float16",
        device_index: int = 0,
        threads: int = 4,
        initial_prompt: str | None = None,
        replace_file: str | None = None,
        lora_dir: str | None = None,
        forced_lang: str = "ja",
        on_event: Callable[[dict], None] | None = None,
    ):
        cuda_requested = (device == "cuda")
        self.device = "cuda" if cuda_requested else "cpu"
        self.compute_type = compute_type if self.device == "cuda" else "int8"
        self.device_index = device_index
        self.forced_lang = forced_lang
        self.last_lang = forced_lang
        self.engine_name = "whisper_cuda"
        self.initial_prompt = initial_prompt or ""
        self.lora_dir = lora_dir
        self.on_event = on_event
        self._is_closed = False

        # RoutedASR 互換インターフェース属性
        self.dual_confirm = False
        self._punctuate = True
        self.lid_switch_confirm = 1
        self.min_switch_s = 0.0
        self._user_replacements: tuple[tuple[str, str], ...] = ()
        self._dynamic_comment_replacements: tuple[tuple[str, str], ...] = ()
        self._replacements: tuple[tuple[str, str], ...] = ()
        self._rule_hit_counts: dict[str, int] = {}
        self._rule_last_hit: dict[str, str] = {}
        self._stats_lock = threading.Lock()
        self._itn_overrides = itn_cjk.EMPTY_OVERRIDES if itn_cjk else None
        self.ko_spacer = None
        self.ko_spacer_ok = False

        # デバイス名の取得
        if HAVE_TORCH and torch.cuda.is_available():
            self.device_name = torch.cuda.get_device_name(self.device_index)
        elif cuda_requested:
            self.device_name = "NVIDIA CUDA (CTranslate2)"
        else:
            self.device_name = "CPU (Fallback)"

        if replace_file and os.path.exists(replace_file):
            self._load_replacements(replace_file)

        # モデルの初期化
        self.model = None
        self.model_path = model_size_or_path or DEFAULT_MODEL_SIZE

        # アクティブプロファイルの LoRA ディレクトリを自動探索
        if not self.lora_dir:
            try:
                from scripts.config_store import get_storage_dir, get_saved_active_speaker_id
                from scripts.lora_state import should_apply_lora
                act_sid = get_saved_active_speaker_id()
                if act_sid:
                    cand_lora = os.path.join(get_storage_dir(), "profiles", f"{act_sid}_lora")
                    if should_apply_lora(cand_lora):
                        self.lora_dir = cand_lora
                        logger.info(f"Auto-detected speaker LoRA directory: {cand_lora}")
                    elif os.path.isdir(cand_lora):
                        logger.info(f"Speaker LoRA is disabled or outdated, using base model: {cand_lora}")
            except Exception:
                pass

        # LoRA アダプタマージ済みモデルがある場合はそちらを優先
        if self.lora_dir and os.path.isdir(self.lora_dir):
            merged_ct2 = os.path.join(self.lora_dir, "ctranslate2")
            if os.path.isdir(merged_ct2):
                logger.info(f"Loading LoRA merged CTranslate2 model: {merged_ct2}")
                self.model_path = merged_ct2

        if HAVE_FASTER_WHISPER:
            candidate_models = [self.model_path]
            for fb in [DEFAULT_MODEL_SIZE, FALLBACK_MODEL_SIZE]:
                if fb not in candidate_models:
                    candidate_models.append(fb)

            # 1. CUDA でのロード試行 (候補モデル順 & compute_type 自動探索)
            if cuda_requested:
                # 指定の compute_type を最優先し、フォールバック候補を試行
                compute_types = [self.compute_type]
                for ct_fb in ["default", "int8_float16", "int8", "float32"]:
                    if ct_fb not in compute_types:
                        compute_types.append(ct_fb)

                for cand in candidate_models:
                    if self.model is not None:
                        break
                    for ct in compute_types:
                        try:
                            msg = f"Loading WhisperModel({cand}, device=cuda, compute_type={ct})..."
                            logger.info(msg)
                            print(f"[whisper_cuda] {msg}", file=sys.stderr, flush=True)
                            self.model = WhisperModel(
                                cand,
                                device="cuda",
                                device_index=self.device_index,
                                compute_type=ct,
                                cpu_threads=threads,
                            )
                            self.model_path = cand
                            self.compute_type = ct
                            self.device = "cuda"
                            if not self.device_name or self.device_name == "CPU (Fallback)":
                                self.device_name = "NVIDIA CUDA (CTranslate2)"
                            success_msg = f"WhisperCUDAEngine loaded successfully on {self.device_name} ({cand}, compute_type={ct})"
                            logger.info(success_msg)
                            print(f"[whisper_cuda] ✔ {success_msg}", file=sys.stderr, flush=True)
                            break
                        except Exception as e:
                            err_msg = f"Failed to load Faster-Whisper on cuda ({cand}, compute_type={ct}): {type(e).__name__}: {e}"
                            logger.warning(err_msg)
                            print(f"[whisper_cuda] ⚠ {err_msg}", file=sys.stderr, flush=True)

            # 2. CUDA で全滅または CPU 指定時の CPU フォールバック試行
            if self.model is None:
                self.device = "cpu"
                self.compute_type = "int8"
                self.device_name = "CPU (Fallback)"
                for cand in candidate_models:
                    try:
                        msg = f"Retrying WhisperModel({cand}) on CPU..."
                        logger.info(msg)
                        print(f"[whisper_cuda] {msg}", file=sys.stderr, flush=True)
                        self.model = WhisperModel(
                            cand,
                            device="cpu",
                            compute_type="int8",
                            cpu_threads=threads,
                        )
                        self.model_path = cand
                        success_msg = f"WhisperCUDAEngine loaded successfully on CPU fallback ({cand})"
                        logger.info(success_msg)
                        print(f"[whisper_cuda] ✔ {success_msg}", file=sys.stderr, flush=True)
                        break
                    except Exception as e2:
                        err_msg = f"Failed to load Faster-Whisper on CPU with {cand}: {type(e2).__name__}: {e2}"
                        logger.warning(err_msg)
                        print(f"[whisper_cuda] ⚠ {err_msg}", file=sys.stderr, flush=True)

            if self.model is None:
                err_fatal = "Failed to load any Faster-Whisper model on either CUDA or CPU."
                logger.error(err_fatal)
                print(f"[whisper_cuda] ✖ {err_fatal}", file=sys.stderr, flush=True)
        else:
            warn_no_fw = "faster-whisper is not installed. Running in mock/fallback mode."
            logger.warning(warn_no_fw)
            print(f"[whisper_cuda] ⚠ {warn_no_fw}", file=sys.stderr, flush=True)

        # partial キャッシュ
        self._last_partial_text = ""
        self._partial_lock = threading.Lock()

    def _load_replacements(self, path: str):
        pairs = []
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if "\t" in line:
                        p, r = line.split("\t", 1)
                        pairs.append((p.strip(), r.strip()))
                    elif "," in line:
                        p, r = line.split(",", 1)
                        pairs.append((p.strip(), r.strip()))
            self._user_replacements = tuple(pairs)
            self._rebuild_replacements()
        except Exception as e:
            logger.warning(f"Failed to load replacements from {path}: {e}")

    def _rebuild_replacements(self):
        combined = list(self._dynamic_comment_replacements) + list(self._user_replacements)
        combined.sort(key=lambda pair: len(pair[0]), reverse=True)
        self._replacements = tuple(combined)

    def set_replacements(self, mapping: list[tuple[str, str]] | dict[str, str] | None):
        if not mapping:
            self._user_replacements = ()
        elif isinstance(mapping, dict):
            self._user_replacements = tuple((str(k), str(v)) for k, v in mapping.items())
        else:
            self._user_replacements = tuple((str(k), str(v)) for k, v in mapping)
        self._rebuild_replacements()

    def set_dynamic_comment_replacements(self, mapping: list[tuple[str, str]] | dict[str, str] | None):
        if not mapping:
            self._dynamic_comment_replacements = ()
        elif isinstance(mapping, dict):
            self._dynamic_comment_replacements = tuple((str(k), str(v)) for k, v in mapping.items())
        else:
            self._dynamic_comment_replacements = tuple((str(k), str(v)) for k, v in mapping)
        self._rebuild_replacements()

    def _replace(self, text: str) -> str:
        if not text or not self._replacements:
            return text
        now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
        res = text
        for pat, rep in self._replacements:
            if pat in res:
                res = res.replace(pat, rep)
                with self._stats_lock:
                    self._rule_hit_counts[pat] = self._rule_hit_counts.get(pat, 0) + 1
                    self._rule_last_hit[pat] = now_iso
        return res

    def set_forced_lang(self, lang: str | None):
        self.forced_lang = lang or "ja"
        self.last_lang = self.forced_lang

    def set_dual_confirm(self, enabled: bool):
        self.dual_confirm = bool(enabled)

    def set_punctuate(self, enabled: bool):
        self._punctuate = bool(enabled)

    def set_lid_switch_confirm(self, n: int):
        self.lid_switch_confirm = max(1, int(n))

    def set_min_switch_s(self, sec: float):
        self.min_switch_s = max(0.0, float(sec))

    def set_itn_overrides(self, exclude_tags=None, force_tags=None):
        if itn_cjk:
            self._itn_overrides = itn_cjk.ITNOverrides(
                exclude_tags=set(exclude_tags or ()),
                force_tags=set(force_tags or ()),
            )

    def reset_session(self):
        self._last_partial_text = ""

    def identify(self, samples: np.ndarray, sample_rate: int = 16000) -> str:
        return self.forced_lang or "ja"

    def _identify_lang(self, samples: np.ndarray, sample_rate: int = 16000) -> str:
        return self.forced_lang or "ja"

    def set_initial_prompt(self, prompt: str | None) -> None:
        self.initial_prompt = prompt or ""

    def get_replacements(self) -> dict[str, str]:
        return dict(self._replacements)

    def rule_hit_stats(self) -> tuple[dict[str, int], dict[str, str]]:
        with self._stats_lock:
            return dict(self._rule_hit_counts), dict(self._rule_last_hit)

    def partial(
        self,
        samples: np.ndarray,
        sample_rate: int = 16000,
        lang_hint: str | None = None,
        known_lang: str | None = None,
        speech_s: float | None = None,
        **kwargs,
    ) -> str:
        """
        スライディングバッファ直近音声の高速 partial デコード (300ms 周期、beam=1)
        """
        if self._is_closed or self.model is None or len(samples) < 3200:
            return self._last_partial_text

        # 16kHz float32 正規化
        if samples.dtype == np.int16:
            samples = samples.astype(np.float32) / 32768.0
        elif samples.dtype != np.float32:
            samples = samples.astype(np.float32)

        # 直近最大 2.5 秒分にトリミングして軽量化
        max_samples = int(sample_rate * 2.5)
        if len(samples) > max_samples:
            samples = samples[-max_samples:]

        try:
            # 高速 Greedy デコード (beam_size=1, max_new_tokens=24)
            target_lang = lang_hint or known_lang or self.forced_lang or "ja"
            segments, _ = self.model.transcribe(
                samples,
                language=target_lang,
                beam_size=1,
                best_of=1,
                temperature=0.0,
                without_timestamps=True,
                initial_prompt=self.initial_prompt or None,
                vad_filter=True,  # Silero VAD によるハルシネーション遮断
            )
            text_parts = [s.text for s in segments]
            raw_text = "".join(text_parts).strip()
            if raw_text:
                replaced = self._replace(raw_text)
                self._last_partial_text = replaced
            return self._last_partial_text
        except Exception as e:
            logger.debug(f"Partial decode error: {e}")
            return self._last_partial_text

    def transcribe(
        self,
        samples: np.ndarray,
        sample_rate: int = 16000,
        known_lang: str | None = None,
        speech_s: float | None = None,
        live: bool = True,
        second_opinion: bool | None = None,
        **kwargs,
    ) -> dict:
        """
        発話区切り (VAD 無音検知) で確定する本推論 (final)
        """
        if self._is_closed:
            raise RuntimeError("WhisperCUDAEngine is closed")

        if len(samples) == 0:
            return {
                "text": "",
                "lang": self.forced_lang,
                "tier": "cuda_whisper",
                "lid_ms": 0.0,
                "decode_ms": 0.0,
                "probe_ms": 0.0,
            }

        t0 = time.perf_counter()

        # float32 正規化
        if samples.dtype == np.int16:
            samples = samples.astype(np.float32) / 32768.0
        elif samples.dtype != np.float32:
            samples = samples.astype(np.float32)

        if sample_rate != 16000:
            target_len = int(len(samples) * 16000 / sample_rate)
            samples = np.interp(
                np.linspace(0, len(samples), target_len),
                np.arange(len(samples)),
                samples,
            ).astype(np.float32)

        if len(samples) < 1600 or self.model is None:
            return {
                "text": "",
                "lang": self.forced_lang,
                "tier": "cuda_whisper",
                "lid_ms": 0.0,
                "decode_ms": 0.0,
                "probe_ms": 0.0,
            }

        # 本推論 (beam_size=1〜2, Silero VAD フィルタ適用)
        effective_lang = known_lang or self.forced_lang or "ja"
        try:
            segments, info = self.model.transcribe(
                samples,
                language=effective_lang,
                beam_size=1,
                best_of=1,
                temperature=0.0,
                initial_prompt=self.initial_prompt or None,
                vad_filter=True,
                vad_parameters=dict(min_silence_duration_ms=400),
            )
            raw_text = "".join(s.text for s in segments).strip()
        except Exception as e:
            logger.warning(f"CUDA transcribe error: {e}")
            raw_text = self._last_partial_text

        # 置換辞書・ITN 適用
        text = self._replace(raw_text)
        decode_ms = (time.perf_counter() - t0) * 1000

        result = {
            "text": text,
            "lang": effective_lang,
            "tier": "cuda_whisper",
            "lid_ms": 0.0,
            "decode_ms": decode_ms,
            "probe_ms": 0.0,
        }

        if self.on_event:
            try:
                self.on_event({
                    "type": "asr_final",
                    "text": text,
                    "lang": self.forced_lang,
                    "decode_ms": decode_ms,
                    "device": self.device_name,
                })
            except Exception:
                pass

        self._last_partial_text = ""
        return result

    def close(self) -> None:
        self._is_closed = True
        self.model = None

