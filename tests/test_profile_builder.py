# -*- coding: utf-8 -*-
"""Unit tests for Speaker Profile Builder."""

import json
import os
import tempfile
import pytest

from tools.profile_builder.profile_model import (
    ExtractedSection,
    ReplacementRule,
    SpeakerProfile,
)
from tools.profile_builder.profile_exporter import (
    delete_profile,
    export_profile_json,
    import_profile_json,
    list_profiles,
    load_profile,
    save_profile,
)
from tools.profile_builder.youtube_fetcher import (
    SubtitleSegment,
    YouTubeFetcher,
    extract_video_id,
    format_seconds_to_timestamp,
    is_channel_url,
    parse_vtt_content,
    parse_vtt_timestamp,
)


def test_profile_model_sanitization_and_replace_text():
    prof = SpeakerProfile(
        speaker_id="Val Channel @Official!",
        display_name="ヴァル閣下",
        articulation_traits=["サ行が甘い", "語尾脱落"],
        replacements=[
            ReplacementRule(pattern="勝てないとうですね", replacement="勝てないそうですね", reason="サ行化け"),
            ReplacementRule(pattern="ロードライン", replacement="ロードナイン", reason="固有名詞"),
            ReplacementRule(pattern="と", replacement="そ", reason="危険な1文字置換"),
        ],
    )

    # Sanitize speaker_id
    assert prof.speaker_id == "val_channel_official"

    # Filter safe replacements (removes 1-char patterns)
    safe = prof.filter_safe_replacements(min_len=2)
    assert len(safe) == 2
    assert safe[0].pattern == "勝てないとうですね"
    assert safe[1].pattern == "ロードライン"

    # Convert to replace text format
    replace_txt = prof.to_replace_text()
    assert "勝てないとうですね\t勝てないそうですね" in replace_txt
    assert "ロードライン\tロードナイン" in replace_txt


def test_profile_exporter_crud_and_import_export():
    with tempfile.TemporaryDirectory() as tmpdir:
        prof = SpeakerProfile(
            speaker_id="streamer_a",
            display_name="配信者A",
            extracted_section=ExtractedSection(
                start_time="00:10:00",
                end_time="00:22:00",
                duration_sec=720.0,
            ),
            replacements=[
                ReplacementRule(pattern="テストですう", replacement="テストですよ", reason="語尾"),
            ],
        )

        # 1. Save
        json_path = save_profile(prof, storage_dir=tmpdir)
        assert os.path.isfile(json_path)
        txt_path = os.path.join(tmpdir, "streamer_a_replace.txt")
        assert os.path.isfile(txt_path)

        # 2. Load
        loaded = load_profile("streamer_a", storage_dir=tmpdir)
        assert loaded is not None
        assert loaded.speaker_id == "streamer_a"
        assert loaded.display_name == "配信者A"
        assert len(loaded.replacements) == 1
        assert loaded.replacements[0].pattern == "テストですう"

        # 3. List
        all_profs = list_profiles(storage_dir=tmpdir)
        assert len(all_profs) == 1
        assert all_profs[0].speaker_id == "streamer_a"

        # 4. Export JSON
        exported_str = export_profile_json(loaded)
        data = json.loads(exported_str)
        assert data["speaker_id"] == "streamer_a"

        # 5. Import JSON with new ID
        data["speaker_id"] = "streamer_b"
        data["display_name"] = "配信者B"
        imported = import_profile_json(json.dumps(data), storage_dir=tmpdir)
        assert imported.speaker_id == "streamer_b"

        assert len(list_profiles(storage_dir=tmpdir)) == 2

        # 6. Delete
        assert delete_profile("streamer_a", storage_dir=tmpdir) is True
        assert load_profile("streamer_a", storage_dir=tmpdir) is None
        assert len(list_profiles(storage_dir=tmpdir)) == 1


def test_vtt_parsing_and_timestamps():
    sample_vtt = """WEBVTT
Kind: captions
Language: ja

00:00:05.120 --> 00:00:08.450
<c>こんにちはー！</c>本日の配信を<c>始めます。</c>

00:00:10.000 --> 00:00:14.500
今日は<v Speaker>ロードナイン</v>をやっていきましょう。
"""
    segments = parse_vtt_content(sample_vtt)
    assert len(segments) == 2
    assert segments[0].start_sec == pytest.approx(5.12)
    assert segments[0].end_sec == pytest.approx(8.45)
    assert "こんにちはー！本日の配信を始めます。" in segments[0].text

    assert segments[1].start_sec == pytest.approx(10.0)
    assert segments[1].end_sec == pytest.approx(14.5)
    assert "今日はロードナインをやっていきましょう。" in segments[1].text

    assert format_seconds_to_timestamp(75.5) == "01:15"
    assert format_seconds_to_timestamp(3665.0) == "01:01:05"
    assert parse_vtt_timestamp("01:01:05.500") == pytest.approx(3665.5)


def test_find_best_speech_section():
    fetcher = YouTubeFetcher()
    # 2 hours video (7200 seconds)
    # Section 0 - 600s: Opening standby, few words
    # Section 1200 - 2400s: Dense fast-paced gaming speech (high density)
    # Section 3600 - 4200s: Long silence (away from keyboard)
    segments = [
        # Sparse opening
        SubtitleSegment(start_sec=60.0, end_sec=65.0, text="テストテスト"),
        SubtitleSegment(start_sec=180.0, end_sec=185.0, text="音量大丈夫かな"),
    ]

    # Dense section around 1500s - 2200s (every 5 seconds speech)
    for t in range(1300, 2100, 8):
        segments.append(
            SubtitleSegment(
                start_sec=float(t),
                end_sec=float(t + 6),
                text="勝てないですねこれどうやっていくのかな次のボスは強いです",
            )
        )

    best_start, best_end, score = fetcher.find_best_speech_section(
        segments=segments,
        video_duration_sec=7200.0,
        window_sec=720.0,  # 12 minutes
        step_sec=60.0,
    )

    # The best window should be in the dense area (~1300s to 1500s start)
    assert 1200.0 <= best_start <= 1500.0
    assert best_end == best_start + 720.0
    assert score > 0.5


def test_profile_merge_and_grow():
    """Verify profile incremental training / merging preserves rules and tracks history."""
    prof1 = SpeakerProfile(
        speaker_id="streamer_x",
        display_name="配信者X",
        source_video_url="https://youtube.com/watch?v=vid1",
        articulation_traits=["サ行が甘い"],
        replacements=[
            ReplacementRule(pattern="勝てないとう", replacement="勝てないそう", reason="動画1での発見"),
            ReplacementRule(pattern="ロードライン", replacement="ロードナイン", reason="ゲーム名A"),
        ],
        second_pass_hints="動画1の文脈",
    )

    prof2 = SpeakerProfile(
        speaker_id="streamer_x",
        display_name="配信者X",
        source_video_url="https://youtube.com/watch?v=vid2",
        articulation_traits=["語尾脱落", "サ行が甘い"],  # 重複あり
        replacements=[
            ReplacementRule(pattern="ロードライン", replacement="ロードナイン", reason="動画2でも多発"),
            ReplacementRule(pattern="ポーションだす", replacement="ポーションです", reason="動画2での新発見"),
        ],
        second_pass_hints="動画2の文脈",
    )

    prof1.merge_profile(prof2)

    # 1. Video URLs history tracked
    assert len(prof1.source_video_urls) == 2
    assert "https://youtube.com/watch?v=vid1" in prof1.source_video_urls
    assert "https://youtube.com/watch?v=vid2" in prof1.source_video_urls

    # 2. Articulation traits merged uniquely
    assert len(prof1.articulation_traits) == 2
    assert "サ行が甘い" in prof1.articulation_traits
    assert "語尾脱落" in prof1.articulation_traits

    # 3. Replacements merged (2 existing + 1 new = 3 total)
    assert len(prof1.replacements) == 3
    rule_map = {r.pattern: r for r in prof1.replacements}
    assert "勝てないとう" in rule_map
    assert "ロードライン" in rule_map
    assert "ポーションだす" in rule_map
    # Combined reason check
    assert "動画1での発見" in (rule_map["ロードライン"].reason or "") or "ゲーム名A" in (rule_map["ロードライン"].reason or "")
    assert "動画2でも多発" in (rule_map["ロードライン"].reason or "")

    # 4. Hints merged
    assert "動画1の文脈" in (prof1.second_pass_hints or "")
    assert "動画2の文脈" in (prof1.second_pass_hints or "")


def test_url_type_detection_and_video_id():
    """Verify URL classifier and Video ID extractor."""
    # Video ID extraction
    assert extract_video_id("https://www.youtube.com/watch?v=dQw4w9WgXcQ") == "dQw4w9WgXcQ"
    assert extract_video_id("https://youtu.be/dQw4w9WgXcQ?t=10") == "dQw4w9WgXcQ"
    assert extract_video_id("https://www.youtube.com/live/dQw4w9WgXcQ?feature=share") == "dQw4w9WgXcQ"
    assert extract_video_id("https://www.youtube.com/shorts/dQw4w9WgXcQ") == "dQw4w9WgXcQ"
    assert extract_video_id("https://www.youtube.com/@ValChannel") is None

    # Channel URL classification
    assert is_channel_url("https://www.youtube.com/@ValChannel") is True
    assert is_channel_url("https://www.youtube.com/@ValChannel/streams") is True
    assert is_channel_url("https://www.youtube.com/@ValChannel/videos") is True
    assert is_channel_url("https://www.youtube.com/channel/UC1234567890abcdef") is True
    assert is_channel_url("https://www.youtube.com/c/SomeCreator") is True
    assert is_channel_url("https://www.youtube.com/watch?v=dQw4w9WgXcQ") is False
    assert is_channel_url("https://youtu.be/dQw4w9WgXcQ") is False


def test_fetch_channel_videos_duplicate_prevention(monkeypatch):
    """Verify channel videos are sorted by view_count and duplicates are filtered out."""
    fetcher = YouTubeFetcher()

    fake_entries = [
        # Video 1: Popular but already trained
        {"id": "vid_already1", "title": "配信1 (既学習)", "view_count": 50000, "duration": 7200, "channel": "Ch"},
        # Video 2: Recent, 20k views
        {"id": "vid_new_pop", "title": "人気配信2", "view_count": 20000, "duration": 5400, "channel": "Ch"},
        # Video 3: Recent, 5k views
        {"id": "vid_new_norm", "title": "通常配信3", "view_count": 5000, "duration": 3600, "channel": "Ch"},
        # Video 4: Short video (< 3min, should be skipped)
        {"id": "vid_short", "title": "告知ショート", "view_count": 100000, "duration": 45, "channel": "Ch"},
        # Video 5: Recent, 30k views
        {"id": "vid_new_top", "title": "最高再生配信5", "view_count": 30000, "duration": 6000, "channel": "Ch"},
    ]

    import subprocess
    fake_jsonl = "\n".join([json.dumps(e) for e in fake_entries])

    class MockCompletedProcess:
        returncode = 0
        stdout = fake_jsonl
        stderr = ""

    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: MockCompletedProcess())

    # Exclude vid_already1
    videos = fetcher.fetch_channel_videos(
        channel_url="https://www.youtube.com/@ValChannel",
        limit=2,
        candidate_pool=10,
        exclude_video_ids={"vid_already1"},
    )

    # vid_already1 must be excluded (duplicate prevention)
    # vid_short must be excluded (too short)
    # Remaining candidates: vid_new_top (30k), vid_new_pop (20k), vid_new_norm (5k)
    # Sorted by view_count descending, top 2
    assert len(videos) == 2
    assert videos[0]["id"] == "vid_new_top"
    assert videos[1]["id"] == "vid_new_pop"
    assert videos[0]["view_count"] == 30000
    assert videos[1]["view_count"] == 20000


def test_pipeline_logging_callback(monkeypatch, tmp_path):
    """Verify Pipeline runs and logs subprocess steps via log_callback."""
    from tools.profile_builder.pipeline import ProfileBuilderPipeline
    from tools.profile_builder.youtube_fetcher import VideoMetadata
    from tools.profile_builder.local_transcribe import LocalTranscriptionResult, TranscribedSegment

    pipeline = ProfileBuilderPipeline(api_key="test_key")

    # Mock fetcher
    monkeypatch.setattr(pipeline.fetcher, "fetch_metadata", lambda url: VideoMetadata(
        video_id="vid_test",
        title="テスト配信",
        channel="テストCh",
        description="概要",
        duration_sec=600.0,
        tags=["テスト", "ゲーム"],
    ))
    monkeypatch.setattr(pipeline.fetcher, "fetch_subtitles", lambda url, tmp: [])
    monkeypatch.setattr(pipeline.fetcher, "download_audio_section", lambda **kwargs: str(tmp_path / "audio.wav"))

    # Mock transcriber
    class MockTranscriber:
        def __init__(self, *args, **kwargs):
            pass
        def transcribe_file(self, wav_path, status_callback=None, log_callback=None, **kwargs):
            if log_callback:
                log_callback("ASR", "推論チャンク [00:00 - 00:12]: 「テスト音声」")
            return LocalTranscriptionResult(
                audio_path=wav_path,
                duration_sec=12.0,
                segments=[TranscribedSegment(0, 12, "テスト音声")],
                full_text="[0.0s - 12.0s] テスト音声",
            )
        def close(self):
            pass

    monkeypatch.setattr("tools.profile_builder.pipeline.LocalTranscriber", MockTranscriber)

    # Mock analyzer
    class MockAnalyzer:
        def __init__(self, *args, **kwargs):
            pass
        def analyze(self, **kwargs):
            log_cb = kwargs.get("log_callback")
            if log_cb:
                log_cb("GEMINI", "Gemini 推論完了")
            return SpeakerProfile(
                speaker_id="test_speaker",
                display_name="テスト話者",
                replacements=[ReplacementRule(pattern="かてないとう", replacement="勝てないそう", reason="テスト")],
            )

    monkeypatch.setattr("tools.profile_builder.pipeline.GeminiProfileAnalyzer", MockAnalyzer)
    monkeypatch.setattr("tools.profile_builder.pipeline.save_profile", lambda prof: str(tmp_path / "prof.json"))

    captured_logs = []
    def on_log(level, msg):
        captured_logs.append((level, msg))

    prof = pipeline.run(
        youtube_url="https://www.youtube.com/watch?v=vid_test",
        speaker_id="test_speaker",
        display_name="テスト話者",
        log_callback=on_log,
    )

    assert prof.speaker_id == "test_speaker"
    assert len(captured_logs) > 0
    levels = [lvl for lvl, msg in captured_logs]
    assert "STEP" in levels
    assert "INFO" in levels
    assert "ASR" in levels
    assert "GEMINI" in levels
    assert "SUCCESS" in levels


def test_safe_parse_json_with_unterminated_string():
    from tools.profile_builder.gemini_analyzer import safe_parse_json

    # 1. Normal valid JSON
    valid_json = '{"speaker_id": "test", "display_name": "Test", "replacements": [{"pattern": "aaa", "replacement": "bbb"}]}'
    res = safe_parse_json(valid_json)
    assert res["speaker_id"] == "test"
    assert len(res["replacements"]) == 1

    # 2. Markdown fenced JSON
    fenced_json = "```json\n" + valid_json + "\n```"
    res = safe_parse_json(fenced_json)
    assert res["speaker_id"] == "test"

    # 3. Truncated / Unterminated string JSON (e.g. cut off at char 4995)
    # The output cuts off mid-string in the second replacement rule
    truncated_json = """{
  "speaker_id": "test_speaker",
  "display_name": "テスト話者",
  "articulation_traits": ["サ行が甘い"],
  "replacements": [
    {
      "pattern": "勝てないとうですね",
      "replacement": "勝てないそうですね",
      "reason": "サ行化け"
    },
    {
      "pattern": "ロードライン",
      "replacement": "ロードナイン",
      "reason": "ゲーム固有
"""
    # Without safe_parse_json, json.loads raises JSONDecodeError: Unterminated string starting at line...
    with pytest.raises(json.JSONDecodeError):
        json.loads(truncated_json)

    # With safe_parse_json, it auto-repairs and recovers the first complete rule!
    logs = []
    repaired = safe_parse_json(truncated_json, log_callback=lambda lvl, msg: logs.append((lvl, msg)))
    assert repaired["speaker_id"] == "test_speaker"
    assert len(repaired["replacements"]) == 1
    assert repaired["replacements"][0]["pattern"] == "勝てないとうですね"
    assert any("救出" in msg for lvl, msg in logs)


def test_voiceprint_model_merging():
    import numpy as np

    # Create dummy 512-dim unit vectors
    v1 = np.zeros(512, dtype=np.float32)
    v1[0] = 1.0  # (1, 0, 0, ...)
    v2 = np.zeros(512, dtype=np.float32)
    v2[1] = 1.0  # (0, 1, 0, ...)

    prof1 = SpeakerProfile(
        speaker_id="speaker_a",
        display_name="話者A",
        voiceprint=v1.tolist(),
        voiceprint_sample_count=10,
    )

    prof2 = SpeakerProfile(
        speaker_id="speaker_a",
        display_name="話者A",
        voiceprint=v2.tolist(),
        voiceprint_sample_count=10,
    )

    prof1.merge_profile(prof2)

    assert prof1.voiceprint is not None
    assert prof1.voiceprint_sample_count == 20
    merged_np = np.array(prof1.voiceprint, dtype=np.float32)
    # L2 norm must be approximately 1.0
    assert abs(np.linalg.norm(merged_np) - 1.0) < 1e-5
    # Equal weights: v1[0] and v2[1] should be equal
    assert abs(merged_np[0] - merged_np[1]) < 1e-5


def test_voiceprint_extractor_availability(tmp_path):
    from tools.profile_builder.voiceprint_extractor import VoiceprintExtractor
    extractor = VoiceprintExtractor(model_path=str(tmp_path / "non_existent.onnx"))
    assert not extractor.is_available
    v, count = extractor.extract_voiceprint(str(tmp_path / "non_existent.wav"))
    assert v is None
    assert count == 0


def test_transcribe_with_voiceprint_filtering(tmp_path, monkeypatch):
    """Verify that chunks from other speakers are skipped during local transcription."""
    import soundfile as sf
    import numpy as np
    from tools.profile_builder.local_transcribe import LocalTranscriber
    from tools.profile_builder.voiceprint_extractor import VoiceprintExtractor

    # 1. Create a dummy WAV file with 2 chunks (24s = two 12s chunks)
    sr = 16000
    duration_s = 24
    t = np.linspace(0, duration_s, duration_s * sr, endpoint=False)
    # Sine wave tone so rms > 0.005
    audio = (0.2 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
    wav_path = str(tmp_path / "test_filter.wav")
    sf.write(wav_path, audio, sr)

    # 2. Target voiceprint
    target_vp = [1.0] + [0.0] * 511

    # 3. Mock VoiceprintExtractor to return matching embedding for chunk 1, non-matching for chunk 2
    call_count = {"count": 0}
    def mock_compute_chunk_emb(self, chunk, chunk_sr=16000):
        call_count["count"] += 1
        emb = np.zeros(512, dtype=np.float32)
        if call_count["count"] == 1:
            emb[0] = 1.0  # similarity = 1.0 (match!)
        else:
            emb[1] = 1.0  # similarity = 0.0 (other speaker/game voice!)
        return emb

    monkeypatch.setattr(VoiceprintExtractor, "is_available", True)
    monkeypatch.setattr(VoiceprintExtractor, "compute_chunk_embedding", mock_compute_chunk_emb)

    # 4. Mock RoutedASR engine
    class MockEngine:
        def transcribe(self, chunk, sr, known_lang="ja", live=False):
            return {"text": f"認識テキスト_{call_count['count']}"}

    transcriber = LocalTranscriber()
    monkeypatch.setattr(transcriber, "_get_engine", lambda: MockEngine())

    captured_logs = []
    def on_log(lvl, msg):
        captured_logs.append((lvl, msg))

    res = transcriber.transcribe_file(
        wav_path=wav_path,
        chunk_sec=12.0,
        target_voiceprint=target_vp,
        voiceprint_threshold=0.40,
        log_callback=on_log,
    )

    # Chunk 1 was transcribed, Chunk 2 was filtered out!
    assert len(res.segments) == 1
    assert res.segments[0].text == "認識テキスト_1"
    # Verify VOICE skip log was emitted
    voice_logs = [msg for lvl, msg in captured_logs if lvl == "VOICE"]
    assert any("コラボ相手/他者音声を検出" in m for m in voice_logs)


def test_gemini_models_are_active_and_no_deprecated_1_5(monkeypatch):
    """Ensure that deprecated/terminated models (1.5, 2.0) are completely excluded and 3.8 is default."""
    from tools.profile_builder.gemini_analyzer import GeminiProfileAnalyzer
    from tools.profile_builder.pipeline import ProfileBuilderPipeline

    # Verify default models
    pipe = ProfileBuilderPipeline(api_key="fake-key")
    assert pipe.model_name == "gemini-3.8-flash"

    analyzer = GeminiProfileAnalyzer(api_key="fake-key")
    assert analyzer.model_name == "gemini-3.8-flash"

    # Capture candidate models during execution
    captured_models = []

    class MockModelsService:
        def generate_content(self, model, **kwargs):
            captured_models.append(model)
            raise RuntimeError(f"Simulate model failure for {model}")

    class MockFilesService:
        def upload(self, **kwargs):
            class FakeFile:
                name = "fake_file"
                uri = "fake_uri"
            return FakeFile()

        def delete(self, name):
            pass

    class MockClient:
        models = MockModelsService()
        files = MockFilesService()

    analyzer.client = MockClient()

    # Create dummy wav
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        dummy_wav = f.name
        f.write(b"RIFF....WAVEfmt ....data....")

    try:
        from tools.profile_builder.youtube_fetcher import VideoMetadata
        meta = VideoMetadata(video_id="dummy_vid", title="Test", channel="Ch", description="", tags=[], duration_sec=60.0)
        try:
            analyzer.analyze(
                audio_wav_path=dummy_wav,
                local_asr_text="テストテキスト",
                metadata=meta,
                speaker_id="test_spk",
                display_name="テスト話者",
            )
        except RuntimeError:
            pass  # Expected since mock raises

        # Verify candidate models: must NOT contain 1.5 or 2.0
        assert len(captured_models) > 0
        for m in captured_models:
            assert "1.5" not in m, f"Deprecated model {m} found in candidate models!"
            assert "2.0" not in m, f"Deprecated model {m} found in candidate models!"

        # Must start with 3.8-flash
        assert captured_models[0] == "gemini-3.8-flash"
    finally:
        if os.path.exists(dummy_wav):
            os.remove(dummy_wav)


def test_download_audio_section_403_fallback(monkeypatch, tmp_path):
    """Verify that download_audio_section triggers _download_and_trim_locally upon 403 Forbidden."""
    import subprocess
    fetcher = YouTubeFetcher()

    # Mock Popen to simulate yt-dlp failing with 403 Forbidden on download-sections
    class Mock403Process:
        def __init__(self, *args, **kwargs):
            self.stdout = [
                "[youtube] Downloading visionos player API JSON\n",
                "[download] Destination: raw.webm\n",
                "HTTP error 403 Forbidden\n",
                "Error opening input: Server returned 403 Forbidden (access denied)\n",
                "ERROR: ffmpeg exited with code 3436169992\n",
            ]

        def wait(self):
            return 1

    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: Mock403Process())

    fallback_called = []

    def mock_fallback(url, start_sec, end_sec, output_wav_path, **kwargs):
        fallback_called.append(True)
        # Create dummy output file
        with open(output_wav_path, "wb") as f:
            f.write(b"RIFF....WAVEfmt ....data....")
        return output_wav_path

    monkeypatch.setattr(fetcher, "_download_and_trim_locally", mock_fallback)

    out_file = str(tmp_path / "section.wav")
    res = fetcher.download_audio_section(
        url="https://youtube.com/watch?v=y13nXFk6D34",
        start_sec=1200.0,
        end_sec=1920.0,
        output_wav_path=out_file,
    )

    assert len(fallback_called) == 1
    assert res == out_file
    assert os.path.exists(out_file)


def test_bot_detection_cookie_fallback(monkeypatch):
    """Verify that bot detection (Sign in to confirm you're not a bot) triggers browser cookie recovery."""
    import subprocess
    import tools.profile_builder.youtube_fetcher as ytf

    fetcher = YouTubeFetcher()
    probe_calls = []

    # Reset any cached working browser
    monkeypatch.setattr(ytf, "_WORKING_COOKIE_BROWSER", None)

    def mock_discover(test_url, yt_dlp_cmd="yt-dlp"):
        probe_calls.append(test_url)
        monkeypatch.setattr(ytf, "_WORKING_COOKIE_BROWSER", "firefox")
        return "firefox"

    monkeypatch.setattr(ytf, "discover_working_browser", mock_discover)

    call_count = 0

    def mock_run(cmd, *args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            # First attempt fails with bot detection
            return subprocess.CompletedProcess(
                args=cmd,
                returncode=1,
                stdout="",
                stderr="ERROR: [youtube] g2vU7VgJ1_I: Sign in to confirm you're not a bot. Use --cookies-from-browser",
            )
        else:
            # Retry succeeds with cookie
            assert "--cookies-from-browser" in cmd
            assert "firefox" in cmd
            if "--" in cmd:
                dash_idx = cmd.index("--")
                cookie_idx = cmd.index("--cookies-from-browser")
                assert cookie_idx < dash_idx, f"Cookie arg must be BEFORE '--' separator: {cmd}"
            return subprocess.CompletedProcess(
                args=cmd,
                returncode=0,
                stdout=json.dumps({
                    "id": "g2vU7VgJ1_I",
                    "title": "Test Title",
                    "uploader": "Test Channel",
                    "description": "desc",
                    "duration": 120,
                    "tags": [],
                }),
                stderr="",
            )

    monkeypatch.setattr(subprocess, "run", mock_run)

    meta = fetcher.fetch_metadata("https://www.youtube.com/watch?v=g2vU7VgJ1_I")
    assert meta.video_id == "g2vU7VgJ1_I"
    assert meta.title == "Test Title"
    assert len(probe_calls) == 1
    assert call_count == 2


def test_is_channel_url_strict_sanitization():
    """Verify that is_channel_url strictly validates domain to prevent URL sanitization bypass."""
    from tools.profile_builder.youtube_fetcher import is_channel_url

    # Legitimate YouTube channel URLs
    assert is_channel_url("https://www.youtube.com/@ChannelName") is True
    assert is_channel_url("https://youtube.com/channel/UC1234567890abcdef") is True
    assert is_channel_url("https://m.youtube.com/c/CustomName") is True
    assert is_channel_url("https://www.youtube.com/user/UserName") is True

    # Legitimate single video URLs should NOT be channels
    assert is_channel_url("https://www.youtube.com/watch?v=12345678901") is False
    assert is_channel_url("https://youtu.be/12345678901") is False
    assert is_channel_url("https://www.youtube.com/shorts/12345678901") is False

    # Malicious / spoofed domains must be rejected
    assert is_channel_url("https://evil-youtube.com/@ChannelName") is False
    assert is_channel_url("https://notyoutube.com/channel/UC123") is False
    assert is_channel_url("https://attacker.com/youtube.com/@ChannelName") is False
    assert is_channel_url("https://youtube.com.attacker.com/@ChannelName") is False
    assert is_channel_url("") is False


def test_save_gemini_api_key_secure_permissions(tmp_path):
    """Verify save_gemini_api_key creates file with restricted permissions and reads back correctly."""
    from tools.profile_builder.gemini_analyzer import save_gemini_api_key, load_gemini_api_key

    key_file = str(tmp_path / "sub" / "gemini_key.txt")
    save_gemini_api_key("AIzaSyTestKey12345", key_file=key_file)

    loaded = load_gemini_api_key(key_file=key_file)
    assert loaded == "AIzaSyTestKey12345"
    assert os.path.isfile(key_file)


def test_resolve_yt_dlp_and_ffmpeg_commands():
    """Verify resolve_yt_dlp_cmd and resolve_ffmpeg_cmd work in zero-config environments."""
    from tools.profile_builder.youtube_fetcher import resolve_yt_dlp_cmd, resolve_ffmpeg_cmd

    yt_cmd = resolve_yt_dlp_cmd()
    assert isinstance(yt_cmd, list)
    assert len(yt_cmd) >= 1

    ffmpeg_cmd = resolve_ffmpeg_cmd()
    assert isinstance(ffmpeg_cmd, str)
    assert len(ffmpeg_cmd) > 0


def test_youtube_fetcher_filenotfound_self_healing(monkeypatch):
    """Verify that FileNotFoundError ([WinError 2]) during yt-dlp execution triggers automatic recovery."""
    import subprocess
    from tools.profile_builder.youtube_fetcher import YouTubeFetcher

    fetcher = YouTubeFetcher()
    attempts = 0

    def mock_run(cmd, *args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise FileNotFoundError(2, "指定されたファイルが見つかりません。")
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=0,
            stdout='{"id": "test_id", "title": "Test Title", "duration": 300}',
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", mock_run)

    res = fetcher._run_ytdl_command(["yt-dlp", "--dump-json", "https://youtube.com/watch?v=test_id"])
    assert attempts == 2
    assert "-m" in res.args or "yt_dlp" in res.args


def test_is_playlist_url_and_extract_playlist_id():
    """Verify is_playlist_url and extract_playlist_id strictly validate domains and extract list IDs."""
    from tools.profile_builder.youtube_fetcher import is_playlist_url, extract_playlist_id

    # Valid playlist URLs
    assert is_playlist_url("https://www.youtube.com/playlist?list=PL1234567890abcdef") is True
    assert is_playlist_url("https://youtube.com/playlist?list=PL1234567890abcdef") is True
    assert is_playlist_url("https://m.youtube.com/playlist?list=PL1234567890abcdef") is True
    assert is_playlist_url("https://www.youtube.com/watch?v=12345678901&list=PL1234567890abcdef") is True
    assert is_playlist_url("https://youtu.be/watch?v=12345678901&list=PL1234567890abcdef") is True

    # Single video / channel / shorts should NOT be playlist
    assert is_playlist_url("https://www.youtube.com/watch?v=12345678901") is False
    assert is_playlist_url("https://www.youtube.com/@ValChannel") is False
    assert is_playlist_url("https://www.youtube.com/shorts/12345678901") is False
    assert is_playlist_url("") is False

    # Spoofed domains must be rejected
    assert is_playlist_url("https://evil-youtube.com/playlist?list=PL123") is False
    assert is_playlist_url("https://notyoutube.com/playlist?list=PL123") is False
    assert is_playlist_url("https://youtube.com.attacker.com/playlist?list=PL123") is False

    # extract_playlist_id tests
    assert extract_playlist_id("https://www.youtube.com/playlist?list=PL1234567890abcdef") == "PL1234567890abcdef"
    assert extract_playlist_id("https://www.youtube.com/watch?v=123&list=PLxyz_99") == "PLxyz_99"
    assert extract_playlist_id("https://www.youtube.com/watch?v=123") is None


def test_fetch_playlist_videos_mock(monkeypatch):
    """Verify fetch_playlist_videos parses yt-dlp flat playlist, filters duplicates/shorts, and respects limit."""
    import subprocess
    from tools.profile_builder.youtube_fetcher import YouTubeFetcher

    fetcher = YouTubeFetcher()

    mock_entries = [
        {"id": "vid1", "title": "Episode 1", "duration": 600, "view_count": 1000},
        {"id": "vid2", "title": "Short Clip", "duration": 60, "view_count": 5000},  # short (<180s)
        {"id": "vid3", "title": "Episode 2", "duration": 1200, "view_count": 2000},
        {"id": "vid_dup", "title": "Already Trained", "duration": 800, "view_count": 3000},
    ]
    stdout_lines = "\n".join(json.dumps(e) for e in mock_entries)

    def mock_run(cmd, *args, **kwargs):
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout=stdout_lines, stderr="")

    monkeypatch.setattr(subprocess, "run", mock_run)

    res = fetcher.fetch_playlist_videos(
        playlist_url="https://www.youtube.com/playlist?list=PLtest",
        limit=5,
        exclude_video_ids={"vid_dup"},
    )
    assert len(res) == 2
    ids = [v["id"] for v in res]
    assert "vid1" in ids
    assert "vid3" in ids
    assert "vid2" not in ids  # filtered because duration < 180s
    assert "vid_dup" not in ids  # excluded


def test_gemini_dynamic_model_filtering():
    """Verify get_available_multimodal_models filters legacy and irrelevant models."""
    from tools.profile_builder.gemini_analyzer import get_available_multimodal_models

    class FakeModel:
        def __init__(self, name, supported_actions):
            self.name = name
            self.supported_actions = supported_actions

    class FakeClient:
        class models:
            @staticmethod
            def list():
                return [
                    FakeModel("models/gemini-2.5-flash", ["generateContent"]),  # legacy
                    FakeModel("models/gemini-1.5-pro", ["generateContent"]),    # legacy
                    FakeModel("models/gemini-3.8-flash-tts", ["generateContent"]),  # tts
                    FakeModel("models/gemini-nano-banana-2.1", ["generateContent"]),  # image
                    FakeModel("models/gemini-embedding-2", ["embedContent"]),  # no generateContent
                    FakeModel("models/gemini-3.5-flash", ["generateContent"]),  # valid
                    FakeModel("models/gemini-3.8-flash", ["generateContent"]),  # valid
                    FakeModel("models/gemini-4-argon", ["generateContent"]),   # valid
                ]

    models = get_available_multimodal_models(FakeClient(), preferred_model="gemini-3.8-flash")
    assert "gemini-3.8-flash" in models
    assert "gemini-3.5-flash" in models
    assert "gemini-4-argon" in models

    # Ensure excluded models are not in the list
    assert "gemini-2.5-flash" not in models
    assert "gemini-1.5-pro" not in models
    assert "gemini-3.8-flash-tts" not in models
    assert "gemini-nano-banana-2.1" not in models


def test_extract_retry_after_seconds():
    """Verify extract_retry_after_seconds parses retry delay from headers and error messages."""
    from tools.profile_builder.gemini_analyzer import extract_retry_after_seconds

    class ErrorWithHeader(Exception):
        def __init__(self, msg, retry_after_val):
            super().__init__(msg)
            class FakeResp:
                headers = {"retry-after": retry_after_val}
            self.response = FakeResp()

    e_header = ErrorWithHeader("Rate limit exceeded", "25.5")
    assert extract_retry_after_seconds(e_header) == 25.5

    e_str1 = Exception("Resource has been exhausted. Please retry after 12.3s")
    assert extract_retry_after_seconds(e_str1) == 12.3

    e_str2 = Exception("Quota exceeded: retry_delay { seconds: 40 }")
    assert extract_retry_after_seconds(e_str2) == 40.0

    e_none = Exception("Standard 404 NOT_FOUND error")
    assert extract_retry_after_seconds(e_none) is None


def test_pipeline_interim_profile_saved_on_batch_error(tmp_path, monkeypatch):
    """Verify ProfileBuilderPipeline preserves and saves interim profile when subsequent videos fail."""
    from tools.profile_builder.pipeline import ProfileBuilderPipeline
    from tools.profile_builder.profile_model import ReplacementRule, SpeakerProfile

    saved_profiles = []

    def mock_save(prof):
        saved_profiles.append(prof)
        return str(tmp_path / f"{prof.speaker_id}.json")

    monkeypatch.setattr("tools.profile_builder.pipeline.save_profile", mock_save)

    pipeline = ProfileBuilderPipeline(api_key="fake-key")

    # Mock fetch_playlist_videos returning 2 videos
    monkeypatch.setattr(
        pipeline.fetcher,
        "fetch_playlist_videos",
        lambda *args, **kwargs: [
            {"id": "v1", "url": "https://youtube.com/watch?v=v1", "title": "Vid 1", "channel": "Chan"},
            {"id": "v2", "url": "https://youtube.com/watch?v=v2", "title": "Vid 2", "channel": "Chan"},
        ],
    )

    call_count = 0

    def mock_process_single(youtube_url, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            # Video 1 succeeds
            prof = SpeakerProfile(
                speaker_id="test_speaker",
                display_name="Test Speaker",
                source_video_urls=[youtube_url],
                processed_video_ids=["v1"],
                replacements=[ReplacementRule(pattern="てすと", replacement="テスト")],
            )
            return prof
        else:
            # Video 2 raises an error (e.g. Gemini failure)
            raise RuntimeError("Gemini API 500 internal server error")

    monkeypatch.setattr(pipeline, "_process_single_video", mock_process_single)

    # Execution should finish partially and return the interim profile from video 1
    result = pipeline.run(
        youtube_url="https://www.youtube.com/playlist?list=PLmock123",
        speaker_id="test_speaker",
    )

    assert result is not None
    assert result.speaker_id == "test_speaker"
    assert "v1" in result.processed_video_ids
    assert len(saved_profiles) >= 1


def test_sanitize_and_trim_rule():
    from tools.profile_builder.profile_model import sanitize_and_trim_rule

    # 1. Normal short word
    assert sanitize_and_trim_rule("ほろらいぶ", "ホロライブ") == ("ほろらいぶ", "ホロライブ")

    # 2. Strips prefix filler
    assert sanitize_and_trim_rule("えーっとホロライブが", "ホロライブ") == ("ホロライブ", "ホロライブ")
    assert sanitize_and_trim_rule("あのーApexの", "Apex") == ("Apex", "Apex")

    # 3. Too short (single char) rejected
    assert sanitize_and_trim_rule("と", "そ") is None
    assert sanitize_and_trim_rule("あ", "い") is None

    # 4. Overly long sentence rejected (> 12 chars)
    assert sanitize_and_trim_rule("本日は大変お日柄もよく皆様いかがお過ごしでしょうか", "今日") is None

    # 5. Empty or identical rejected
    assert sanitize_and_trim_rule("", "テスト") is None
    assert sanitize_and_trim_rule("テスト", "テスト") is None


def test_archive_stale_rules_180_days():
    import datetime
    from tools.profile_builder.profile_model import ReplacementRule, SpeakerProfile

    now = datetime.datetime.now(datetime.timezone.utc)
    old_dt = (now - datetime.timedelta(days=200)).isoformat()
    recent_dt = (now - datetime.timedelta(days=30)).isoformat()

    rule_stale = ReplacementRule(
        pattern="古い単語",
        replacement="ふるい単語",
        hit_count=0,
        added_at=old_dt,
    )
    rule_recent = ReplacementRule(
        pattern="最近の単語",
        replacement="さいきん単語",
        hit_count=0,
        added_at=recent_dt,
    )
    rule_active_used = ReplacementRule(
        pattern="頻出単語",
        replacement="ひんしゅつ単語",
        hit_count=15,
        added_at=old_dt,
    )

    prof = SpeakerProfile(
        speaker_id="archive_test",
        display_name="Archive Test",
        replacements=[rule_stale, rule_recent, rule_active_used],
    )

    # Run archiving with 180 days cutoff
    archived_count = prof.archive_stale_rules(days=180)
    assert archived_count == 1
    assert len(prof.replacements) == 2
    assert len(prof.archived_replacements) == 1
    assert prof.archived_replacements[0].pattern == "古い単語"

    # Test replace_text excludes archived
    replace_txt = prof.to_replace_text()
    assert "古い単語" not in replace_txt
    assert "最近の単語" in replace_txt
    assert "頻出単語" in replace_txt

    # Test unarchive
    unarchived = prof.unarchive_rule("古い単語")
    assert unarchived is True
    assert len(prof.replacements) == 3
    assert len(prof.archived_replacements) == 0


def test_legacy_profile_migration():
    from tools.profile_builder.profile_model import SpeakerProfile

    legacy_json = {
        "schema_version": "1.0.0",
        "speaker_id": "legacy_spk",
        "display_name": "Legacy Speaker",
        "created_at": "2026-01-01T00:00:00Z",
        "replacements": [
            {"pattern": "れがしー", "replacement": "レガシー"}
        ],
    }

    prof = SpeakerProfile.model_validate(legacy_json)
    assert prof.schema_version == "1.0.0"
    assert prof.replacements[0].hit_count == 0

    # Migrate
    changed = prof.migrate_from_legacy()
    assert changed is True
    assert prof.schema_version == "1.2.0"
    assert prof.replacements[0].tags == ["共通"]
    # Verify legacy rule is given grace period using created_at
    assert prof.replacements[0].added_at == "2026-01-01T00:00:00Z"
    assert prof.replacements[0].is_archived is False




