"""
cloud/translator.py
クラウド向けリアルタイム翻訳モジュール
- Gemini API (ユーザー登録キー): 最高品質・文脈認識・固有名詞対応
- Google Translate (Web API): キー未登録でも即時・無料で動作するフォールバック
"""

import json
import logging
import re
import urllib.parse
import urllib.request
from typing import Dict, List, Optional

logger = logging.getLogger("hayamimi.cloud.translator")

SUPPORTED_LANGUAGES: Dict[str, str] = {
    "en": "英語 (English)",
    "zh-CN": "中国語 簡体字 (Simplified Chinese)",
    "zh-TW": "中国語 繁体字 (Traditional Chinese)",
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


def translate_with_gemini(text: str, target_lang: str, api_key: str) -> str:
    """Gemini API (gemini-2.5-flash) を使った高精度コンテキスト翻訳"""
    if not text or not api_key:
        return text
    try:
        from google import genai
        client = genai.Client(api_key=api_key)
        lang_name = SUPPORTED_LANGUAGES.get(target_lang, target_lang)
        prompt = (
            f"You are a professional real-time subtitle translator. "
            f"Translate the following Japanese speech transcript into {lang_name} naturally and concisely. "
            f"Output ONLY the translated line, with NO quotes, explanations, or commentary.\n\n"
            f"Japanese: {text}\n"
            f"Translation:"
        )
        resp = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
        )
        res_text = resp.text.strip() if resp.text else text
        # 余計な改行やプレフィックスがあれば除去
        res_text = re.sub(r"^(Translation:|訳:)\s*", "", res_text, flags=re.IGNORECASE).strip()
        if not digits_consistent(text, res_text):
            logger.warning("Gemini translation mangled digits: '%s' -> '%s'", text, res_text)
        return res_text
    except Exception as e:
        logger.warning("Gemini translation error for %s (%s), falling back to Google: %s", target_lang, text, e)
        return translate_with_google(text, target_lang)


def translate_with_google(text: str, target_lang: str) -> str:
    """Google 翻訳 (Web API) による無料フォールバック翻訳"""
    if not text:
        return text
    try:
        g_lang = target_lang
        if target_lang == "zh-CN":
            g_lang = "zh-CN"
        elif target_lang == "zh-TW":
            g_lang = "zh-TW"

        encoded = urllib.parse.quote(text)
        url = f"https://translate.googleapis.com/translate_a/single?client=gtx&sl=ja&tl={g_lang}&dt=t&q={encoded}"
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        )
        with urllib.request.urlopen(req, timeout=3.5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if data and data[0]:
                out = "".join(item[0] for item in data[0] if item and item[0])
                return out.strip()
    except Exception as e:
        logger.warning("Google translation failed for '%s' (%s): %s", text, target_lang, e)
    return text


def translate_text(text: str, target_lang: str, api_key: str = "") -> str:
    """翻訳のエントリポイント"""
    if not text or not text.strip():
        return ""
    if api_key:
        return translate_with_gemini(text, target_lang, api_key)
    return translate_with_google(text, target_lang)
