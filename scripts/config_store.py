# -*- coding: utf-8 -*-
"""config_store.py - Hayamimi 堅牢な設定管理 & アトミック永続化モジュール

設定ファイル（JSON）の破損・消失・アップデート時の初期化を完全に防止するため、
以下の安全機構を提供する:
1. アトミック書き込み (一時ファイル作成 -> flush/fsync -> os.replace)
2. 自動直前バックアップ (.bak 生成)
3. 読み込み破損時の .bak 自動フェイルオーバー & 自己修復
4. 永続ストレージディレクトリの厳格な解決 (Tempへの安易な退避防止、ユーザー永続領域フォールバック)
5. OBS接続設定 (URL, パスワード, マイク名, 有効フラグ) のサーバー側永続化
6. 話者プロファイル選択状態 (active_speaker_id) の永続化
"""

import copy
import json
import logging
import os
import shutil
import sys
import tempfile
from typing import Any, Dict, Optional

logger = logging.getLogger("hayamimi.config_store")

_CACHED_STORAGE_DIR: Optional[str] = None

# 設定ファイル名定数
CONFIG_FILENAME_ASR = "asr_config.json"
CONFIG_FILENAME_OVERLAY = "overlay_style.json"
CONFIG_FILENAME_OBS = "obs_config.json"
CONFIG_FILENAME_DSP = "dsp_config.json"
CONFIG_FILENAME_COMMENT = "comment_sync_config.json"
FILENAME_GEMINI_KEY = "gemini_key.txt"


def reset_storage_dir_cache() -> None:
    """テスト用: キャッシュされたストレージディレクトリをリセットする。"""
    global _CACHED_STORAGE_DIR
    _CACHED_STORAGE_DIR = None


def get_storage_dir() -> str:
    """安全で永続的な設定ディレクトリを解決して返す。

    優先順位:
    1. HAYAMIMI_CONFIG_DIR 環境変数 (テスト分離 / 明示的指定)
    2. Docker / コンテナ環境 (/app/config または /app/models)
    3. プロジェクトルートの config/ ディレクトリ
    4. ユーザープロファイル永続ディレクトリ (%LOCALAPPDATA%\\hayamimi\\config または ~/.config/hayamimi)
    5. システムテンポラリ (極限状態の最終手段、明示的警告ログ付き)
    """
    global _CACHED_STORAGE_DIR

    env_dir = os.environ.get("HAYAMIMI_CONFIG_DIR")
    if env_dir:
        os.makedirs(env_dir, exist_ok=True)
        return env_dir

    if _CACHED_STORAGE_DIR and os.path.isdir(_CACHED_STORAGE_DIR):
        return _CACHED_STORAGE_DIR

    # 2. コンテナ環境
    for candidate in ("/app/config", "/app/models"):
        if os.path.isdir(candidate):
            try:
                test_file = os.path.join(candidate, f".write_test_{os.getpid()}")
                with open(test_file, "w", encoding="utf-8") as f:
                    f.write("ok")
                os.remove(test_file)
                _CACHED_STORAGE_DIR = candidate
                return candidate
            except Exception:
                pass

    # 3. プロジェクトルートの config/
    local_cfg = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config"
    )
    try:
        os.makedirs(local_cfg, exist_ok=True)
        test_file = os.path.join(local_cfg, f".write_test_{os.getpid()}")
        with open(test_file, "w", encoding="utf-8") as f:
            f.write("ok")
        os.remove(test_file)
        _CACHED_STORAGE_DIR = local_cfg
        return local_cfg
    except Exception as e:
        logger.warning(
            "Project root config dir '%s' is not writable (%s). Trying user persistent storage.",
            local_cfg,
            e,
        )

    # 4. ユーザープロファイル永続ディレクトリ (%LOCALAPPDATA%\\hayamimi\\config or ~/.config/hayamimi)
    user_base = os.environ.get("LOCALAPPDATA")
    if not user_base:
        user_base = os.path.join(os.path.expanduser("~"), ".config")
    user_cfg = os.path.join(user_base, "hayamimi", "config")
    try:
        os.makedirs(user_cfg, exist_ok=True)
        test_file = os.path.join(user_cfg, f".write_test_{os.getpid()}")
        with open(test_file, "w", encoding="utf-8") as f:
            f.write("ok")
        os.remove(test_file)
        logger.info("Using user profile persistent config directory: %s", user_cfg)
        _CACHED_STORAGE_DIR = user_cfg
        return user_cfg
    except Exception as e:
        logger.warning(
            "User persistent config dir '%s' is not writable (%s). Falling back to tempdir.",
            user_cfg,
            e,
        )

    # 5. テンポラリディレクトリ (極限状態)
    tmp = tempfile.gettempdir()
    logger.error(
        "CRITICAL: All persistent config locations failed. Using temporary directory '%s'. "
        "Settings may be lost upon system reboot or disk cleanup!",
        tmp,
    )
    _CACHED_STORAGE_DIR = tmp
    return tmp


def atomic_save_json(path: str, data: Any, create_backup: bool = True) -> None:
    """データを JSON として一時ファイルに書き込み、アトミックに差し替える。

    既存ファイルがある場合は直前バックアップ (.bak) を自動作成する。
    """
    dir_name = os.path.dirname(os.path.abspath(path))
    os.makedirs(dir_name, exist_ok=True)

    # 既存の有効なファイルがあれば .bak にバックアップ
    if create_backup and os.path.isfile(path):
        try:
            if os.path.getsize(path) > 0:
                bak_path = path + ".bak"
                shutil.copy2(path, bak_path)
        except Exception as e:
            logger.debug("Failed to create backup for %s: %s", path, e)

    # 同一ディレクトリに一時ファイルを作成
    fd, temp_path = tempfile.mkstemp(dir=dir_name, prefix=".tmp_cfg_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            try:
                os.fsync(f.fileno())
            except Exception:
                pass
        os.replace(temp_path, path)
    except Exception:
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass
        raise


def load_json_with_backup(path: str, default: Any = None) -> Any:
    """JSON ファイルをロードする。破損時は .bak から自動修復する。"""
    # 1. メインファイルを試行
    if os.path.isfile(path) and os.path.getsize(path) > 0:
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as exc:
            logger.warning(
                "Config file '%s' corrupted or unreadable (%s). Checking backup...",
                path,
                exc,
            )

    # 2. バックアップファイル (.bak) を試行
    bak_path = path + ".bak"
    if os.path.isfile(bak_path) and os.path.getsize(bak_path) > 0:
        try:
            with open(bak_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            logger.info("Successfully recovered configuration from backup '%s'", bak_path)
            # 破損していたメインファイルをバックアップから自己修復
            try:
                atomic_save_json(path, data, create_backup=False)
            except Exception as se:
                logger.warning("Failed to auto-heal %s from backup: %s", path, se)
            return data
        except Exception as be:
            logger.error("Backup file '%s' also corrupted: %s", bak_path, be)

    return copy.deepcopy(default) if default is not None else {}


# ==============================================================================
# OBS WebSocket 設定
# ==============================================================================
_DEFAULT_OBS_CONFIG: Dict[str, Any] = {
    "enabled": True,
    "url": "ws://127.0.0.1:4455",
    "password": "",
    "target_source": "",
}


def get_saved_obs_config() -> Dict[str, Any]:
    """保存された OBS WebSocket 接続設定を返す。"""
    storage_dir = get_storage_dir()
    path = os.path.join(storage_dir, CONFIG_FILENAME_OBS)
    cfg = load_json_with_backup(path, default=_DEFAULT_OBS_CONFIG)
    # デフォルトキーを確実に包含
    res = copy.deepcopy(_DEFAULT_OBS_CONFIG)
    if isinstance(cfg, dict):
        res.update(cfg)
    return res


def save_obs_config(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """OBS WebSocket 接続設定を保存する。"""
    current = get_saved_obs_config()
    for k in ("enabled", "url", "password", "target_source"):
        if k in cfg:
            current[k] = cfg[k]
    storage_dir = get_storage_dir()
    path = os.path.join(storage_dir, CONFIG_FILENAME_OBS)
    atomic_save_json(path, current, create_backup=True)
    return current


# ==============================================================================
# ASR / 翻訳 / 句読点 / アクティブ話者プロファイル設定
# ==============================================================================
def get_saved_asr_config() -> Dict[str, Any]:
    """保存された ASR 設定を返す。"""
    storage_dir = get_storage_dir()
    path = os.path.join(storage_dir, CONFIG_FILENAME_ASR)
    return load_json_with_backup(path, default={})


def save_asr_config(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """ASR 設定を保存する。"""
    storage_dir = get_storage_dir()
    path = os.path.join(storage_dir, CONFIG_FILENAME_ASR)
    existing = get_saved_asr_config()
    save_keys = (
        "lang",
        "translate",
        "punctuate",
        "dual_confirm",
        "lid_switch_confirm",
        "min_switch_s",
        "vad",
        "active_speaker_id",
        "ja_haas_switched",
    )
    for k in save_keys:
        if k in cfg:
            existing[k] = cfg[k]
    atomic_save_json(path, existing, create_backup=True)
    return existing


def get_saved_active_speaker_id() -> Optional[str]:
    """保存されたアクティブ話者プロファイル ID を返す。"""
    cfg = get_saved_asr_config()
    sid = cfg.get("active_speaker_id")
    if sid and isinstance(sid, str) and sid.strip():
        return sid.strip()
    return None


def save_active_speaker_id(speaker_id: Optional[str]) -> None:
    """アクティブ話者プロファイル ID を永続化保存する。"""
    save_asr_config({"active_speaker_id": speaker_id or ""})


# ==============================================================================
# OBS オーバーレイ スタイル設定
# ==============================================================================
def get_saved_overlay_style(default_style: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """保存された OBS オーバーレイ スタイルを返す。"""
    storage_dir = get_storage_dir()
    path = os.path.join(storage_dir, CONFIG_FILENAME_OVERLAY)
    return load_json_with_backup(path, default=default_style or {})


def save_overlay_style(style: Dict[str, Any]) -> None:
    """OBS オーバーレイ スタイルを保存する。"""
    storage_dir = get_storage_dir()
    path = os.path.join(storage_dir, CONFIG_FILENAME_OVERLAY)
    atomic_save_json(path, style, create_backup=True)


# ==============================================================================
# 設定フォルダ全体の自動バックアップ (アップデート直前保護用)
# ==============================================================================
def create_config_snapshot_backup() -> Optional[str]:
    """現在の config/ ディレクトリ全体の自動スナップショットバックアップを作成する。

    アップデート時や重大な更新の直前に呼び出され、万が一のファイル消失を防ぐ。
    作成先: <project_root>/config_backups/backup_<timestamp>
    """
    try:
        storage_dir = get_storage_dir()
        if not os.path.isdir(storage_dir):
            return None
        import datetime

        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        parent_dir = os.path.dirname(storage_dir)
        backups_root = os.path.join(parent_dir, "config_backups")
        os.makedirs(backups_root, exist_ok=True)
        backup_target = os.path.join(backups_root, f"backup_{ts}")
        shutil.copytree(storage_dir, backup_target, dirs_exist_ok=True)
        logger.info("Created config snapshot backup at: %s", backup_target)
        return backup_target
    except Exception as e:
        logger.warning("Failed to create config snapshot backup: %s", e)
        return None


# ==============================================================================
# 旧置換辞書から Initial Prompt への自動移行ヘルパー
# ==============================================================================
def extract_initial_prompt_from_replace_file(replace_file: str, max_words: int = 50) -> str:
    """既存の置換辞書 (*_replace.txt) から固有名詞を抽出し、Whisper の Initial Prompt 文字列を生成する。

    タブ区切り ('アマンガス\tAmongUs') または等号区切り ('アマンガス=AmongUs') から、
    正規の英単語・固有名詞（置換先）を抽出し、カンマ区切りで返却します。
    """
    if not replace_file or not os.path.exists(replace_file):
        return ""

    words: list[str] = []
    seen: set[str] = set()

    try:
        with open(replace_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue

                target = ""
                if "\t" in line:
                    parts = line.split("\t", 1)
                    target = parts[1].strip()
                elif "=" in line:
                    parts = line.split("=", 1)
                    target = parts[1].strip()

                if target and target not in seen:
                    seen.add(target)
                    words.append(target)
                    if len(words) >= max_words:
                        break
    except Exception as e:
        logger.warning("Failed to extract initial prompt from %s: %s", replace_file, e)

    return ", ".join(words)


