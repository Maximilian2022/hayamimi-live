"""scripts/audio_timing.py の単体テスト"""
import os
import re
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import audio_timing  # noqa: E402


def test_each_stage_prints_once_per_session(capsys):
    audio_timing.start_session()
    audio_timing.mark("first")
    audio_timing.mark("first")
    assert capsys.readouterr().err.count("first") == 1
    audio_timing.start_session()
    audio_timing.mark("first")
    assert capsys.readouterr().err.count("first") == 1


def test_line_shows_clock_time_and_elapsed_seconds(capsys):
    audio_timing.start_session()
    capsys.readouterr()
    audio_timing.mark("stage")
    assert re.fullmatch(r"\[timing\] \d\d:\d\d:\d\d\.\d{3} \+\d+\.\d\ds stage\n", capsys.readouterr().err)


def test_only_slow_steps_are_reported(capsys):
    audio_timing.report_slow("quick", time.perf_counter())
    assert capsys.readouterr().err == ""
    audio_timing.report_slow("slow", time.perf_counter() - 2.5)
    assert "遅い処理: slow 2.5" in capsys.readouterr().err
