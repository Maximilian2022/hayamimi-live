"""
Tests for Priority Tags (ASR Hotwords replacement filtering) and SudachiPy morphological correction.
"""
import os
import sys

# Ensure scripts and tools can be imported
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts")))

import json
import shutil
import tempfile
import unittest

from tools.profile_builder.profile_model import (
    SpeakerProfile,
    ReplacementRule,
    CURRENT_SCHEMA_VERSION,
)
from tools.profile_builder.youtube_fetcher import extract_hashtags, extract_hashtags_from_metadata, VideoMetadata
from tools.profile_builder.profile_exporter import save_profile, load_profile


class TestHashtagExtraction(unittest.TestCase):
    def test_extract_basic_hashtags(self):
        title = "【#ApexLegends】ランクマいくぞ！ソロダイヤ目指す配信 #ストグラ #VTuber"
        tags = extract_hashtags(title)
        self.assertIn("#ApexLegends", tags)
        self.assertIn("#ストグラ", tags)
        self.assertIn("#VTuber", tags)
        self.assertEqual(len(tags), 3)

    def test_extract_fullwidth_hashtags(self):
        title = "雑談枠 ＃スト6 初心者マスターへの道！"
        tags = extract_hashtags(title)
        self.assertEqual(tags, ["#スト6"])

    def test_no_hashtags(self):
        title = "マインクラフトで巨大建築を作る配信 パート12"
        tags = extract_hashtags(title)
        self.assertEqual(tags, [])

    def test_deduplication(self):
        title = "#Apex #Apex #apex"
        tags = extract_hashtags(title)
        # Case-insensitive deduplication preserves first occurrence
        self.assertEqual(tags, ["#Apex"])

    def test_extract_hashtags_from_metadata_title_and_description(self):
        meta = VideoMetadata(
            video_id="test1234",
            title="【#Apex】ランクマ配信！ソロダイヤいくぞ",
            channel="TestChannel",
            description="ご視聴ありがとうございます！\nチャンネル登録よろしく！\n#ApexLegends #ストグラ #ゲーム実況",
            duration_sec=3600.0,
            tags=["Apex", "FPS"]
        )
        tags = extract_hashtags_from_metadata(meta)
        self.assertIn("#Apex", tags)
        self.assertIn("#ApexLegends", tags)
        self.assertIn("#ストグラ", tags)
        self.assertIn("#ゲーム実況", tags)
        self.assertEqual(len(tags), 4)


class TestProfileModelPriorityTags(unittest.TestCase):
    def test_schema_version_and_tags_default(self):
        prof = SpeakerProfile(speaker_id="test_spk", display_name="Test Speaker")
        self.assertEqual(prof.schema_version, CURRENT_SCHEMA_VERSION)
        rule = ReplacementRule(pattern="ウル", replacement="ウルト")
        self.assertEqual(rule.tags, [])

    def test_get_available_tags(self):
        rules = [
            ReplacementRule(pattern="ウル", replacement="ウルト", tags=["#ApexLegends", "共通"]),
            ReplacementRule(pattern="パリィ", replacement="ジャストパリィ", tags=["#スト6"]),
            ReplacementRule(pattern="あさひ", replacement="朝日", tags=[]),
        ]
        prof = SpeakerProfile(speaker_id="test_spk", display_name="Test Speaker", replacements=rules)
        available = prof.get_available_tags()
        self.assertEqual(available[0], "共通")
        self.assertIn("#ApexLegends", available)
        self.assertIn("#スト6", available)

    def test_get_active_replacements_filtering(self):
        rules = [
            ReplacementRule(pattern="ウル", replacement="ウルト", tags=["#ApexLegends"]),
            ReplacementRule(pattern="パリィ", replacement="ジャストパリィ", tags=["#スト6"]),
            ReplacementRule(pattern="あいさつ", replacement="こんちはー", tags=["共通"]),
            ReplacementRule(pattern="セル", replacement="シールドセル", tags=["#ApexLegends", "共通"]),
        ]
        prof = SpeakerProfile(speaker_id="test_spk", display_name="Test Speaker", replacements=rules)

        # 1. active_tags が None または空リスト: 全ルール適用
        all_rep = prof.get_active_replacements(None)
        self.assertEqual(len(all_rep), 4)

        # 2. active_tags = ["#ApexLegends"]: Apex用 + 共通のみ
        apex_rep = prof.get_active_replacements(["#ApexLegends"])
        self.assertIn("ウル", apex_rep)
        self.assertIn("セル", apex_rep)
        self.assertIn("あいさつ", apex_rep)
        self.assertNotIn("パリィ", apex_rep)  # スト6専用は除外
        self.assertEqual(len(apex_rep), 3)

        # 3. active_tags = ["共通"]: 共通タグ付きのみ
        common_rep = prof.get_active_replacements(["共通"])
        self.assertIn("あいさつ", common_rep)
        self.assertIn("セル", common_rep)
        self.assertNotIn("ウル", common_rep)
        self.assertNotIn("パリィ", common_rep)

    def test_merge_profile_tags_growth(self):
        # 既存プロファイルに Apex のルールがある
        base_rules = [
            ReplacementRule(pattern="ウル", replacement="ウルト", tags=["#ApexLegends"]),
            ReplacementRule(pattern="セル", replacement="シールドセル", tags=["#ApexLegends"]),
        ]
        base_prof = SpeakerProfile(speaker_id="streamer_a", display_name="Streamer A", replacements=base_rules)

        # 新しい動画（オーバーウォッチなど）で「ウル -> ウルト」が再検出された
        new_rules = [
            ReplacementRule(pattern="ウル", replacement="ウルト", tags=["#OW2", "共通"]),
            ReplacementRule(pattern="ナノ", replacement="ナノブースト", tags=["#OW2"]),
        ]
        new_prof = SpeakerProfile(speaker_id="streamer_a", display_name="Streamer A", replacements=new_rules)

        # マージ実行
        base_prof.merge_profile(new_prof)

        # 同一ワードの tags がマージ・重複排除されているか検証
        ult_rule = next(r for r in base_prof.replacements if r.pattern == "ウル")
        self.assertIn("#ApexLegends", ult_rule.tags)
        self.assertIn("#OW2", ult_rule.tags)
        self.assertIn("共通", ult_rule.tags)

        # 新規ワードが追加されているか
        nano_rule = next(r for r in base_prof.replacements if r.pattern == "ナノ")
        self.assertEqual(nano_rule.tags, ["#OW2"])

    def test_legacy_migration(self):
        # 旧形式（schema_version 1.1.0、tagsフィールドなし）
        legacy_data = {
            "speaker_id": "legacy_spk",
            "display_name": "Legacy",
            "schema_version": "1.1.0",
            "replacements": [
                {"pattern": "てす", "replacement": "テスト"}
            ],
            "archived_replacements": [
                {"pattern": "ふるい", "replacement": "古い"}
            ]
        }
        prof = SpeakerProfile.model_validate(legacy_data)
        prof.migrate_from_legacy()
        self.assertEqual(prof.schema_version, CURRENT_SCHEMA_VERSION)
        self.assertEqual(prof.replacements[0].tags, ["共通"])
        self.assertEqual(prof.archived_replacements[0].tags, ["共通"])


class TestSudachiPyMorphologicalCorrection(unittest.TestCase):
    def test_sudachipy_integration_in_asr_engine(self):
        from scripts.asr_engine import RoutedASR

        # AsrEngine インスタンス（モデルロードなしで _replace のみテスト）
        class MockArgs:
            device = "cpu"
            model = "test"
            beam = 1
            punct = "none"
            punct_model = "none"
            replace = ""
            profile = ""
            hotwords = ""

        import threading
        engine = RoutedASR.__new__(RoutedASR)
        engine._rule_hit_counts = {}
        engine._rule_last_hit = {}
        engine._stats_lock = threading.Lock()
        engine.set_replacements({"ウルト": "アルティメット", "ランクマ": "ランクマッチ"})

        # 直接置換
        result1 = engine._replace("ウルトを発動した")
        self.assertEqual(result1, "アルティメットを発動した")

        # 複数置換
        result2 = engine._replace("今日のランクマでウルト使った")
        self.assertEqual(result2, "今日のランクマッチでアルティメット使った")


class TestProfileApiPriorityTags(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.orig_profiles_dir = os.environ.get("HAYAMIMI_PROFILES_DIR")
        os.environ["HAYAMIMI_PROFILES_DIR"] = self.temp_dir

        # テストプロファイルの作成
        self.prof = SpeakerProfile(
            speaker_id="api_test_spk",
            display_name="API Test Speaker",
            schema_version="1.2.0",
            replacements=[
                ReplacementRule(pattern="ウル", replacement="ウルト", tags=["#Apex"]),
                ReplacementRule(pattern="あいさつ", replacement="こんにちは", tags=["共通"]),
                ReplacementRule(pattern="パリィ", replacement="ジャストパリィ", tags=["#スト6"]),
            ]
        )
        save_profile(self.prof, self.temp_dir)

    def tearDown(self):
        if self.orig_profiles_dir:
            os.environ["HAYAMIMI_PROFILES_DIR"] = self.orig_profiles_dir
        else:
            os.environ.pop("HAYAMIMI_PROFILES_DIR", None)
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_profile_filtering_for_asr(self):
        # サーバーのプロファイル読み込みとタグフィルタリングの動作確認
        loaded = load_profile("api_test_spk", self.temp_dir)
        self.assertIsNotNone(loaded)

        apex_active = loaded.get_active_replacements(["#Apex"])
        self.assertIn("ウル", apex_active)
        self.assertIn("あいさつ", apex_active)
        self.assertNotIn("パリィ", apex_active)

        sf6_active = loaded.get_active_replacements(["#スト6"])
        self.assertNotIn("ウル", sf6_active)
        self.assertIn("あいさつ", sf6_active)
        self.assertIn("パリィ", sf6_active)


if __name__ == "__main__":
    unittest.main()
