# -*- coding: utf-8 -*-
"""OneComme (わんコメ) & Local Comment File Synchronizer for Hayamimi Live.

Fetches live viewer comments from:
1. Primary: OneComme Local HTTP API (http://127.0.0.1:11180/api/comments)
2. Secondary: Local text file polling (e.g. comment.txt)

Extracts proper nouns and viewer names via SudachiPy (Mode C) and hands them
to the active ASR engine with a sliding-window TTL (default 5 minutes). The
engine corrects a word only when the streamer says it: see comment_matcher.py.
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

try:
    import comment_matcher
    from comment_matcher import CommentTerm
except ImportError:
    from scripts import comment_matcher
    from scripts.comment_matcher import CommentTerm

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


class CommentTermExtractor:
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
            logger.debug("SudachiPy not available in CommentTermExtractor: %s", e)
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


_NAME_SEPARATOR = re.compile(r"[\s　/／|｜(（【\[]")
_NAME_NOISE = re.compile(r"[^0-9A-Za-z_\-一-龥々ぁ-んァ-ヶー]")


def clean_author_name(name: object) -> str:
    """Reduce a viewer's display name or handle to the part a streamer would say."""
    if not isinstance(name, str):
        return ""
    cleaned = name.strip().lstrip("@＠")
    cleaned = _NAME_SEPARATOR.split(cleaned, maxsplit=1)[0]
    cleaned = _NAME_NOISE.sub("", cleaned)
    cleaned = re.sub(r"[-_]?[0-9a-z]{3,}$", "", cleaned) if re.search(r"[-_][0-9a-z]{3,}$", cleaned) else cleaned
    cleaned = re.sub(r"[-_]?[0-9]+$", "", cleaned)
    cleaned = re.sub(r"(さん|ちゃん|くん|様)$", "", cleaned)
    return cleaned if 2 <= len(cleaned) <= 12 and cleaned not in _STOPWORDS else ""


def normalize_youtube_channel_url(url: str) -> str:
    """Normalize YouTube channel URL to its live streaming endpoint."""
    u = (url or "").strip()
    if not u:
        return ""
    if u.startswith("@"):
        u = f"https://www.youtube.com/{u}"
    elif not u.startswith("http://") and not u.startswith("https://"):
        u = f"https://www.youtube.com/{u}"

    u = u.split("?")[0].split("#")[0].rstrip("/")
    for suffix in ("/videos", "/featured", "/streams", "/community", "/playlists", "/about"):
        if u.endswith(suffix):
            u = u[:-len(suffix)]
            break
    if not u.endswith("/live"):
        u = f"{u}/live"
    return u


def parse_youtube_live_page(html: str, final_url: str = "") -> Tuple[Optional[str], bool, Optional[str]]:
    """Extract (video_id, is_live, initial_continuation) from YouTube /live HTML/URL.
    
    Returns:
        (video_id, is_live, continuation)
        is_live is True ONLY when the stream is actively live (not upcoming/waiting room).
    """
    video_id = None
    if final_url:
        m = re.search(r"[?&]v=([a-zA-Z0-9_-]{11})", final_url)
        if m:
            video_id = m.group(1)

    if not video_id and html:
        m = re.search(r'itemprop="videoId" content="([a-zA-Z0-9_-]{11})"', html)
        if m:
            video_id = m.group(1)
    if not video_id and html:
        m = re.search(r'"videoId":"([a-zA-Z0-9_-]{11})"', html)
        if m:
            video_id = m.group(1)

    if not video_id:
        return None, False, None

    is_upcoming = ('"isUpcoming":true' in html) or ('"status":"UPCOMING"' in html)
    is_live = False
    if not is_upcoming:
        if ('"isLive":true' in html) or ('"isLiveNow":true' in html) or ('"liveStreamability"' in html):
            is_live = True

    continuation = None
    if html:
        m_cont = re.search(r'"continuation":"([a-zA-Z0-9_\-\%]+)"', html)
        if m_cont:
            continuation = m_cont.group(1)

    return video_id, is_live, continuation


def fetch_youtube_live_chat_initial_continuation(video_id: str, timeout_sec: float = 4.0) -> Optional[str]:
    """Fetch initial chat continuation token from YouTube live_chat iframe page."""
    if not video_id:
        return None
    chat_url = f"https://www.youtube.com/live_chat?v={video_id}"
    req = urllib.request.Request(
        chat_url,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            html = resp.read().decode("utf-8", errors="replace")
        m = re.search(r'"continuation":"([a-zA-Z0-9_\-\%]+)"', html)
        if m:
            return m.group(1)
    except Exception as e:
        logger.debug("Failed to fetch initial chat continuation for %s: %s", video_id, e)
    return None


def parse_youtube_live_chat_response(data: dict) -> Tuple[List[str], Optional[str], float]:
    """Parse YouTube get_live_chat response JSON.
    
    Returns (messages, next_continuation, timeout_ms).
    """
    if not isinstance(data, dict):
        return [], None, 3000.0

    lcc = data.get("continuationContents", {}).get("liveChatContinuation", {})
    actions = lcc.get("actions", [])
    texts: List[str] = []

    for act in actions:
        item = act.get("addChatItemAction", {}).get("item", {})
        msg_renderer = item.get("liveChatTextMessageRenderer") or item.get("liveChatPaidMessageRenderer")
        if not msg_renderer:
            continue
        runs = msg_renderer.get("message", {}).get("runs", [])
        msg_text = "".join(r.get("text", "") for r in runs if isinstance(r, dict)).strip()
        if msg_text:
            texts.append(msg_text)

    continuations = lcc.get("continuations", [])
    next_continuation = None
    timeout_ms = 3000.0

    for c in continuations:
        cdata = c.get("timedContinuationData") or c.get("invalidationContinuationData") or c.get("liveChatReplayContinuationData")
        if cdata and "continuation" in cdata:
            next_continuation = cdata["continuation"]
            timeout_ms = float(cdata.get("timeoutMs", 3000))
            break

    return texts, next_continuation, timeout_ms


def parse_youtube_live_chat_names(data: dict) -> List[str]:
    """Return the display names of the authors in a get_live_chat response."""
    if not isinstance(data, dict):
        return []
    actions = data.get("continuationContents", {}).get("liveChatContinuation", {}).get("actions", [])
    names: List[str] = []
    for act in actions:
        item = act.get("addChatItemAction", {}).get("item", {})
        renderer = item.get("liveChatTextMessageRenderer") or item.get("liveChatPaidMessageRenderer")
        name = (renderer or {}).get("authorName", {}).get("simpleText")
        if name:
            names.append(name)
    return names


def poll_youtube_live_chat_api(
    continuation: str, timeout_sec: float = 4.0, names_out: Optional[List[str]] = None
) -> Tuple[List[str], Optional[str], float]:
    """POST to YouTube internal get_live_chat endpoint and return (texts, next_continuation, timeout_ms)."""
    if not continuation:
        return [], None, 3000.0
    url = "https://www.youtube.com/youtubei/v1/live_chat/get_live_chat"
    payload = {
        "context": {
            "client": {
                "clientName": "WEB",
                "clientVersion": "2.20240101.00.00",
                "hl": "ja",
                "gl": "JP",
            }
        },
        "continuation": continuation,
    }
    data_bytes = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data_bytes,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            data = json.loads(raw)
        if names_out is not None:
            names_out.extend(parse_youtube_live_chat_names(data))
        return parse_youtube_live_chat_response(data)
    except Exception as e:
        logger.debug("Failed poll_youtube_live_chat_api: %s", e)
        return [], None, 3000.0


class CommentSynchronizer:
    """Background daemon that polls OneComme HTTP API and/or a local comment text file."""

    def __init__(
        self,
        enabled: bool = False,
        onecomme_url: str = "http://127.0.0.1:11180/api/comments",
        comment_file_path: str = "",
        youtube_live_enabled: bool = False,
        youtube_channel_url: str = "",
        poll_interval_sec: float = 3.0,
        word_ttl_sec: float = 300.0,
        on_dictionary_updated: Optional[Callable[[List[CommentTerm]], None]] = None,
    ):
        self._lock = threading.Lock()
        self.enabled = bool(enabled)
        self.onecomme_url = onecomme_url.strip() or "http://127.0.0.1:11180/api/comments"
        self.comment_file_path = comment_file_path.strip()
        self.youtube_live_enabled = bool(youtube_live_enabled)
        self.youtube_channel_url = youtube_channel_url.strip()
        self.youtube_live_status: str = "disabled"  # disabled, waiting_stream, receiving, ended, error
        self.youtube_video_id: Optional[str] = None
        self._yt_continuation: Optional[str] = None
        self._yt_next_poll_time: float = 0.0
        self._yt_last_stream_check_time: float = 0.0
        self._yt_stream_check_interval: float = 30.0
        self.poll_interval_sec = max(0.5, float(poll_interval_sec))
        self.word_ttl_sec = max(0.1, float(word_ttl_sec))
        self.on_dictionary_updated = on_dictionary_updated

        self._extractor = CommentTermExtractor()
        self._pending_names: List[str] = []
        # surface -> { "reading": str, "expires_at": float }
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
        """Return current status and the words currently used for correction."""
        self._prune_expired()
        with self._lock:
            active_words = list(self._active_terms.keys())
            yt_status = self.youtube_live_status if (self.enabled and self.youtube_live_enabled) else "disabled"
            return {
                "enabled": self.enabled,
                "onecomme_url": self.onecomme_url,
                "comment_file_path": self.comment_file_path,
                "youtube_live_enabled": self.youtube_live_enabled,
                "youtube_channel_url": self.youtube_channel_url,
                "youtube_live_status": yt_status,
                "youtube_video_id": self.youtube_video_id,
                "word_ttl_sec": self.word_ttl_sec,
                "connected_source": self._connected_source if self.enabled else "disabled",
                "active_words": active_words,
                "active_words_count": len(active_words),
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
            if "youtube_live_enabled" in cfg:
                new_yt_en = bool(cfg["youtube_live_enabled"])
                if new_yt_en != self.youtube_live_enabled:
                    self.youtube_live_enabled = new_yt_en
                    if not self.youtube_live_enabled:
                        self.youtube_live_status = "disabled"
                        self._yt_continuation = None
                        self.youtube_video_id = None
                    else:
                        self.youtube_live_status = "waiting_stream"
                        self._yt_last_stream_check_time = 0.0
            if "youtube_channel_url" in cfg and isinstance(cfg["youtube_channel_url"], str):
                new_chan = cfg["youtube_channel_url"].strip()
                if new_chan != self.youtube_channel_url:
                    self.youtube_channel_url = new_chan
                    self._yt_continuation = None
                    self.youtube_video_id = None
                    self._yt_last_stream_check_time = 0.0
            if "word_ttl_sec" in cfg:
                try:
                    self.word_ttl_sec = max(30.0, float(cfg["word_ttl_sec"]))
                except Exception:
                    pass
            if not self.enabled:
                self._connected_source = "disabled"

        if notify_needed and self.on_dictionary_updated:
            try:
                self.on_dictionary_updated(self.get_active_terms())
            except Exception as e:
                logger.warning("Failed on_dictionary_updated callback: %s", e)

        return self.get_status()

    def ingest_comments(self, texts: List[str], source_label: str = "manual", names: Optional[List[str]] = None) -> int:
        """Process raw comments and their authors' names, and update the sliding window."""
        if not texts and not names:
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
                    self._active_terms[surf] = {"reading": reading, "expires_at": now + self.word_ttl_sec}
                    added_or_refreshed += 1
            for name in names or []:
                surf = clean_author_name(name)
                if surf:
                    self._active_terms[surf] = {"reading": self._name_reading(surf), "expires_at": now + self.word_ttl_sec}
                    added_or_refreshed += 1

        self._prune_expired()
        if added_or_refreshed > 0 and self.on_dictionary_updated:
            try:
                self.on_dictionary_updated(self.get_active_terms())
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

    def get_active_terms(self) -> List[CommentTerm]:
        """Return the comment words currently used for correction, newest last."""
        self._prune_expired()
        with self._lock:
            if not self.enabled:
                return []
            ordered = sorted(self._active_terms.items(), key=lambda kv: kv[1]["expires_at"])
            return [CommentTerm(surface, info["reading"]) for surface, info in ordered]

    def _take_pending_names(self) -> List[str]:
        names, self._pending_names = self._pending_names, []
        return names

    @staticmethod
    def _name_reading(surface: str) -> str:
        if re.fullmatch(r"[0-9A-Za-z_\-]+", surface):
            return ""
        try:
            return comment_matcher.reading_of(surface)
        except Exception:
            return surface if re.fullmatch(r"[ァ-ヶーぁ-ん]+", surface) else ""

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
            data_part = item.get("data") if isinstance(item.get("data"), dict) else {}
            self._pending_names.append(item.get("name") or data_part.get("name") or data_part.get("displayName") or "")

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

    def _poll_youtube_live_chat(self) -> List[str]:
        """Poll YouTube live chat using internal get_live_chat endpoint without external libraries."""
        if not self.enabled or not self.youtube_live_enabled or not self.youtube_channel_url:
            return []

        now = time.time()

        # Step 1: Probe channel for an active live stream if no continuation token
        if not self._yt_continuation:
            if now - self._yt_last_stream_check_time < self._yt_stream_check_interval:
                return []
            self._yt_last_stream_check_time = now
            live_url = normalize_youtube_channel_url(self.youtube_channel_url)
            if not live_url:
                self.youtube_live_status = "error"
                return []

            try:
                req = urllib.request.Request(
                    live_url,
                    headers={
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                        "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
                    },
                )
                with urllib.request.urlopen(req, timeout=4.5) as resp:
                    final_url = resp.geturl()
                    html = resp.read().decode("utf-8", errors="replace")

                vid, is_live, cont = parse_youtube_live_page(html, final_url)
                if vid and is_live:
                    self.youtube_video_id = vid
                    if not cont:
                        cont = fetch_youtube_live_chat_initial_continuation(vid)
                    if cont:
                        self._yt_continuation = cont
                        self.youtube_live_status = "receiving"
                        self._yt_next_poll_time = now
                        logger.info("YouTube Live detected! video_id=%s, starting live chat sync", vid)
                    else:
                        self.youtube_live_status = "waiting_stream"
                elif vid and not is_live:
                    self.youtube_video_id = vid
                    self.youtube_live_status = "waiting_stream"
                else:
                    self.youtube_video_id = None
                    self.youtube_live_status = "waiting_stream"
            except Exception as e:
                logger.debug("YouTube Live probe failed: %s", e)
                self.youtube_live_status = "waiting_stream"
            return []

        # Step 2: Poll live chat messages with continuation token
        if now < self._yt_next_poll_time:
            return []

        texts, next_cont, timeout_ms = poll_youtube_live_chat_api(self._yt_continuation, names_out=self._pending_names)
        if next_cont:
            self._yt_continuation = next_cont
            self.youtube_live_status = "receiving"
            self._yt_next_poll_time = now + max(1.5, timeout_ms / 1000.0)
        else:
            self._yt_continuation = None
            self.youtube_live_status = "ended"
            self._yt_last_stream_check_time = now
            logger.info("YouTube Live chat ended for video_id=%s", self.youtube_video_id)

        return texts

    def poll_once(self) -> int:
        """Perform a single poll cycle across OneComme API and/or text file."""
        if not self.enabled:
            return 0

        # 1. Try OneComme API
        try:
            api_texts = self._poll_onecomme_api()
            if api_texts:
                return self.ingest_comments(api_texts, "onecomme_api", self._take_pending_names())
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

        # 3. Try YouTube Live Chat directly if enabled and configured
        if self.youtube_live_enabled and self.youtube_channel_url:
            try:
                yt_texts = self._poll_youtube_live_chat()
                if yt_texts:
                    return self.ingest_comments(yt_texts, "youtube_live", self._take_pending_names())
            except Exception as yt_err:
                logger.debug("YouTube Live polling error: %s", yt_err)

        # Prune expired terms and notify if changed
        if self._prune_expired() and self.on_dictionary_updated:
            try:
                self.on_dictionary_updated(self.get_active_terms())
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
_COMMENT_CONFIG_PATH: Optional[str] = None

def _get_comment_config_path() -> str:
    if _COMMENT_CONFIG_PATH:
        return _COMMENT_CONFIG_PATH
    try:
        from scripts.config_store import get_storage_dir, CONFIG_FILENAME_COMMENT
        return os.path.join(get_storage_dir(), CONFIG_FILENAME_COMMENT)
    except Exception:
        return os.path.join(
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
            cfg_path = _get_comment_config_path()
            try:
                from scripts.config_store import load_json_with_backup
                data = load_json_with_backup(cfg_path, default=None)
            except Exception:
                data = None
                if os.path.exists(cfg_path):
                    try:
                        with open(cfg_path, "r", encoding="utf-8") as f:
                            data = json.load(f)
                    except Exception as e:
                        logger.warning("Failed to load comment_sync_config.json: %s", e)
            if isinstance(data, dict):
                _GLOBAL_COMMENT_SYNC.set_config(data)
        return _GLOBAL_COMMENT_SYNC


def save_global_comment_sync_config(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Update the global CommentSynchronizer and persist configuration."""
    syncer = get_global_comment_sync()
    status = syncer.set_config(cfg)
    cfg_path = _get_comment_config_path()
    payload = {
        "enabled": status["enabled"],
        "onecomme_url": status["onecomme_url"],
        "comment_file_path": status["comment_file_path"],
        "youtube_live_enabled": status.get("youtube_live_enabled", False),
        "youtube_channel_url": status.get("youtube_channel_url", ""),
        "word_ttl_sec": status["word_ttl_sec"],
    }
    try:
        from scripts.config_store import atomic_save_json
        atomic_save_json(cfg_path, payload, create_backup=True)
    except Exception:
        try:
            os.makedirs(os.path.dirname(cfg_path), exist_ok=True)
            with open(cfg_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
        except Exception as e:
            logger.warning("Failed to save comment_sync_config.json: %s", e)
    return status
