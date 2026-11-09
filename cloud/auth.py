"""
cloud/auth.py
Discord OAuth2 認証および暗号署名付きセッション管理
"""

import base64
import hashlib
import hmac
import json
import time
import urllib.parse
from typing import Optional, Dict, Any
import httpx

from cloud.config import (
    DISCORD_CLIENT_ID,
    DISCORD_CLIENT_SECRET,
    DISCORD_REDIRECT_URI,
    SESSION_SECRET_KEY,
    ALLOWED_DISCORD_IDS,
)

DISCORD_API_ENDPOINT = "https://discord.com/api/v10"


def generate_discord_oauth_url(state: str = "") -> str:
    """Discord OAuth2 認可画面の URL を生成する (scope=identify のみ、Bot不要)"""
    params = {
        "client_id": DISCORD_CLIENT_ID,
        "redirect_uri": DISCORD_REDIRECT_URI,
        "response_type": "code",
        "scope": "identify",
    }
    if state:
        params["state"] = state
    return f"https://discord.com/oauth2/authorize?{urllib.parse.urlencode(params)}"


async def exchange_code_for_token(code: str) -> Optional[str]:
    """認可コード (code) を Access Token に交換する"""
    data = {
        "client_id": DISCORD_CLIENT_ID,
        "client_secret": DISCORD_CLIENT_SECRET,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": DISCORD_REDIRECT_URI,
    }
    headers = {"Content-Type": "application/x-www-form-urlencoded"}

    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(f"{DISCORD_API_ENDPOINT}/oauth2/token", data=data, headers=headers)
        if resp.status_code == 200:
            result = resp.json()
            return result.get("access_token")
        return None


async def fetch_discord_user(access_token: str) -> Optional[Dict[str, Any]]:
    """Access Token を使って Discord ユーザープロフィール (@me) を取得する"""
    headers = {"Authorization": f"Bearer {access_token}"}
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(f"{DISCORD_API_ENDPOINT}/users/@me", headers=headers)
        if resp.status_code == 200:
            return resp.json()
        return None


def is_user_allowed(discord_user_id: str) -> bool:
    """ホワイトリストに登録されているか判定する"""
    return str(discord_user_id).strip() in ALLOWED_DISCORD_IDS


def create_session_token(user_data: Dict[str, Any], max_age_days: int = 30) -> str:
    """ユーザー情報を含む HMAC-SHA256 署名付きセッショントークンを生成する"""
    payload = {
        "id": str(user_data.get("id")),
        "username": user_data.get("username", ""),
        "global_name": user_data.get("global_name") or user_data.get("username", ""),
        "avatar": user_data.get("avatar") or "",
        "exp": int(time.time()) + (max_age_days * 86400),
    }
    payload_json = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    payload_b64 = base64.urlsafe_b64encode(payload_json.encode("utf-8")).decode("utf-8").rstrip("=")

    sig = hmac.new(
        SESSION_SECRET_KEY.encode("utf-8"),
        payload_b64.encode("utf-8"),
        hashlib.sha256
    ).digest()
    sig_b64 = base64.urlsafe_b64encode(sig).decode("utf-8").rstrip("=")

    return f"{payload_b64}.{sig_b64}"


def verify_session_token(token: str) -> Optional[Dict[str, Any]]:
    """署名付きセッショントークンを検証し、有効なユーザーペイロードを返す"""
    if not token or "." not in token:
        return None
    try:
        payload_b64, sig_b64 = token.rsplit(".", 1)

        # 署名検証
        expected_sig = hmac.new(
            SESSION_SECRET_KEY.encode("utf-8"),
            payload_b64.encode("utf-8"),
            hashlib.sha256
        ).digest()
        actual_sig = base64.urlsafe_b64decode(sig_b64 + "==")
        if not hmac.compare_digest(expected_sig, actual_sig):
            return None

        # ペイロードデコード
        payload_json = base64.urlsafe_b64decode(payload_b64 + "==").decode("utf-8")
        payload = json.loads(payload_json)

        # 有効期限チェック
        if payload.get("exp", 0) < int(time.time()):
            return None

        # ホワイトリストチェック (途中で除外された場合も即座に無効化)
        if not is_user_allowed(payload.get("id", "")):
            return None

        return payload
    except Exception:
        return None
