"""tools/profile_builder/lora_labeler.py の単体テスト"""
import json
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from tools.profile_builder import lora_labeler as ll
from tools.profile_builder.local_transcribe import TranscribedSegment as TS


def _client(replies):
    client = MagicMock()
    client.files.upload.return_value.state.name = "ACTIVE"
    client.models.generate_content.side_effect = replies
    return client


def _ok(*items):
    return MagicMock(text=json.dumps({"segments": list(items)}, ensure_ascii=False))


def _label(tmp_path, client, n=2):
    audio = np.zeros(16000 * 40, dtype=np.float32)
    segments = [(float(i * 3), float(i * 3 + 2)) for i in range(n)]
    return ll.label_segments_with_gemini(client, "m", audio, 16000, segments, [""] * n, str(tmp_path))


def test_usable_label_rules():
    assert ll.is_usable_label("こんにちは", "speech", "こんにちは")
    assert not ll.is_usable_label("", "speech", "こんにちは")
    assert not ll.is_usable_label("こんにちは", "read_aloud", "こんにちは")
    assert not ll.is_usable_label("いやいや大丈夫", "speech", "すんませんわらいやいや大丈夫")
    assert not ll.is_usable_label("あいつら生きてんの", "speech", "いいけど")
    assert ll.is_usable_label("こんにちは", "speech", "")


def test_hints_are_empty_when_whisper_cannot_load():
    pytest.importorskip("faster_whisper")
    with patch("faster_whisper.WhisperModel", side_effect=RuntimeError("no gpu")):
        assert ll.transcribe_ranges(np.zeros(16000), 16000, [(0.0, 0.5), (0.5, 1.0)]) == ["", ""]


def test_hints_are_transcribed_per_range():
    pytest.importorskip("faster_whisper")
    model = MagicMock()
    model.transcribe.side_effect = [([MagicMock(text=" あ")], None), ([MagicMock(text="い ")], None)]
    with patch("faster_whisper.WhisperModel", return_value=model):
        assert ll.transcribe_ranges(np.zeros(16000), 16000, [(0.0, 0.5), (0.5, 1.0)]) == ["あ", "い"]


def test_batches_and_keeps_boundaries(tmp_path):
    first = [{"id": i, "text": f"発話{i}", "kind": "speech"} for i in range(10)]
    client = _client([_ok(*first), _ok({"id": 10, "text": "最後", "kind": "speech"})])
    labelled = _label(tmp_path, client, n=11)
    assert client.models.generate_content.call_count == 2
    assert labelled[10] == TS(30.0, 32.0, "最後")
    assert client.files.delete.call_count == 2


def test_quota_error_stops_and_keeps_finished_batches(tmp_path):
    first = [{"id": i, "text": f"発話{i}", "kind": "speech"} for i in range(10)]
    client = _client([_ok(*first), Exception("429 RESOURCE_EXHAUSTED")])
    assert len(_label(tmp_path, client, n=25)) == 10
    assert client.models.generate_content.call_count == 2


def test_overload_is_retried_then_gives_up_after_two_batches(tmp_path):
    client = _client([Exception("503 UNAVAILABLE")] * 4)
    with patch.object(ll.time, "sleep"):
        assert _label(tmp_path, client, n=35) == []
    assert client.models.generate_content.call_count == 4


def test_overload_recovers_on_retry(tmp_path):
    client = _client([Exception("503 UNAVAILABLE"), _ok({"id": 0, "text": "はい", "kind": "speech"})])
    with patch.object(ll.time, "sleep"):
        assert _label(tmp_path, client, n=1) == [TS(0.0, 2.0, "はい")]


def test_accepts_bare_list_response_and_ignores_malformed_items(tmp_path):
    client = _client([
        MagicMock(text=json.dumps([{"id": 0, "text": "はい", "kind": "speech"}, "壊れた項目"], ensure_ascii=False)),
    ])
    assert _label(tmp_path, client, n=1) == [TS(0.0, 2.0, "はい")]


def test_broken_response_skips_batch_instead_of_stopping(tmp_path):
    first = [{"id": i, "text": f"発話{i}", "kind": "speech"} for i in range(10)]
    third = [{"id": 20, "text": "最後", "kind": "speech"}]
    client = _client([
        _ok(*first),
        MagicMock(text="{壊れた応答"), MagicMock(text="{壊れた応答"),
        _ok(*third),
    ])
    with patch.object(ll.time, "sleep"), \
         patch.object(ll, "safe_parse_json", side_effect=lambda t: json.loads(t)):
        labelled = _label(tmp_path, client, n=21)
    assert len(labelled) == 11
    assert client.models.generate_content.call_count == 4


def test_lite_models_do_not_write_labels():
    assert ll.choose_label_model("gemini-flash-lite-latest", "gemini-3.8-flash") == "gemini-3.8-flash"
    assert ll.choose_label_model("gemini-3.7-flash", "gemini-3.8-flash") == "gemini-3.7-flash"
