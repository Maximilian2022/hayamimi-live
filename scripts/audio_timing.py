"""
scripts/audio_timing.py
Log lines that show where the time goes between pressing the mic button and the first subtitle.

Each stage prints once per mic session. Any step of the audio loop that takes a second or more
also prints, because subtitles stop for that long.
"""

from __future__ import annotations

import sys
import threading
import time
from datetime import datetime

SLOW_STEP_SEC = 1.0
_lock = threading.Lock()
_session_start: float | None = None
_seen: set[str] = set()


def _write(message: str) -> None:
    print(f"[timing] {datetime.now():%H:%M:%S.%f}"[:-3] + f" {message}", file=sys.stderr, flush=True)


def start_session() -> None:
    global _session_start
    with _lock:
        _session_start = time.perf_counter()
        _seen.clear()
    _write("マイク接続")


def mark(stage: str) -> None:
    global _session_start
    with _lock:
        if stage in _seen:
            return
        _seen.add(stage)
        if _session_start is None:
            _session_start = time.perf_counter()
        elapsed = time.perf_counter() - _session_start
    _write(f"+{elapsed:.2f}s {stage}")


def report_slow(step: str, started: float) -> None:
    elapsed = time.perf_counter() - started
    if elapsed >= SLOW_STEP_SEC:
        _write(f"遅い処理: {step} {elapsed:.2f}s")
