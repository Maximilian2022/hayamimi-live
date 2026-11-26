"""cloud/session_manager.py と cloud/gateway.py のセキュリティ回帰テスト (CodeQL Alert #32-#44 対応)."""

import asyncio
import os
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from cloud import session_manager as sm  # noqa: E402
from tests.test_cloud_stream_and_translate import _load_gateway_module  # noqa: E402


# ---------------------------------------------------------------------------
# Alert #32-#38, #41-#44: Path Injection 防御テスト
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_uid",
    [
        "",
        ".",
        "..",
        "../etc",
        "..\\windows",
        "user/../admin",
        "user\\..\\admin",
        "/abs/path",
        "C:\\Windows\\System32",
        "user\x00null",
        "user with space",
        "a" * 65,
    ],
)
def test_session_manager_rejects_traversal_user_ids(tmp_path, monkeypatch, bad_uid):
    monkeypatch.setattr(sm, "DATA_DIR", str(tmp_path))
    mgr = sm.SessionManager()

    assert not sm.is_safe_user_id(bad_uid)
    with pytest.raises(ValueError):
        sm.UserSession(bad_uid)
    with pytest.raises(ValueError):
        mgr.get_or_create_user(bad_uid)


def test_user_session_file_operations_confined_to_user_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(sm, "DATA_DIR", str(tmp_path))
    mgr = sm.SessionManager()

    sess = mgr.get_or_create_user("310034034690359308", username="tester")
    assert sess.user_id == "310034034690359308"
    assert sess.obs_token.startswith("obs_sec_")

    # 許可外ファイル名やトラバーサルは拒否されること
    with pytest.raises(ValueError):
        sess._file_path("../secret.txt")
    with pytest.raises(ValueError):
        sess._file_path("arbitrary.py")

    # 置換辞書・スタイル・設定の正常読み書き
    sess.save_replacements_raw("誤認識 => 正解\n")
    assert sess.apply_replacements("これは誤認識です") == "これは正解です"
    assert "誤認識 => 正解" in sess.get_replacements_raw()

    saved_style = sess.save_style({"size": 48, "color": "#ff0000"})
    assert saved_style["size"] == 48

    saved_settings = sess.save_settings({"asr_model": "reazon", "enable_trans": True})
    assert saved_settings["asr_model"] == "reazon"
    assert saved_settings["enable_trans"] is True

    # ディスクからの再読込と OBS トークン逆引き (不正名ディレクトリは無視されること)
    bad_dir = tmp_path / "users" / "..bad..dir"
    bad_dir.mkdir(parents=True, exist_ok=True)
    (bad_dir / "token.txt").write_text("obs_sec_fake_token_1234567890", encoding="utf-8")

    mgr2 = sm.SessionManager()
    assert mgr2.get_user_by_obs_token("obs_sec_fake_token_1234567890") is None
    found = mgr2.get_user_by_obs_token(sess.obs_token)
    assert found is not None
    assert found.user_id == "310034034690359308"


# ---------------------------------------------------------------------------
# Alert #39: Gemini API キー暗号化保存・後方互換マイグレーションテスト
# ---------------------------------------------------------------------------


def test_gemini_key_encrypted_at_rest_and_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(sm, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(sm, "SESSION_SECRET_KEY", "unit-test-secret-key-2026")

    sess = sm.UserSession("user_enc_01")
    secret_key = "AIzaSyTestSecretKey_DoNotStorePlainText_12345"
    sess.save_gemini_key(secret_key)

    key_file = Path(sess.user_dir) / "gemini_key.txt"
    assert key_file.is_file()
    on_disk = key_file.read_text(encoding="utf-8")

    # ディスク上に平文が含まれず FERNET: 暗号化形式であること
    assert secret_key not in on_disk
    assert on_disk.startswith("FERNET:")

    # 正しく復号できること
    assert sess.get_gemini_key() == secret_key

    # 空文字保存で削除されること
    sess.save_gemini_key("")
    assert not key_file.exists()
    assert sess.get_gemini_key() == ""


def test_gemini_key_legacy_plaintext_auto_migration(tmp_path, monkeypatch):
    monkeypatch.setattr(sm, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(sm, "SESSION_SECRET_KEY", "unit-test-secret-key-2026")

    sess = sm.UserSession("user_legacy_01")
    legacy_key = "AIzaSyLegacyPlaintextKey_987654321"
    key_file = Path(sess.user_dir) / "gemini_key.txt"
    key_file.write_text(legacy_key, encoding="utf-8")

    # 初回取得で平文が読め、同時に自動で FERNET: へ移行されること
    assert sess.get_gemini_key() == legacy_key
    migrated = key_file.read_text(encoding="utf-8")
    assert legacy_key not in migrated
    assert migrated.startswith("FERNET:")
    assert sess.get_gemini_key() == legacy_key


# ---------------------------------------------------------------------------
# Alert #40: Stack Trace / Exception 非露出テスト
# ---------------------------------------------------------------------------


def test_gemini_key_test_endpoint_never_exposes_exception_details(tmp_path, monkeypatch):
    gw = _load_gateway_module(monkeypatch)
    monkeypatch.setattr(sm, "DATA_DIR", str(tmp_path))

    mgr = sm.SessionManager()
    u_sess = mgr.get_or_create_user("310034034690359308")
    u_sess.save_gemini_key("AIzaSyDummyKeyForTest")

    monkeypatch.setattr(gw, "session_manager", mgr)
    monkeypatch.setattr(gw, "get_current_user", lambda req: {"id": "310034034690359308"})

    # 内部パスやシークレットを含む例外を発生させるスタブ
    def _make_failing_genai(exc_to_raise: Exception):
        fake_google = types.ModuleType("google")
        fake_genai = types.ModuleType("google.genai")

        class _FakeModels:
            def generate_content(self, **kwargs):
                raise exc_to_raise

        class _FakeClient:
            def __init__(self, **kwargs):
                self.models = _FakeModels()

        fake_genai.Client = _FakeClient
        fake_google.genai = fake_genai
        return fake_google, fake_genai

    # 1) 一般内部エラー (内部ファイルパスやトレース文字列を含む)
    sensitive_exc = RuntimeError("Internal crash at C:\\secret\\server\\key_vault.py:99 secret_token=xyz987")
    _, fake_genai = _make_failing_genai(sensitive_exc)
    monkeypatch.setitem(sys.modules, "google.genai", fake_genai)
    import google
    monkeypatch.setattr(google, "genai", fake_genai, raising=False)

    res1 = asyncio.run(gw.test_gemini_key(MagicMock()))
    assert res1["ok"] is False
    assert "key_vault.py" not in res1["error"]
    assert "xyz987" not in res1["error"]
    assert "Internal crash" not in res1["error"]
    assert "接続テスト失敗" in res1["error"]

    # 2) 認証エラー (403 API_KEY_INVALID)
    auth_exc = Exception("403 API_KEY_INVALID: raw_key=AIzaSyDummyKeyForTest")
    _, fake_genai2 = _make_failing_genai(auth_exc)
    monkeypatch.setitem(sys.modules, "google.genai", fake_genai2)
    monkeypatch.setattr(google, "genai", fake_genai2, raising=False)

    res2 = asyncio.run(gw.test_gemini_key(MagicMock()))
    assert res2["ok"] is False
    assert "AIzaSyDummyKeyForTest" not in res2["error"]
    assert "API キーが無効" in res2["error"]

    # 3) クォータエラー (429 RESOURCE_EXHAUSTED)
    quota_exc = Exception("429 RESOURCE_EXHAUSTED: project_id=internal-proj-123")
    _, fake_genai3 = _make_failing_genai(quota_exc)
    monkeypatch.setitem(sys.modules, "google.genai", fake_genai3)
    monkeypatch.setattr(google, "genai", fake_genai3, raising=False)

    res3 = asyncio.run(gw.test_gemini_key(MagicMock()))
    assert res3["ok"] is False
    assert "internal-proj-123" not in res3["error"]
    assert "クォータ" in res3["error"]
