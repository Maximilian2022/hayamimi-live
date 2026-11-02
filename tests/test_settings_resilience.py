# -*- coding: utf-8 -*-
import json
import os
import shutil
import tempfile
import pytest

from scripts.config_store import (
    reset_storage_dir_cache,
    get_saved_obs_config,
    save_obs_config,
    get_saved_asr_config,
    save_asr_config,
    get_saved_active_speaker_id,
    save_active_speaker_id,
    create_config_snapshot_backup,
)
from scripts.audio_dsp import get_global_dsp, save_global_dsp_config
from scripts.comment_sync import get_global_comment_sync, save_global_comment_sync_config


@pytest.fixture
def clean_config_env(monkeypatch):
    td = tempfile.mkdtemp(prefix="test_settings_resilience_")
    monkeypatch.setenv("HAYAMIMI_CONFIG_DIR", td)
    reset_storage_dir_cache()
    yield td
    reset_storage_dir_cache()
    shutil.rmtree(td, ignore_errors=True)


def test_obs_config_api_and_persistence(clean_config_env):
    obs_initial = get_saved_obs_config()
    assert obs_initial["url"] == "ws://127.0.0.1:4455"
    assert obs_initial["enabled"] is True

    # ユーザーがOBS設定を変更・保存
    saved = save_obs_config({
        "url": "ws://192.168.1.100:4455",
        "password": "mypassword123",
        "target_source": "マイク (USB Audio)",
        "enabled": True,
    })
    assert saved["url"] == "ws://192.168.1.100:4455"
    assert saved["password"] == "mypassword123"

    # 再読み込みで同一設定が復元される
    reloaded = get_saved_obs_config()
    assert reloaded["url"] == "ws://192.168.1.100:4455"
    assert reloaded["password"] == "mypassword123"
    assert reloaded["target_source"] == "マイク (USB Audio)"

    # .bak ファイルが存在すること
    obs_file = os.path.join(clean_config_env, "obs_config.json")
    save_obs_config({"password": "new_password"})
    assert os.path.isfile(obs_file + ".bak")


def test_audio_dsp_and_comment_sync_persistence(clean_config_env):
    dsp_res = save_global_dsp_config({"noise_suppress_enabled": False, "agc_compressor_enabled": True})
    assert dsp_res["noise_suppress_enabled"] is False

    comment_res = save_global_comment_sync_config({
        "enabled": True,
        "youtube_live_enabled": True,
        "youtube_channel_url": "https://youtube.com/@test",
        "word_ttl_sec": 300.0,
    })
    assert comment_res["youtube_live_enabled"] is True
    assert comment_res["word_ttl_sec"] == 300.0

    dsp_file = os.path.join(clean_config_env, "dsp_config.json")
    comment_file = os.path.join(clean_config_env, "comment_sync_config.json")
    assert os.path.isfile(dsp_file)
    assert os.path.isfile(comment_file)


def test_profile_active_persistence_and_clear(clean_config_env):
    assert get_saved_active_speaker_id() is None

    # プロファイル適用
    save_active_speaker_id("speaker_xyz")
    assert get_saved_active_speaker_id() == "speaker_xyz"

    # プロファイル解除
    save_active_speaker_id(None)
    assert get_saved_active_speaker_id() is None
