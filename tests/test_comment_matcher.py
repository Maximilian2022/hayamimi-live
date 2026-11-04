"""コメント連動の補正 (scripts/comment_matcher.py) の単体テスト"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from scripts import comment_matcher as cm  # noqa: E402
from scripts.comment_sync import (  # noqa: E402
    CommentSynchronizer,
    clean_author_name,
    parse_youtube_live_chat_names,
)


def _moras(a):
    return cm.to_moras(a)


def test_moras_keep_small_kana_together():
    assert _moras("きゃらくたー") == ["キャ", "ラ", "ク", "タ", "ー"]


@pytest.mark.parametrize("a, b, expected", [
    ("バイパー", "バイバー", 0.4),
    ("ブラーキウム", "ブラキウム", 0.4),
    ("バイパー", "バイパー", 0.0),
    ("マイキー", "マイケー", 1.0),
])
def test_phonetic_distance(a, b, expected):
    assert cm.phonetic_distance(_moras(a), _moras(b)) == pytest.approx(expected)


def test_short_terms_allow_only_light_differences():
    assert cm.allowed_distance(2) < 0
    assert cm.allowed_distance(3) == cm.MAX_DISTANCE_SHORT
    assert cm.allowed_distance(5) == cm.MAX_DISTANCE_LONG


class TestCorrect:
    @pytest.fixture(autouse=True)
    def _sudachi(self):
        pytest.importorskip("sudachipy")

    @staticmethod
    def _corrector(*terms):
        c = cm.CommentCorrector()
        c.set_terms(cm.CommentTerm(s, r) for s, r in terms)
        return c

    def test_fixes_a_similar_sounding_comment_word(self):
        c = self._corrector(("バイパー", "バイパー"))
        assert c.correct("今バイバーすごい人気じゃないですか") == "今バイパーすごい人気じゃないですか"

    def test_fixes_ascii_case(self):
        assert self._corrector(("RvR", "")).correct("ローカルとRVR") == "ローカルとRvR"

    @pytest.mark.parametrize("text", ["見事な試合だった", "クレバーな動きだった", "今日は勉強したいです"])
    def test_common_words_are_left_alone(self, text):
        c = self._corrector(("ミコト", "ミコト"), ("クレーバー", "クレーバー"), ("ベンキョウ", "ベンキョウ"))
        assert c.correct(text) == text

    def test_nothing_changes_without_terms(self):
        assert cm.CommentCorrector().correct("バイバー") == "バイバー"

    def test_unrelated_words_are_left_alone(self):
        assert self._corrector(("バイパー", "バイパー")).correct("サーバーが落ちた") == "サーバーが落ちた"


@pytest.mark.parametrize("raw, expected", [
    ("@mikey-1234", "mikey"),
    ("マイキー", "マイキー"),
    ("マイキーさん", "マイキー"),
    ("田中 太郎", "田中"),
    ("【配信】", ""),
    ("a", ""),
    (None, ""),
])
def test_clean_author_name(raw, expected):
    assert clean_author_name(raw) == expected


def test_commenter_name_becomes_an_active_term():
    pytest.importorskip("sudachipy")
    sync = CommentSynchronizer(enabled=True)
    sync.ingest_comments(["こんばんは"], "manual", names=["@マイキー"])
    assert "マイキー" in [t.surface for t in sync.get_active_terms()]


def test_disabled_sync_supplies_no_terms():
    sync = CommentSynchronizer(enabled=False)
    sync.ingest_comments(["クレーバー"])
    assert sync.get_active_terms() == []


def test_parse_youtube_author_names():
    data = {"continuationContents": {"liveChatContinuation": {"actions": [
        {"addChatItemAction": {"item": {"liveChatTextMessageRenderer": {"authorName": {"simpleText": "@viewer"}}}}},
        {"addChatItemAction": {"item": {"other": {}}}},
    ]}}}
    assert parse_youtube_live_chat_names(data) == ["@viewer"]
