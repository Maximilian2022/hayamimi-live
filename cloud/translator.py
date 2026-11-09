"""
cloud/translator.py
Standalone 完全準拠ローカル CTranslate2 翻訳エンジン
- 英語 (ja->en): FuguMT (mojicast-fugumt-ja-en-ct2) int8 (高速・高品質)
- 多言語 (ja->zh/ko/es/...): M2M-100 418M (mojicast-m2m100-ct2) int8
Google 翻訳や外部 Web API を一切使わず、サーバーローカル推論で完結します。
"""

import logging
import os
import re
import sys
import threading
from typing import Dict, Optional

# scripts をインポートパスに追加
_SCRIPTS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from cloud.config import MODELS_DIR

logger = logging.getLogger("hayamimi.cloud.translator")

FUGUMT_DIR = os.path.join(MODELS_DIR, "mojicast-fugumt-ja-en-ct2")
M2M_DIR = os.path.join(MODELS_DIR, "mojicast-m2m100-ct2")

SUPPORTED_LANGUAGES: Dict[str, str] = {
    "en": "英語 (English - FuguMT/M2M-100)",
    "zh": "中国語 簡体字 (Chinese)",
    "zh-tw": "中国語 繁体字 (Traditional Chinese)",
    "ko": "韓国語 (Korean)",
    "es": "スペイン語 (Spanish)",
    "fr": "フランス語 (French)",
    "de": "ドイツ語 (German)",
    "id": "インドネシア語 (Indonesian)",
    "vi": "ベトナム語 (Vietnamese)",
    "pt": "ポルトガル語 (Portuguese)",
}


def digits_consistent(src: str, out: str) -> bool:
    """元テキストに含まれる数字が翻訳後にも欠落せず残っているか検証"""
    src_runs = re.findall(r"\d+", src)
    if not src_runs:
        return True
    out_runs = set(re.findall(r"\d+", out))
    return all(run in out_runs for run in src_runs)


def safe_translate(translator, text: str) -> str:
    """1 行翻訳。数字が壊れた場合は原文にフォールバック"""
    try:
        out = translator.translate(text)
        if out != text and not digits_consistent(text, out):
            logger.warning("Translation mangled digits: '%s' -> '%s', falling back to source", text, out)
            return text
        return out
    except Exception as e:
        logger.warning("Translation error for '%s': %s", text, e)
        return text


def translate_by_sentence(translator, text: str) -> str:
    """句読点で文ごとに分割してローカル翻訳し、結合する"""
    sentences = [s for s in re.split(r"(?<=[。！？!?])\s*", text) if s.strip()]
    if not sentences:
        return text
    out = []
    for s in sentences:
        res = safe_translate(translator, s)
        out.append(res)
    return " ".join(out)


class LocalTranslatorPool:
    """共有ローカル翻訳モデルプール (スレッドセーフ・遅延共有ロード)
    全ユーザーで 1 つの FuguMT / M2M-100 インスタンスを共有し、メモリ消費を最適化します。
    """

    def __init__(self, models_dir: str = MODELS_DIR):
        self.models_dir = models_dir
        self._translators: Dict[str, object] = {}
        self._lock = threading.Lock()

    def get_translator(self, target_lang: str):
        lang = target_lang.lower().strip()
        # 言語コード正規化 (zh-cn -> zh)
        if lang in ("zh-cn", "cn"):
            lang = "zh"
        elif lang in ("zh-tw", "tw", "zh-hk", "hk"):
            lang = "zh-tw"

        if lang in self._translators:
            return self._translators[lang]

        with self._lock:
            if lang in self._translators:
                return self._translators[lang]

            # 1. 英語専用 FuguMT (最優先)
            if lang == "en":
                if os.path.isdir(FUGUMT_DIR):
                    try:
                        logger.info("Loading local FuguMT ja->en translator from %s...", FUGUMT_DIR)
                        from translate_ja_en import TranslatorJaEn
                        tr = TranslatorJaEn(model_dir=FUGUMT_DIR, device="cpu", compute_type="int8")
                        self._translators[lang] = tr
                        logger.info("✔ Local FuguMT ja->en loaded successfully.")
                        return tr
                    except Exception as e:
                        logger.error("Failed to load FuguMT: %s", e)
                else:
                    logger.warning("FuguMT model directory not found: %s", FUGUMT_DIR)

            # 2. 多言語 M2M-100
            if os.path.isdir(M2M_DIR):
                try:
                    logger.info("Loading local M2M-100 ja->%s translator from %s...", lang, M2M_DIR)
                    from translate_m2m import TranslatorM2M
                    tr = TranslatorM2M(target_lang=lang, model_dir=M2M_DIR, device="cpu", compute_type="int8")
                    self._translators[lang] = tr
                    logger.info("✔ Local M2M-100 ja->%s loaded successfully.", lang)
                    return tr
                except Exception as e:
                    logger.error("Failed to load M2M-100 for %s: %s", lang, e)
            else:
                logger.warning("M2M-100 model directory not found: %s", M2M_DIR)

        return None

    def translate(self, text: str, target_lang: str) -> str:
        if not text or not text.strip():
            return ""
        tr = self.get_translator(target_lang)
        if tr is None:
            return text
        return translate_by_sentence(tr, text)


# グローバル共有翻訳プール
local_translator_pool = LocalTranslatorPool()


def translate_text(text: str, target_lang: str, api_key: str = "") -> str:
    """翻訳のエントリポイント (完全ローカル推論)"""
    return local_translator_pool.translate(text, target_lang)
