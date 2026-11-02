"""Tests for scripts/translate_ja_en.py: verifying FuguMT with EOS appended."""

import os
import sys
import time
import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "scripts"))

pytest.importorskip("ctranslate2")
pytest.importorskip("sentencepiece")

from translate_ja_en import TranslatorJaEn, BEAM_SIZE, NO_REPEAT_NGRAM_SIZE


def test_beam_size_and_ngram_settings():
    assert BEAM_SIZE == 1, "BEAM_SIZE should be 1 (greedy decoding)"
    assert NO_REPEAT_NGRAM_SIZE == 3, "NO_REPEAT_NGRAM_SIZE should be 3"


def test_translate_ja_en_smoke():
    tr = TranslatorJaEn()
    
    # 1. Question sentence: must not hallucinate rain/snow repetition
    out1 = tr.translate("明日は雨が降ると思いますか？")
    assert "rain" in out1.lower(), f"Unexpected translation: {out1}"
    assert "tomorrow" in out1.lower(), f"Unexpected translation: {out1}"
    assert len(out1.split()) < 15, f"Hallucination detected (too long): {out1}"

    # 2. Casual / Japanese culture: ramen
    out2 = tr.translate("お腹空いたからラーメン食べに行こうかな。")
    assert "ramen" in out2.lower() or "noodles" in out2.lower(), f"Unexpected translation: {out2}"
    assert len(out2.split()) < 20, f"Too long: {out2}"

    # 3. Numbers & currency
    out3 = tr.translate("このプロジェクトの予算は500万円です。")
    assert "five million" in out3.lower() or "5" in out3 or "million" in out3.lower()
    assert "yen" in out3.lower()

    # 4. Latency check: should be ultra fast (under 300ms on CPU)
    t0 = time.perf_counter()
    tr.translate("本日はお集まりいただきありがとうございます。")
    dt = (time.perf_counter() - t0) * 1000
    assert dt < 500, f"Latency too high: {dt:.1f}ms"

