"""tools/profile_builder/lora_pool.py の単体テスト"""
import os

import numpy as np
import pytest

from tools.profile_builder import lora_pool as lp
from tools.profile_builder.local_transcribe import TranscribedSegment as TS

SR = 16000


def _audio(value):
    return np.full(SR * 10, value, dtype=np.float32)


def test_pool_joins_videos_and_segments_point_to_their_audio(tmp_path):
    lora = str(tmp_path)
    lp.add_video(lora, "a", _audio(0.1), SR, [TS(1.0, 3.0, "一")])
    os.utime(tmp_path / "label_pool" / "a.json", (1, 1))
    lp.add_video(lora, "b", _audio(0.5), SR, [TS(2.0, 3.0, "二"), TS(4.0, 5.0, "三")])

    audio, segments = lp.load_pool(lora)

    assert [s.text for s in segments] == ["一", "二", "三"]
    assert segments[1].start_sec == pytest.approx(2.0)
    third = segments[2]
    assert audio[int(third.start_sec * SR) : int(third.end_sec * SR)].mean() == pytest.approx(0.5, abs=0.01)
    assert lp.video_count(lora) == 2


def test_same_video_replaces_and_oldest_is_evicted(tmp_path):
    lora = str(tmp_path)
    for i in range(lp.MAX_POOL_VIDEOS + 1):
        lp.add_video(lora, f"v{i}", _audio(0.1), SR, [TS(0.0, 1.0, "x")])
        os.utime(tmp_path / "label_pool" / f"v{i}.json", (i + 1, i + 1))
        lp.add_video(lora, "v0" if i == 0 else f"v{i}", _audio(0.1), SR, [TS(0.0, 1.0, "x")])
        os.utime(tmp_path / "label_pool" / f"v{i}.json", (i + 1, i + 1))
    assert lp.video_count(lora) == lp.MAX_POOL_VIDEOS


def test_unreadable_entry_is_skipped(tmp_path):
    lora = str(tmp_path)
    lp.add_video(lora, "a", _audio(0.1), SR, [TS(0.0, 1.0, "良")])
    lp.add_video(lora, "b", _audio(0.1), SR, [TS(0.0, 1.0, "悪")])
    os.remove(tmp_path / "label_pool" / "b.flac")
    assert [s.text for s in lp.load_pool(lora)[1]] == ["良"]


def test_video_id_cannot_leave_pool_directory(tmp_path):
    lp.add_video(str(tmp_path), "../../evil", _audio(0.1), SR, [TS(0.0, 1.0, "x")])
    assert not (tmp_path.parent / "evil.json").exists()
    assert lp.video_count(str(tmp_path)) == 1


def test_speaker_ratio():
    assert lp.speaker_ratio([(0.0, 30.0), (40.0, 60.0)], 100.0) == pytest.approx(0.5)
    assert lp.speaker_ratio([], 0.0) == 0.0
