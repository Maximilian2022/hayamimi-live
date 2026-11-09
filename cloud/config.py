"""
cloud/config.py
hayamimi-cloud 設定モジュール
環境変数、.env、またはデフォルト値からクラウド版の設定をロードします。
"""

import os
from typing import List, Set

# .env ファイルがあれば読み込む (任意)
_env_path = os.path.join(os.path.dirname(__file__), ".env")
if os.path.isfile(_env_path):
    try:
        with open(_env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    k, v = k.strip(), v.strip().strip("'\"")
                    if k not in os.environ:
                        os.environ[k] = v
    except Exception:
        pass

# サービス基盤設定
BASE_URL: str = os.getenv("HAYAMIMI_BASE_URL", "https://hayamimi.kit-a.net").rstrip("/")
PORT: int = int(os.getenv("HAYAMIMI_PORT", "8833"))
HOST: str = os.getenv("HAYAMIMI_HOST", "0.0.0.0")

# Discord OAuth2 設定
DISCORD_CLIENT_ID: str = os.getenv("DISCORD_CLIENT_ID", "1569179545062608976")
DISCORD_CLIENT_SECRET: str = os.getenv("DISCORD_CLIENT_SECRET", "N35VUnX_Z2x5EwE8Gy-ziDsyZg9IXYKI")
DISCORD_REDIRECT_URI: str = os.getenv("DISCORD_REDIRECT_URI", f"{BASE_URL}/auth/callback")

# セッション暗号化鍵
SESSION_SECRET_KEY: str = os.getenv("SESSION_SECRET_KEY", "hayamimi-cloud-secret-salt-2026-secure-key")
SESSION_COOKIE_NAME: str = "hayamimi_cloud_session"

# 許可する Discord ユーザー ID (ホワイトリスト)
_default_allowed = ["310034034690359308", "386456746149019649", "1567350238870773811"]
_env_allowed = os.getenv("ALLOWED_DISCORD_IDS", "")
if _env_allowed:
    ALLOWED_DISCORD_IDS: Set[str] = {uid.strip() for uid in _env_allowed.split(",") if uid.strip()}
else:
    ALLOWED_DISCORD_IDS: Set[str] = set(_default_allowed)

# 管理者 Discord ID
ADMIN_DISCORD_ID: str = "310034034690359308"

# ディレクトリパス
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(BASE_DIR)
DATA_DIR: str = os.getenv("HAYAMIMI_DATA_DIR", os.path.join(PROJECT_ROOT, "data"))
MODELS_DIR: str = os.getenv("HAYAMIMI_MODELS_DIR", os.path.join(PROJECT_ROOT, "models"))

# レガシーパスからの自動マイグレーション (/app/cloud/data -> /app/data)
_legacy_data = os.path.join(BASE_DIR, "data")
if os.path.isdir(_legacy_data) and _legacy_data != DATA_DIR:
    import shutil
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        for item in os.listdir(_legacy_data):
            s = os.path.join(_legacy_data, item)
            d = os.path.join(DATA_DIR, item)
            if not os.path.exists(d):
                if os.path.isdir(s):
                    shutil.copytree(s, d)
                else:
                    shutil.copy2(s, d)
    except Exception as _e:
        pass


