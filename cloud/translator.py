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
from typing import Dict, List, Optional

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


def detect_language(text: str) -> str:
    """外部ライブラリ不要の高速・軽量言語自動判定 (Unicode ブロック + ヒューリスティック)"""
    if not text or not text.strip():
        return "en"

    # 1. 非ラテン文字言語 (Unicode ブロックで 100% 判定可能)
    # 日本語: ひらがな・カタカナ (半角カタカナ含む)
    if re.search(r"[\u3040-\u309F\u30A0-\u30FF\uFF65-\uFF9F]", text):
        return "ja"
    # 韓国語: ハングル
    if re.search(r"[\uAC00-\uD7AF\u1100-\u11FF\u3130-\u318F]", text):
        return "ko"
    # 中国語: 漢字 (ひらがな・カタカナが無い状態)
    if re.search(r"[\u4E00-\u9FFF]", text):
        return "zh"
    # ロシア語など: キリル文字
    if re.search(r"[\u0400-\u04FF]", text):
        return "ru"
    # タイ語
    if re.search(r"[\u0E00-\u0E7F]", text):
        return "th"
    # アラビア語
    if re.search(r"[\u0600-\u06FF]", text):
        return "ar"

    # 2. ラテン文字言語の特徴判定
    lower = text.lower()
    # ベトナム語特有の声調記号
    if re.search(r"[ăđơưĂĐƠƯ]|ệ|ế|ề|ể|ễ|ồ|ổ|ỗ|ố|ộ|ờ|ở|ỡ|ớ|ợ|ừ|ử|ữ|ứ|ự", lower):
        return "vi"
    # スペイン語特有記号・高頻度語
    if re.search(r"[ñ¿¡]", lower) or re.search(r"\b(hola|gracias|por favor|buenos días|buenas noches|cómo estás)\b", lower):
        return "es"
    # ポルトガル語特有文字・高頻度語
    if re.search(r"[ãõ]", lower) or re.search(r"\b(olá|obrigado|obrigada|você|não|tudo bem|bom dia)\b", lower):
        return "pt"
    # ドイツ語特有文字・高頻度語
    if re.search(r"[ßäöü]", lower) or re.search(r"\b(ich|und|nicht|danke|hallo|guten tag|wie geht)\b", lower):
        return "de"
    # フランス語特有文字・高頻度語
    if re.search(r"[œæç]", lower) or re.search(r"\b(bonjour|merci|je suis|vous|c'est|très|comment)\b", lower):
        return "fr"
    # インドネシア語高頻度語
    if re.search(r"\b(terima kasih|selamat|apa kabar|saya|tidak|bagaimana)\b", lower):
        return "id"

    # デフォルトは英語
    return "en"


def digits_consistent(src: str, out: str) -> bool:
    """元テキストに含まれる数字が翻訳後にも欠落せず残っているか検証"""
    src_runs = re.findall(r"\d+", src)
    if not src_runs:
        return True
    out_runs = set(re.findall(r"\d+", out))
    return all(run in out_runs for run in src_runs)


def safe_translate(translator, text: str, source_lang: Optional[str] = None) -> str:
    """1 行翻訳。数字が壊れた場合は原文にフォールバック"""
    try:
        if source_lang and hasattr(translator, "translate"):
            # TranslatorM2M は source_lang 引数を受け取る
            try:
                out = translator.translate(text, source_lang=source_lang)
            except TypeError:
                out = translator.translate(text)
        else:
            out = translator.translate(text)

        if out != text and not digits_consistent(text, out):
            logger.warning("Translation mangled digits: '%s' -> '%s', falling back to source", text, out)
            return text
        return out
    except Exception as e:
        logger.warning("Translation error for '%s': %s", text, e)
        return text


def translate_by_sentence(translator, text: str, source_lang: Optional[str] = None) -> str:
    """句読点で文ごとに分割してローカル翻訳し、結合する"""
    sentences = [s for s in re.split(r"(?<=[。！？!?]|(?<!\d)\.(?!\d))\s*", text) if s.strip()]
    if not sentences:
        return text
    out = []
    for s in sentences:
        res = safe_translate(translator, s, source_lang=source_lang)
        out.append(res)
    return " ".join(out)


class LocalTranslatorPool:
    """共有ローカル翻訳モデルプール (スレッドセーフ・遅延共有ロード)
    全ユーザーで 1 つの FuguMT / M2M-100 インスタンスを共有し、メモリ消費を最適化します。
    """

    def __init__(self, models_dir: str = MODELS_DIR):
        self.models_dir = models_dir
        self._translators: Dict[str, object] = {}
        self._m2m_translators: Dict[str, object] = {}
        self._lock = threading.Lock()

    def _normalize_lang(self, target_lang: str) -> str:
        lang = (target_lang or "en").lower().strip()
        if lang in ("zh-cn", "cn"):
            return "zh"
        elif lang in ("zh-tw", "tw", "zh-hk", "hk"):
            return "zh-tw"
        return lang

    def get_m2m_translator(self, target_lang: str):
        """任意の target_lang 向けの M2M-100 インスタンスを取得 (双方向翻訳用)"""
        lang = self._normalize_lang(target_lang)
        if lang in self._m2m_translators:
            return self._m2m_translators[lang]

        with self._lock:
            if lang in self._m2m_translators:
                return self._m2m_translators[lang]

            if os.path.isdir(M2M_DIR):
                try:
                    logger.info("Loading local M2M-100 ->%s translator from %s...", lang, M2M_DIR)
                    from translate_m2m import TranslatorM2M
                    tr = TranslatorM2M(target_lang=lang, model_dir=M2M_DIR, device="cpu", compute_type="int8")
                    self._m2m_translators[lang] = tr
                    logger.info("✔ Local M2M-100 ->%s loaded successfully.", lang)
                    return tr
                except Exception as e:
                    logger.error("Failed to load M2M-100 for %s: %s", lang, e)
            else:
                logger.warning("M2M-100 model directory not found: %s", M2M_DIR)
        return None

    def get_translator(self, target_lang: str):
        """日本語からの翻訳用トランスレータ (英語は FuguMT 最優先、他言語は M2M-100)"""
        lang = self._normalize_lang(target_lang)
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
        tr_m2m = self.get_m2m_translator(lang)
        if tr_m2m is not None:
            self._translators[lang] = tr_m2m
        return tr_m2m

    def translate(self, text: str, target_lang: str, source_lang: str = "ja") -> str:
        if not text or not text.strip():
            return ""
        src = self._normalize_lang(source_lang)
        tgt = self._normalize_lang(target_lang)
        if src == tgt:
            return text

        if src == "ja":
            tr = self.get_translator(tgt)
            if tr is None:
                return text
            return translate_by_sentence(tr, text, source_lang="ja")
        else:
            # 他言語からの翻訳は常に M2M-100 を使用
            tr = self.get_m2m_translator(tgt)
            if tr is None:
                return text
            return translate_by_sentence(tr, text, source_lang=src)


# グローバル共有翻訳プール
local_translator_pool = LocalTranslatorPool()


def translate_text(text: str, target_lang: str, api_key: str = "") -> str:
    """翻訳のエントリポイント (完全ローカル推論)"""
    return local_translator_pool.translate(text, target_lang, source_lang="ja")


def translate_bidirectional(
    text: str,
    target_langs: Optional[List[str]] = None,
    source_lang: Optional[str] = None,
) -> Dict[str, object]:
    """
    双方向テキスト翻訳エントリポイント:
    - 日本語の場合: target_langs (複数可、未指定時は ['en']) へ翻訳して返す
    - 他言語の場合: 自動で日本語 ('ja') へ翻訳して返す
    """
    stripped = (text or "").strip()
    if not stripped:
        return {
            "ok": True,
            "source_lang": "unknown",
            "original": text,
            "translations": {},
        }

    src = (source_lang or "").lower().strip() or detect_language(stripped)
    translations: Dict[str, str] = {}

    if src == "ja":
        raw_targets = target_langs if target_langs else ["en"]
        # 空文字除去・重複排除 (順序保持)
        targets = list(dict.fromkeys([t.strip().lower() for t in raw_targets if t and t.strip()]))
        if not targets:
            targets = ["en"]
        for lang in targets:
            translations[lang] = local_translator_pool.translate(stripped, target_lang=lang, source_lang="ja")
    else:
        translations["ja"] = local_translator_pool.translate(stripped, target_lang="ja", source_lang=src)

    return {
        "ok": True,
        "source_lang": src,
        "original": text,
        "translations": translations,
    }
