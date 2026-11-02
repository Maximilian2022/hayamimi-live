# -*- coding: utf-8 -*-
import json
import os
import shutil
import tempfile
import pytest

from scripts.config_store import (
    atomic_save_json,
    load_json_with_backup,
    get_saved_obs_config,
    save_obs_config,
    get_saved_asr_config,
    save_asr_config,
    get_saved_active_speaker_id,
    save_active_speaker_id,
    get_storage_dir,
    reset_storage_dir_cache,
    create_config_snapshot_backup,
    extract_initial_prompt_from_replace_file,
)


@pytest.fixture
def tmp_config_dir(monkeypatch):
    td = tempfile.mkdtemp(prefix="test_cfg_store_")
    monkeypatch.setenv("HAYAMIMI_CONFIG_DIR", td)
    reset_storage_dir_cache()
    yield td
    reset_storage_dir_cache()
    shutil.rmtree(td, ignore_errors=True)


def test_atomic_save_and_backup(tmp_config_dir):
    target = os.path.join(tmp_config_dir, "test.json")
    data1 = {"version": 1, "msg": "hello"}
    atomic_save_json(target, data1)

    assert os.path.isfile(target)
    assert not os.path.isfile(target + ".bak")  # 初回は .bak なし
    assert load_json_with_backup(target) == data1

    # 2回目保存: .bak が生成される
    data2 = {"version": 2, "msg": "world"}
    atomic_save_json(target, data2)

    assert os.path.isfile(target + ".bak")
    assert load_json_with_backup(target) == data2
    with open(target + ".bak", "r", encoding="utf-8") as f:
        bak_content = json.load(f)
    assert bak_content == data1


def test_auto_heal_from_corrupted_file(tmp_config_dir):
    target = os.path.join(tmp_config_dir, "corrupt_test.json")
    good_data = {"key": "valid_value", "int": 42}
    atomic_save_json(target, good_data)
    atomic_save_json(target, {"key": "second_valid"})

    # メインファイルを0バイトに破損させる（急な電源断・クラッシュを模倣）
    with open(target, "w", encoding="utf-8") as f:
        f.write("")

    # ロード時に .bak から自動修復されるか
    recovered = load_json_with_backup(target)
    assert recovered == good_data

    # 修復によりメインファイルが再度正常化されているか
    assert os.path.getsize(target) > 0
    with open(target, "r", encoding="utf-8") as f:
        assert json.load(f) == good_data


def test_obs_config_persistence(tmp_config_dir):
    # 初期値
    default_obs = get_saved_obs_config()
    assert default_obs["url"] == "ws://127.0.0.1:4455"
    assert default_obs["enabled"] is True
    assert default_obs["password"] == ""

    # 一部更新
    saved = save_obs_config({"password": "secret_pwd", "target_source": "マイク (Realtek)"})
    assert saved["password"] == "secret_pwd"
    assert saved["target_source"] == "マイク (Realtek)"
    assert saved["url"] == "ws://127.0.0.1:4455"  # 既存値維持

    # 再読み込み
    reloaded = get_saved_obs_config()
    assert reloaded["password"] == "secret_pwd"
    assert reloaded["target_source"] == "マイク (Realtek)"


def test_asr_and_active_speaker_persistence(tmp_config_dir):
    assert get_saved_active_speaker_id() is None

    save_asr_config({"lang": "ja", "translate": ["en"], "punctuate": True})
    cfg = get_saved_asr_config()
    assert cfg["lang"] == "ja"
    assert cfg["translate"] == ["en"]
    assert cfg["punctuate"] is True

    # 話者プロファイル永続化
    save_active_speaker_id("speaker_test_123")
    assert get_saved_active_speaker_id() == "speaker_test_123"

    # ASR設定の他の項目を更新しても active_speaker_id が保持される
    save_asr_config({"punctuate": False})
    assert get_saved_active_speaker_id() == "speaker_test_123"
    cfg_updated = get_saved_asr_config()
    assert cfg_updated["punctuate"] is False
    assert cfg_updated["lang"] == "ja"

    # 解除
    save_active_speaker_id(None)
    assert get_saved_active_speaker_id() is None


def test_config_snapshot_backup(tmp_config_dir):
    save_obs_config({"password": "snap_pwd"})
    save_asr_config({"lang": "ko"})

    backup_dir = create_config_snapshot_backup()
    assert backup_dir is not None
    assert os.path.isdir(backup_dir)
    assert os.path.isfile(os.path.join(backup_dir, "obs_config.json"))
    assert os.path.isfile(os.path.join(backup_dir, "asr_config.json"))

    with open(os.path.join(backup_dir, "obs_config.json"), "r", encoding="utf-8") as f:
        bak_obs = json.load(f)
    assert bak_obs["password"] == "snap_pwd"


def test_extract_initial_prompt_from_replace_file(tmp_path):
    replace_file = tmp_path / "test_replace.txt"
    content = """# 置換辞書
アマンガス\tAmongUs
モット\tMOD
GTハブ=GitHub
ウインドズ=Windows
"""
    replace_file.write_text(content, encoding="utf-8")

    prompt = extract_initial_prompt_from_replace_file(str(replace_file))
    assert "AmongUs" in prompt
    assert "MOD" in prompt
    assert "GitHub" in prompt
    assert "Windows" in prompt


