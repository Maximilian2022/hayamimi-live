"""
scripts/whisper_dml_engine.py
Whisper DirectML 推論エンジン (Kotoba-Whisper-v2.2 / DirectML iGPU)

メイン GPU (GeForce RTX 等) に一切負荷を与えず (負荷 0%)、
Intel / AMD 内蔵 GPU (DirectML) 単体で高速な音声認識推論を実行します。
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
import torch

try:
    import itn_cjk
except ImportError:
    itn_cjk = None

try:
    from transformers import AutoProcessor, GenerationConfig
    from optimum.onnxruntime import ORTModelForSpeechSeq2Seq
    import onnxruntime as ort
    HAVE_DML_DEPENDENCIES = True
except ImportError:
    AutoProcessor = None
    GenerationConfig = None
    ORTModelForSpeechSeq2Seq = None
    ort = None
    HAVE_DML_DEPENDENCIES = False

from scripts.hardware_probe import resolve_igpu_directml_device_id

logger = logging.getLogger("whisper_dml_engine")

DEFAULT_MODEL_ID = "onnx-community/kotoba-whisper-v2.2-ONNX"


class WhisperDirectMLEngine:
    """
    DirectML を利用した Kotoba-Whisper-v2.2 高速音声認識エンジン。
    メイン GPU には一切触れず、内蔵 GPU (iGPU) 単体で推論を実行します。
    RoutedASR と互換性のあるインターフェースを提供します。
    """

    def __init__(
        self,
        model_dir: str | None = None,
        device_id: int | None = None,
        threads: int = 4,
        initial_prompt: str | None = None,
        replace_file: str | None = None,
        forced_lang: str = "ja",
        on_event: Callable[[dict], None] | None = None,
    ):
        if not HAVE_DML_DEPENDENCIES:
            raise RuntimeError(
                "WhisperDirectMLEngine requires transformers, optimum, and onnxruntime-directml. "
                "Please run `uv pip install onnxruntime-directml transformers optimum[onnxruntime]`"
            )

        self.model_dir = model_dir or DEFAULT_MODEL_ID
        self.threads = max(1, threads)
        self.forced_lang = forced_lang
        self.engine_name = "whisper_dml" 
        self.last_lang = forced_lang
        self.min_switch_s = 0.0
        self.dual_confirm = False
        self._punctuate = False
        self.lid_switch_confirm = 1
        self.on_event = on_event
        self._is_closed = False

        # 置換辞書 & ITN (RoutedASR 互換インターフェース)
        self._user_replacements: tuple[tuple[str, str], ...] = ()
        self._dynamic_comment_replacements: tuple[tuple[str, str], ...] = ()
        self._replacements: tuple[tuple[str, str], ...] = ()
        self._rule_hit_counts: dict[str, int] = {}
        self._rule_last_hit: dict[str, str] = {}
        self._stats_lock = threading.Lock()
        self._itn_overrides = itn_cjk.EMPTY_OVERRIDES if itn_cjk else None
        self.ko_spacer = None
        self.ko_spacer_ok = False

        if replace_file and os.path.exists(replace_file):
            self._load_replacements(replace_file)

        # DirectML デバイス解決: メイン GPU は保護し、内蔵 GPU のみを特定
        if device_id is None:
            self.device_id = resolve_igpu_directml_device_id()
        else:
            self.device_id = device_id

        # プロバイダとプロバイダオプションの設定
        available_providers = ort.get_available_providers()
        if self.device_id is not None and "DmlExecutionProvider" in available_providers:
            self.provider = "DmlExecutionProvider"
            self.provider_options = {"device_id": self.device_id}
            self.device_name = f"DirectML iGPU (Device ID: {self.device_id})"
        else:
            self.provider = "CPUExecutionProvider"
            self.provider_options = {}
            self.device_name = "CPU (Fallback)"

        logger.info(f"Initializing WhisperDirectMLEngine on {self.device_name} (threads: {self.threads})")

        # Processor & Model のロード
        t0 = time.perf_counter()
        self.processor = AutoProcessor.from_pretrained(self.model_dir)

        # GenerationConfig の調整
        try:
            self.gen_config = GenerationConfig.from_pretrained(self.model_dir)
            self.gen_config.forced_decoder_ids = None
            self.gen_config.language = "ja"
            self.gen_config.task = "transcribe"
        except Exception:
            self.gen_config = None

        load_kwargs = {
            "provider": self.provider,
            "use_merged": True,
            "subfolder": "onnx" if "kotoba" in str(self.model_dir) else "",
        }
        if self.provider_options:
            load_kwargs["provider_options"] = self.provider_options
        if self.gen_config is not None:
            load_kwargs["generation_config"] = self.gen_config

        # INT8 ONNX ファイルの自動選択
        try:
            self.model = ORTModelForSpeechSeq2Seq.from_pretrained(
                self.model_dir,
                encoder_file_name="encoder_model_int8.onnx",
                decoder_file_name="decoder_model_merged_int8.onnx",
                **load_kwargs,
            )
        except Exception as e:
            logger.info(f"INT8 ONNX モデルが見つからないため標準 ONNX をロードします: {e}")
            self.model = ORTModelForSpeechSeq2Seq.from_pretrained(
                self.model_dir,
                **load_kwargs,
            )

        # 日本語・文字起こし用デコーダプロンプトを事前バインド
        try:
            self.default_decoder_prompt_ids = self.processor.get_decoder_prompt_ids(
                language=self.forced_lang, task="transcribe"
            )
            self.model.generation_config.forced_decoder_ids = self.default_decoder_prompt_ids
        except Exception:
            self.default_decoder_prompt_ids = None

        self.initial_prompt = initial_prompt
        self.prompt_ids = None
        self._last_partial_time = 0.0
        self._last_partial_text = ""
        if self.initial_prompt:
            self.set_initial_prompt(self.initial_prompt)

        logger.info(f"WhisperDirectMLEngine loaded on {self.device_name} in {time.perf_counter() - t0:.2f}s")

    def set_initial_prompt(self, prompt: str | None) -> None:
        """デコーダ初期プロンプトを設定・更新する"""
        self.initial_prompt = prompt
        if not prompt or not self.processor:
            self.prompt_ids = None
            return

        try:
            self.prompt_ids = self.processor.get_prompt_ids(prompt, return_tensors="pt")
        except Exception as e:
            logger.warning(f"Initial prompt tokenization failed: {e}")
            self.prompt_ids = None

    def set_forced_lang(self, lang: str | None) -> None:
        """言語設定 (Whisper DirectML は日本語/他言語のプロンプト・タスク連動)"""
        self.forced_lang = lang or "ja"
        self.last_lang = self.forced_lang
        try:
            self.default_decoder_prompt_ids = self.processor.get_decoder_prompt_ids(
                language=self.forced_lang, task="transcribe"
            )
            if hasattr(self, "model") and self.model is not None:
                self.model.generation_config.forced_decoder_ids = self.default_decoder_prompt_ids
        except Exception:
            pass

    def set_dual_confirm(self, enabled: bool) -> None:
        """二重確認フラグ (互換性用)"""
        self.dual_confirm = bool(enabled)

    def set_punctuate(self, enabled: bool) -> None:
        """句読点付与設定 (互換性用: Whisperは内部で句読点を付与)"""
        self._punctuate = bool(enabled)

    def set_lid_switch_confirm(self, n: int) -> None:
        """言語判定スイッチ閾値 (互換性用)"""
        self.lid_switch_confirm = max(1, int(n))

    def set_min_switch_s(self, seconds: float) -> None:
        """最小スイッチ秒数 (互換性用)"""
        self.min_switch_s = max(0.0, float(seconds))

    def reset_session(self) -> None:
        """セッション状態リセット (互換性用)"""
        pass

    def identify(self, samples: np.ndarray, sample_rate: int) -> str:
        """言語同定 (互換性用)"""
        return self.forced_lang or "ja"

    def _identify_lang(self, samples: np.ndarray, sample_rate: int) -> str:
        return self.forced_lang or "ja"

    def set_replacements(self, mapping: dict[str, str]) -> None:
        """置換辞書を設定する (RoutedASR 互換: タプル構造に変換)"""
        user_pairs = []
        for k, v in (mapping or {}).items():
            if isinstance(k, str) and k.strip() != "":
                user_pairs.append((k, v if isinstance(v, str) else str(v)))
        self._user_replacements = tuple(user_pairs)
        self._rebuild_merged_replacements()

    def set_dynamic_comment_replacements(self, mapping: dict) -> None:
        """配信コメント由来の動的置換辞書を設定する"""
        dyn_pairs = []
        for k, v in (mapping or {}).items():
            if isinstance(k, str) and k.strip() != "":
                dyn_pairs.append((k, v if isinstance(v, str) else str(v)))
        self._dynamic_comment_replacements = tuple(dyn_pairs)
        self._rebuild_merged_replacements()

    def _rebuild_merged_replacements(self) -> None:
        """ユーザー置換辞書とコメント動的辞書をマージ"""
        user_tups = getattr(self, "_user_replacements", ()) or ()
        dyn_tups = getattr(self, "_dynamic_comment_replacements", ()) or ()
        user_keys = {k for k, _ in user_tups}
        combined = list(user_tups)
        for k, v in dyn_tups:
            if k not in user_keys:
                combined.append((k, v))
        self._replacements = tuple(combined)

    def set_itn_overrides(self, exclude: set | None = None, force: dict | None = None) -> None:
        """ITN (逆テキスト正規化) オーバーライド設定"""
        if itn_cjk is not None:
            self._itn_overrides = itn_cjk.ITNOverrides(
                exclude=frozenset(exclude or ()),
                force=dict(force or {}),
            )

    def rule_hit_stats(self) -> list[dict]:
        """置換辞書の適用回数統計を取得"""
        with self._stats_lock:
            return [
                {
                    "pattern": pattern,
                    "count": self._rule_hit_counts.get(pattern, 0),
                    "last_hit": self._rule_last_hit.get(pattern),
                }
                for pattern in set(self._rule_hit_counts) | set(self._rule_last_hit)
            ]

    def get_replacements(self) -> dict[str, str]:
        """現在の置換辞書を取得する"""
        return dict(self._replacements)

    def _load_replacements(self, path: str) -> None:
        """ファイルから置換辞書を読み込む"""
        pairs = []
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "\t" not in line:
                        continue
                    src, dst = line.split("\t", 1)
                    if src.strip():
                        pairs.append((src.strip(), dst.strip()))
            self._user_replacements = tuple(pairs)
            self._rebuild_merged_replacements()
        except Exception as e:
            logger.warning(f"Failed to load replacement file {path}: {e}")

    def _replace(self, text: str) -> str:
        """置換辞書および ITN をテキストに適用する"""
        if not text:
            return text

        # 1. ユーザー定義・動的置換辞書の適用
        for wrong, right in self._replacements:
            if not wrong or not isinstance(wrong, str):
                continue
            if wrong in text:
                text = text.replace(wrong, right if isinstance(right, str) else str(right))
                with self._stats_lock:
                    self._rule_hit_counts[wrong] = self._rule_hit_counts.get(wrong, 0) + 1
                    self._rule_last_hit[wrong] = datetime.datetime.now(datetime.timezone.utc).isoformat()

        # 2. CJK ITN の適用 (数字等の正規化)
        if itn_cjk is not None and self.forced_lang == "ja":
            try:
                text = itn_cjk.itn_ja(text, overrides=self._itn_overrides)
            except Exception as e:
                logger.debug(f"ITN ja failed: {e}")

        return text

    def partial(
        self,
        samples: np.ndarray,
        sample_rate: int = 16000,
        lang_hint: str | None = None,
    ) -> str:
        """
        発話途中の音声サンプルに対する高速な暫定文字起こし (Draft) を返却する。
        iGPU の負荷詰まりを防ぐためデバウンス (500ms) および最大トークン数 (16) を厳格制限。
        """
        if self._is_closed or len(samples) < 8000:  # 0.5秒未満はスキップ
            return ""

        now = time.perf_counter()
        if now - self._last_partial_time < 0.5:
            return self._last_partial_text

        self._last_partial_time = now

        try:
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

            # 末尾最大 4 秒分のみを暫定推論に渡す
            if len(samples) > 16000 * 4:
                samples = samples[-16000 * 4:]

            inputs = self.processor(
                samples,
                sampling_rate=16000,
                return_tensors="pt",
            )
            input_features = inputs.input_features
            attn_mask = torch.ones(input_features.shape[0], input_features.shape[-1], dtype=torch.int64)

            gen_kwargs = {
                "attention_mask": attn_mask,
                "max_new_tokens": 16,
                "num_beams": 1,
                "do_sample": False,
                "use_cache": True,
            }
            if self.default_decoder_prompt_ids is not None:
                gen_kwargs["forced_decoder_ids"] = self.default_decoder_prompt_ids
            if self.prompt_ids is not None:
                gen_kwargs["prompt_ids"] = self.prompt_ids

            predicted_ids = self.model.generate(input_features, **gen_kwargs)
            transcription = self.processor.batch_decode(
                predicted_ids, skip_special_tokens=True
            )[0]
            self._last_partial_text = self._replace(transcription.strip())
            return self._last_partial_text
        except Exception as e:
            logger.debug(f"Partial transcription error: {e}")
            return self._last_partial_text

    def transcribe(
        self,
        samples: np.ndarray,
        sample_rate: int = 16000,
        known_lang: str | None = None,
        speech_s: float | None = None,
        live: bool = True,
        second_opinion: bool | None = None,
    ) -> dict:
        """
        PCM 音声サンプルを文字起こしする。
        RoutedASR と同一フォーマットの辞書を返却します。
        """
        if self._is_closed:
            raise RuntimeError("WhisperDirectMLEngine is closed")

        if len(samples) == 0:
            return {
                "text": "",
                "lang": self.forced_lang,
                "tier": "dml_whisper",
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

        # リサンプリング (16000Hz 以外の場合)
        if sample_rate != 16000:
            target_len = int(len(samples) * 16000 / sample_rate)
            samples = np.interp(
                np.linspace(0, len(samples), target_len),
                np.arange(len(samples)),
                samples,
            ).astype(np.float32)

        # 極端に短い音声 (< 0.1s) は空文字を返却
        if len(samples) < 1600:
            return {
                "text": "",
                "lang": self.forced_lang,
                "tier": "dml_whisper",
                "lid_ms": 0.0,
                "decode_ms": 0.0,
                "probe_ms": 0.0,
            }

        # 特徴量抽出
        inputs = self.processor(
            samples,
            sampling_rate=16000,
            return_tensors="pt",
        )
        input_features = inputs.input_features
        attn_mask = torch.ones(input_features.shape[0], input_features.shape[-1], dtype=torch.int64)

        # 生成引数 (リアルタイム用に適正化)
        gen_kwargs = {
            "attention_mask": attn_mask,
            "max_new_tokens": 48,
            "num_beams": 1,
            "do_sample": False,
            "use_cache": True,
        }
        if self.default_decoder_prompt_ids is not None:
            gen_kwargs["forced_decoder_ids"] = self.default_decoder_prompt_ids
        if self.prompt_ids is not None:
            gen_kwargs["prompt_ids"] = self.prompt_ids

        # DirectML iGPU 推論
        predicted_ids = self.model.generate(input_features, **gen_kwargs)
        transcription = self.processor.batch_decode(
            predicted_ids, skip_special_tokens=True
        )[0]

        # 置換辞書適用
        text = self._replace(transcription.strip())
        decode_ms = (time.perf_counter() - t0) * 1000

        result = {
            "text": text,
            "lang": self.forced_lang,
            "tier": "dml_whisper",
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

        return result

    def close(self) -> None:
        """リソース解放"""
        self._is_closed = True
        self.model = None
        self.processor = None
