"""
scripts/comment_matcher.py
コメント連動の補正: 配信者がコメントの語を話したときだけ、字幕のその部分を直す。

認識結果を形態素に分けて読みを取り、コメントの語の読みと音が近い部分を置き換える。
意味は判定しないので、普通の言葉を壊さないよう置き換える条件を厳しくしている。
"""

from __future__ import annotations

import re
import sys
import threading
from dataclasses import dataclass
from typing import Iterable, Sequence

MIN_MORAS = 3
# 音の差の許容量。1 音違いは 1.0、濁点の有無や長音の有無は 0.4
MAX_DISTANCE_SHORT = 0.4
MAX_DISTANCE_LONG = 1.0
LONG_TERM_MORAS = 5
MAX_SPAN_MORPHEMES = 3
MAX_TERMS = 300
_SMALL_KANA = set("ァィゥェォャュョヮ")
_LIGHT_EDIT_COST = 0.4
_KANJI = re.compile(r"[一-龥々]")
_ASCII_WORD = re.compile(r"^[A-Za-z0-9]+$")
_ALLOWED_POS = {"名詞"}

_VOICING_GROUPS = (
    "カガ", "キギ", "クグ", "ケゲ", "コゴ", "サザ", "シジ", "スズ", "セゼ", "ソゾ", "タダ", "チヂ", "ツヅ",
    "テデ", "トド", "ハバパ", "ヒビピ", "フブプ", "ヘベペ", "ホボポ",
)
_VOICING = {c: i for i, group in enumerate(_VOICING_GROUPS) for c in group}


@dataclass(frozen=True)
class CommentTerm:
    surface: str
    reading: str


def to_katakana(text: str) -> str:
    return "".join(chr(ord(c) + 0x60) if "ぁ" <= c <= "ゖ" else c for c in text)


def to_moras(reading: str) -> list[str]:
    moras: list[str] = []
    for ch in to_katakana(reading):
        if ch in _SMALL_KANA and moras:
            moras[-1] += ch
        else:
            moras.append(ch)
    return moras


def _substitution_cost(a: str, b: str) -> float:
    if a == b:
        return 0.0
    if a[1:] == b[1:] and _VOICING.get(a[0], -1) == _VOICING.get(b[0], -2):
        return _LIGHT_EDIT_COST
    return 1.0


def _gap_cost(mora: str) -> float:
    return _LIGHT_EDIT_COST if mora in ("ー", "ッ") else 1.0


def phonetic_distance(a: Sequence[str], b: Sequence[str]) -> float:
    previous = [0.0]
    for mora in b:
        previous.append(previous[-1] + _gap_cost(mora))
    for x in a:
        current = [previous[0] + _gap_cost(x)]
        for j, y in enumerate(b, 1):
            current.append(min(
                previous[j] + _gap_cost(x),
                current[j - 1] + _gap_cost(y),
                previous[j - 1] + _substitution_cost(x, y),
            ))
        previous = current
    return previous[-1]


def allowed_distance(moras: int) -> float:
    if moras < MIN_MORAS:
        return -1.0
    return MAX_DISTANCE_LONG if moras >= LONG_TERM_MORAS else MAX_DISTANCE_SHORT


_tokenizer = None
_mode = None
_tokenizer_lock = threading.Lock()


def _get_tokenizer():
    global _tokenizer, _mode
    with _tokenizer_lock:
        if _tokenizer is None:
            from sudachipy import dictionary, tokenizer

            _tokenizer = dictionary.Dictionary().create()
            _mode = tokenizer.Tokenizer.SplitMode.C
    return _tokenizer, _mode


def tokenize(text: str):
    tok, mode = _get_tokenizer()
    with _tokenizer_lock:
        return list(tok.tokenize(text, mode))


def reading_of(text: str) -> str:
    return "".join(m.reading_form() or "" for m in tokenize(text))


def _can_be_replaced(morpheme) -> bool:
    """Common words must survive, so only nouns the dictionary does not know as written Japanese qualify."""
    pos = morpheme.part_of_speech()
    if pos[0] not in _ALLOWED_POS:
        return False
    return not _KANJI.search(morpheme.surface()) or pos[1] == "固有名詞" or morpheme.is_oov()


class CommentCorrector:
    """Holds the words from recent comments and applies them to recognized text."""

    def __init__(self):
        self._terms: tuple[CommentTerm, ...] = ()
        self._by_ascii: dict[str, str] = {}
        self._reported: set[tuple[str, str]] = set()

    @property
    def active(self) -> bool:
        return bool(self._terms)

    def set_terms(self, terms: Iterable[CommentTerm]) -> None:
        kept = tuple(list(dict.fromkeys(terms))[:MAX_TERMS])
        self._by_ascii = {t.surface.lower(): t.surface for t in kept if _ASCII_WORD.match(t.surface)}
        self._terms = kept
        if len(self._reported) > 500:
            self._reported = set()

    def _report(self, before: str, after: str) -> None:
        # The same word repeats in partial results, so each correction prints once.
        if (before, after) not in self._reported:
            self._reported.add((before, after))
            print(f"[comment-link] {before} -> {after}", file=sys.stderr)

    def _best_term(self, surface: str, reading: str) -> str | None:
        moras = to_moras(reading)
        best, best_distance = None, float("inf")
        for term in self._terms:
            if not term.reading or term.surface == surface:
                continue
            target = to_moras(term.reading)
            limit = allowed_distance(min(len(moras), len(target)))
            if limit < 0 or abs(len(moras) - len(target)) > 1:
                continue
            distance = phonetic_distance(moras, target)
            if distance <= limit and distance < best_distance:
                best, best_distance = term.surface, distance
        return best

    def correct(self, text: str) -> str:
        """Return text with spoken comment words corrected. Other words are left unchanged."""
        if not text or not self._terms:
            return text
        try:
            morphemes = tokenize(text)
        except Exception:
            return text
        surfaces = {t.surface for t in self._terms}
        out: list[str] = []
        i = 0
        while i < len(morphemes):
            replaced = False
            first = morphemes[i].surface()
            ascii_hit = self._by_ascii.get(first.lower())
            if ascii_hit and ascii_hit != first:
                self._report(first, ascii_hit)
                out.append(ascii_hit)
                i += 1
                continue
            for length in range(MAX_SPAN_MORPHEMES, 0, -1):
                span = morphemes[i : i + length]
                if len(span) < length or not all(_can_be_replaced(m) for m in span):
                    continue
                surface = "".join(m.surface() for m in span)
                if surface in surfaces:
                    out.append(surface)
                    i += length
                    replaced = True
                    break
                target = self._best_term(surface, "".join(m.reading_form() or "" for m in span))
                if target:
                    self._report(surface, target)
                    out.append(target)
                    i += length
                    replaced = True
                    break
            if not replaced:
                out.append(first)
                i += 1
        return "".join(out)
