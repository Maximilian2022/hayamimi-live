# -*- coding: utf-8 -*-
"""OneComme (わんコメ) & Local Comment File Synchronizer for Hayamimi Live.

Fetches live viewer comments from:
1. Primary: OneComme Local HTTP API (http://127.0.0.1:11180/api/comments)
2. Secondary: Local text file polling (e.g. comment.txt)

Extracts proper nouns and trending stream terms via SudachiPy (Mode C),
generates phonetic/hiragana/katakana fallback replacement pairs, and
atomically merges them into the active RoutedASR replacement dictionary
with a sliding-window TTL (default 5 minutes).
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import threading
import time
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

# Guarantee canonical module aliasing between 'comment_sync' and 'scripts.comment_sync'
if __name__ == "scripts.comment_sync":
    sys.modules["comment_sync"] = sys.modules[__name__]
elif __name__ == "comment_sync":
    sys.modules["scripts.comment_sync"] = sys.modules[__name__]

logger = logging.getLogger("hayamimi.comment_sync")

# Common stream greetings and noise words to ignore
_STOPWORDS: Set[str] = {
    "こんにちは", "こんばんは", "おはよう", "おつかれ", "お疲れ様", "おつ", "わこつ",
    "初見", "初見です", "よろしく", "ありがとう", "ナイス", "ないす", "草", "笑",
    "すごい", "やばい", "かわいい", "かっこいい", "それな", "たしかに", "ほんと",
    "今日", "明日", "昨日", "今回", "配信", "放送", "コメント", "リスナー", "ちゃん",
    "さん", "くん", "様", "自分", "俺", "私", "僕", "みんな", "ここ", "そこ", "あれ",
    "これ", "それ", "どこ", "なに", "なん", "ため", "こと", "もの", "とき", "よう",
}


def _katakana_to_hiragana(text: str) -> str:
    """Convert Katakana characters in text to Hiragana."""
    res = []
    for ch in text:
        code = ord(ch)
        if 0x30A1 <= code <= 0x30F6:
            res.append(chr(code - 0x60))
        else:
            res.append(ch)
    return "".join(res)


class CommentHotwordExtractor:
    """Extracts nouns/proper nouns and generates phonetic replacement pairs using SudachiPy."""

    def __init__(self):
        self._tokenizer = None
        self._mode = None
        self._init_sudachi()

    def _init_sudachi(self) -> None:
        try:
            try:
                from scripts.asr_engine import get_sudachi_tokenizer
            except ImportError:
                from asr_engine import get_sudachi_tokenizer
            tok, mode, lock = get_sudachi_tokenizer()
            if tok is not None:
                self._tokenizer = tok
                self._mode = mode
                self._lock = lock
                return
        except Exception:
            pass

        try:
            from sudachipy import dictionary, tokenizer
            dic = dictionary.Dictionary()
            if hasattr(dic, "tokenizer"):
                self._tokenizer = dic.tokenizer()
            else:
                self._tokenizer = dic.create()
            self._mode = tokenizer.Tokenizer.SplitMode.C
            self._lock = threading.Lock()
        except Exception as e:
            logger.debug("SudachiPy not available in CommentHotwordExtractor: %s", e)
            self._lock = threading.Lock()

    def extract_terms_from_text(self, text: str) -> List[Tuple[str, str]]:
        """Extract (surface_word, katakana_reading) pairs from a comment string."""
        if not text or not isinstance(text, str):
            return []

        # Strip URLs, HTML tags, emojis, and repeated w/888
        clean = re.sub(r"https?://[^\s　]+", " ", text)
        clean = re.sub(r"<[^>]+>", " ", clean)
        clean = re.sub(r"[wWｗＷ8８]{2,}", " ", clean)
        clean = clean.strip()
        if not clean:
            return []

        results: List[Tuple[str, str]] = []
        seen: Set[str] = set()

        if self._tokenizer is not None:
            try:
                with getattr(self, '_lock', threading.Lock()):
                    morphemes = self._tokenizer.tokenize(clean, self._mode)
                for m in morphemes:
                    surf = m.surface().strip()
                    if len(surf) < 2 or len(surf) > 12:
                        continue
                    if surf.lower() in _STOPWORDS or surf in seen:
                        continue
                    # Ignore pure numbers or punctuation
                    if re.fullmatch(r"[0-9０-９!！?？_.\-、。・\s]+", surf):
                        continue

                    pos = m.part_of_speech()
                    if not pos or pos[0] != "名詞":
                        continue
                    # SAFETY GUARD (H-1): ONLY accept Proper Nouns (固有名詞) or OOV (新語・未知語)
                    # Exclude general common nouns (普通名詞) e.g. "死体", "時間", "機械", "視聴" to prevent transcript poisoning!
                    is_proper = len(pos) > 1 and pos[1] == "固有名詞"
                    is_oov = hasattr(m, "is_oov") and m.is_oov()
                    if not (is_proper or is_oov):
                        continue

                    reading = m.reading_form() or ""
                    seen.add(surf)
                    results.append((surf, reading))
            except Exception as e:
                logger.debug("Sudachi tokenize error in comment: %s", e)

        # Fallback / supplement: Katakana words >= 3 chars (excluding common words)
        for km in re.findall(r"[ァ-ヶー]{3,12}", clean):
            if km not in seen and km not in _STOPWORDS:
                seen.add(km)
                results.append((km, km))

        return results

    def is_common_japanese_reading(self, reading_hira: str) -> bool:
        """Check if a reading form conflicts with common everyday Japanese words/grammar."""
        if not reading_hira or len(reading_hira) < 3:
            return True
        if self._tokenizer is not None:
            try:
                morphemes = self._tokenizer.tokenize(reading_hira, self._mode)
                for m in morphemes:
                    pos = m.part_of_speech()
                    # If reading splits into common verbs, auxiliaries, particles, adjectives, or common nouns
                    if pos[0] in ("動詞", "助動詞", "助詞", "形容詞", "副詞", "接続詞"):
                        return True
                    if pos[0] == "名詞" and len(pos) > 1 and pos[1] in ("普通名詞", "代名詞", "非自立可能", "数詞"):
                        return True
            except Exception:
                pass
        return False

    def build_replacement_candidates(self, surface: str, reading_katakana: str) -> Dict[str, str]:
        """Generate safe ASR misrecognition -> correct surface mappings from a term and its reading."""
        candidates: Dict[str, str] = {}
        surf = surface.strip()
        if len(surf) < 2 or len(surf) > 12:
            return candidates

        # 1. If surface contains Kanji/Alphabet and reading is >= 3 chars, map Hiragana & Katakana readings -> surface
        # BUT ONLY IF the reading is not a common Japanese word!
        has_kanji_or_alpha = bool(re.search(r"[一-龥A-Za-z0-9]", surf))
        if reading_katakana and len(reading_katakana) >= 3:
            hira = _katakana_to_hiragana(reading_katakana)
            if has_kanji_or_alpha:
                if hira != surf and len(hira) >= 3 and not self.is_common_japanese_reading(hira):
                    candidates[hira] = surf
                if reading_katakana != surf and len(reading_katakana) >= 3 and not self.is_common_japanese_reading(hira):
                    candidates[reading_katakana] = surf

        # 2. If surface is Katakana with long vowel 'ー' (>= 4 chars), map vowel-shortened variants -> surface
        # e.g. "クレーバー" -> "クレバー", "クレーバ", "クレバ", "くれーばー" -> "クレーバー"
        if "ー" in surf and len(surf) >= 4:
            shortened_all = surf.replace("ー", "")
            if len(shortened_all) >= 3 and shortened_all != surf:
                candidates[shortened_all] = surf
            for i, ch in enumerate(surf):
                if ch == "ー":
                    variant = surf[:i] + surf[i + 1 :]
                    if len(variant) >= 3 and variant != surf:
                        candidates[variant] = surf
            hira_full = _katakana_to_hiragana(surf)
            if len(hira_full) >= 3 and hira_full != surf:
                candidates[hira_full] = surf

        return candidates


class CommentSynchronizer:
    """Background daemon that polls OneComme HTTP API and/or a local comment text file."""

    def __init__(
        self,
        enabled: bool = False,
        onecomme_url: str = "http://127.0.0.1:11180/api/comments",
        comment_file_path: str = "",
        poll_interval_sec: float = 3.0,
        word_ttl_sec: float = 300.0,
        on_dictionary_updated: Optional[Callable[[Dict[str, str]], None]] = None,
    ):
        self._lock = threading.Lock()
        self.enabled = bool(enabled)
        self.onecomme_url = onecomme_url.strip() or "http://127.0.0.1:11180/api/comments"
        self.comment_file_path = comment_file_path.strip()
        self.poll_interval_sec = max(0.5, float(poll_interval_sec))
        self.word_ttl_sec = max(0.1, float(word_ttl_sec))
        self.on_dictionary_updated = on_dictionary_updated

        self._extractor = CommentHotwordExtractor()
        # surface -> { "reading": str, "expires_at": float, "replacements": Dict[str, str] }
        self._active_terms: Dict[str, Dict[str, Any]] = {}
        self._seen_comment_ids: Set[str] = set()
        self._last_file_mtime: float = 0.0
        self._last_file_size: int = 0

        self._connected_source: str = "disconnected"
        self._last_error: Optional[str] = None
        self._total_comments_processed: int = 0

        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> "CommentSynchronizer":
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return self
            self._stop_event.clear()
            self._thread = threading.Thread(target=self._run_loop, daemon=True)
            self._thread.start()
        return self

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=2.0)

    def get_status(self) -> Dict[str, Any]:
        """Return current status and active hotwords."""
        self._prune_expired()
        with self._lock:
            active_words = list(self._active_terms.keys())
            dyn_replacements = self._build_merged_replacements_unlocked()
            return {
                "enabled": self.enabled,
                "onecomme_url": self.onecomme_url,
                "comment_file_path": self.comment_file_path,
                "word_ttl_sec": self.word_ttl_sec,
                "connected_source": self._connected_source if self.enabled else "disabled",
                "active_words": active_words,
                "active_words_count": len(active_words),
                "dynamic_rules_count": len(dyn_replacements),
                "total_comments_processed": self._total_comments_processed,
                "last_error": self._last_error,
            }

    def set_config(self, cfg: Dict[str, Any]) -> Dict[str, Any]:
        """Update configuration at runtime."""
        if not isinstance(cfg, dict):
            return self.get_status()
        notify_needed = False
        with self._lock:
            if "enabled" in cfg:
                new_en = bool(cfg["enabled"])
                if new_en != self.enabled:
                    self.enabled = new_en
                    notify_needed = True
            if "onecomme_url" in cfg and isinstance(cfg["onecomme_url"], str):
                u = cfg["onecomme_url"].strip()
                if u:
                    self.onecomme_url = u
            if "comment_file_path" in cfg and isinstance(cfg["comment_file_path"], str):
                self.comment_file_path = cfg["comment_file_path"].strip()
            if "word_ttl_sec" in cfg:
                try:
                    self.word_ttl_sec = max(30.0, float(cfg["word_ttl_sec"]))
                except Exception:
                    pass
            if not self.enabled:
                self._connected_source = "disabled"

        if notify_needed and self.on_dictionary_updated:
            try:
                self.on_dictionary_updated(self.get_dynamic_replacements())
            except Exception as e:
                logger.warning("Failed on_dictionary_updated callback: %s", e)

        return self.get_status()

    def ingest_comments(self, texts: List[str], source_label: str = "manual") -> int:
        """Process a list of raw comment strings, extract terms, and update sliding window."""
        if not texts:
            return 0
        now = time.time()
        added_or_refreshed = 0

        with self._lock:
            self._connected_source = source_label
            self._last_error = None
            for txt in texts:
                if not txt or not isinstance(txt, str):
                    continue
                self._total_comments_processed += 1
                pairs = self._extractor.extract_terms_from_text(txt)
                for surf, reading in pairs:
                    repls = self._extractor.build_replacement_candidates(surf, reading)
                    self._active_terms[surf] = {
                        "reading": reading,
                        "expires_at": now + self.word_ttl_sec,
                        "replacements": repls,
                    }
                    added_or_refreshed += 1

        self._prune_expired()
        if added_or_refreshed > 0 and self.on_dictionary_updated:
            try:
                self.on_dictionary_updated(self.get_dynamic_replacements())
            except Exception as e:
                logger.warning("Callback error on ingest_comments: %s", e)

        return added_or_refreshed

    def _prune_expired(self) -> bool:
        now = time.time()
        removed = False
        with self._lock:
            expired_keys = [k for k, v in self._active_terms.items() if v["expires_at"] < now]
            for k in expired_keys:
                del self._active_terms[k]
                removed = True
        return removed

    def _build_merged_replacements_unlocked(self) -> Dict[str, str]:
        if not self.enabled:
            return {}
        merged: Dict[str, str] = {}
        for info in self._active_terms.values():
            for pat, rep in info.get("replacements", {}).items():
                if pat and rep and pat != rep:
                    merged[pat] = rep
        return merged

    def get_dynamic_replacements(self) -> Dict[str, str]:
        """Return currently active dynamic replacement dictionary."""
        self._prune_expired()
        with self._lock:
            return self._build_merged_replacements_unlocked()

    def _poll_onecomme_api(self) -> List[str]:
        """Poll OneComme local HTTP API and return newly arrived comment texts."""
        url = self.onecomme_url
        if not url.startswith(("http://127.0.0.1", "http://localhost")):
            # Restrict to localhost for security
            return []

        req = urllib.request.Request(url, headers={"Accept": "application/json"}, method="GET")
        with urllib.request.urlopen(req, timeout=1.5) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            data = json.loads(raw)

        comments_list = []
        if isinstance(data, list):
            comments_list = data
        elif isinstance(data, dict):
            comments_list = data.get("comments", []) or data.get("data", []) or []

        new_texts: List[str] = []
        for item in comments_list:
            if not isinstance(item, dict):
                continue
            cid = str(item.get("id") or (item.get("data", {}) if isinstance(item.get("data"), dict) else {}).get("id") or "")
            comment_body = (
                item.get("comment")
                or (item.get("data", {}) if isinstance(item.get("data"), dict) else {}).get("comment")
                or item.get("text")
                or ""
            )
            if not comment_body:
                continue
            dedup_key = cid if cid else f"{item.get('name', '')}:{comment_body}"
            if dedup_key in self._seen_comment_ids:
                continue
            self._seen_comment_ids.add(dedup_key)
            if len(self._seen_comment_ids) > 2000:
                # Prevent unbounded set growth
                self._seen_comment_ids = set(list(self._seen_comment_ids)[-1000:])
            new_texts.append(str(comment_body))

        return new_texts

    def _poll_comment_file(self) -> List[str]:
        """Poll local text file for newly appended lines."""
        fpath = self.comment_file_path
        if not fpath or not os.path.isfile(fpath):
            return []
        try:
            stat = os.stat(fpath)
            if stat.st_mtime <= self._last_file_mtime and stat.st_size == self._last_file_size:
                return []
            self._last_file_mtime = stat.st_mtime
            self._last_file_size = stat.st_size
            with open(fpath, "r", encoding="utf-8", errors="replace") as f:
                lines = [ln.strip() for ln in f.readlines() if ln.strip()]
            # Take last 30 lines and deduplicate
            recent = lines[-30:]
            new_texts = []
            for ln in recent:
                if ln not in self._seen_comment_ids:
                    self._seen_comment_ids.add(ln)
                    new_texts.append(ln)
            return new_texts
        except Exception as e:
            logger.debug("Comment file read error: %s", e)
            return []

    def poll_once(self) -> int:
        """Perform a single poll cycle across OneComme API and/or text file."""
        if not self.enabled:
            return 0

        # 1. Try OneComme API
        try:
            api_texts = self._poll_onecomme_api()
            if api_texts:
                return self.ingest_comments(api_texts, source_label="onecomme_api")
            with self._lock:
                self._connected_source = "onecomme_api"
                self._last_error = None
        except Exception as api_err:
            with self._lock:
                if self._connected_source == "onecomme_api":
                    self._connected_source = "disconnected"
                self._last_error = str(api_err)

        # 2. Try fallback comment text file if configured
        if self.comment_file_path:
            file_texts = self._poll_comment_file()
            if file_texts:
                return self.ingest_comments(file_texts, source_label="comment_file")
            if os.path.isfile(self.comment_file_path):
                with self._lock:
                    self._connected_source = "comment_file"
                    self._last_error = None

        # Prune expired terms and notify if changed
        if self._prune_expired() and self.on_dictionary_updated:
            try:
                self.on_dictionary_updated(self.get_dynamic_replacements())
            except Exception:
                pass

        return 0

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            if self.enabled:
                try:
                    self.poll_once()
                except Exception as e:
                    logger.debug("CommentSynchronizer loop error: %s", e)
            self._stop_event.wait(self.poll_interval_sec)


_GLOBAL_COMMENT_SYNC_LOCK = threading.Lock()
_GLOBAL_COMMENT_SYNC: Optional[CommentSynchronizer] = None
_COMMENT_CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "config",
    "comment_sync_config.json",
)


def get_global_comment_sync() -> CommentSynchronizer:
    """Return the singleton CommentSynchronizer instance."""
    global _GLOBAL_COMMENT_SYNC
    with _GLOBAL_COMMENT_SYNC_LOCK:
        if _GLOBAL_COMMENT_SYNC is None:
            _GLOBAL_COMMENT_SYNC = CommentSynchronizer()
            if os.path.exists(_COMMENT_CONFIG_PATH):
                try:
                    with open(_COMMENT_CONFIG_PATH, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    if isinstance(data, dict):
                        _GLOBAL_COMMENT_SYNC.set_config(data)
                except Exception as e:
                    logger.warning("Failed to load comment_sync_config.json: %s", e)
        return _GLOBAL_COMMENT_SYNC


def save_global_comment_sync_config(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Update the global CommentSynchronizer and persist configuration."""
    syncer = get_global_comment_sync()
    status = syncer.set_config(cfg)
    try:
        os.makedirs(os.path.dirname(_COMMENT_CONFIG_PATH), exist_ok=True)
        with open(_COMMENT_CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "enabled": status["enabled"],
                    "onecomme_url": status["onecomme_url"],
                    "comment_file_path": status["comment_file_path"],
                    "word_ttl_sec": status["word_ttl_sec"],
                },
                f,
                indent=2,
            )
    except Exception as e:
        logger.warning("Failed to save comment_sync_config.json: %s", e)
    return status
