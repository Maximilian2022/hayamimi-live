"""
cloud/session_manager.py
ユーザー固有データ (置換辞書・OBSトークン・プロファイル設定) および
リアルタイム OBS 配信キューの管理
"""

import asyncio
import json
import logging
import os
import secrets
from typing import Dict, Optional, Set
from cloud.config import DATA_DIR

logger = logging.getLogger("hayamimi.cloud.session")


class UserSession:
    """特定ユーザーのセッションおよびデータ管理"""

    def __init__(self, user_id: str, username: str = "", display_name: str = ""):
        self.user_id = str(user_id)
        self.username = username
        self.display_name = display_name or username
        self.user_dir = os.path.join(DATA_DIR, "users", self.user_id)
        os.makedirs(self.user_dir, exist_ok=True)

        self.obs_token: str = self._load_or_create_obs_token()
        self.replacements: list[tuple[str, str]] = self._load_replacements()
        self.obs_subscribers: Set[asyncio.Queue] = set()
        self.last_transcript: str = ""

    def _load_or_create_obs_token(self) -> str:
        """OBS 字幕用シークレットトークンをロード、または新規生成する"""
        token_path = os.path.join(self.user_dir, "token.txt")
        if os.path.isfile(token_path):
            try:
                with open(token_path, "r", encoding="utf-8") as f:
                    tok = f.read().strip()
                    if tok and len(tok) >= 16:
                        return tok
            except Exception as e:
                logger.warning("Failed to read token for %s: %s", self.user_id, e)

        new_tok = f"obs_sec_{secrets.token_urlsafe(24)}"
        try:
            with open(token_path, "w", encoding="utf-8") as f:
                f.write(new_tok)
        except Exception as e:
            logger.error("Failed to save token for %s: %s", self.user_id, e)
        return new_tok

    def regenerate_obs_token(self) -> str:
        """OBS トークンを再生成して保存する"""
        new_tok = f"obs_sec_{secrets.token_urlsafe(24)}"
        token_path = os.path.join(self.user_dir, "token.txt")
        with open(token_path, "w", encoding="utf-8") as f:
            f.write(new_tok)
        self.obs_token = new_tok
        return new_tok

    def _load_replacements(self) -> list[tuple[str, str]]:
        """置換辞書 (replace.txt) をロードする"""
        rep_path = os.path.join(self.user_dir, "replace.txt")
        if not os.path.isfile(rep_path):
            # デフォルト辞書ファイルを作成
            try:
                with open(rep_path, "w", encoding="utf-8") as f:
                    f.write("# ユーザー専用 置換辞書 (誤認識しやすい単語を修正)\n# 誤認識語 => 正しい表記\n")
            except Exception:
                pass
            return []

        rules = []
        try:
            with open(rep_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if "=>" in line:
                        src, dst = line.split("=>", 1)
                        rules.append((src.strip(), dst.strip()))
                    elif "," in line:
                        parts = line.split(",", 1)
                        rules.append((parts[0].strip(), parts[1].strip()))
        except Exception as e:
            logger.warning("Failed to load replace.txt for %s: %s", self.user_id, e)
        return rules

    def save_replacements_raw(self, text: str) -> None:
        """置換辞書のテキストを保存し、メモリ内ルールを再読み込みする"""
        rep_path = os.path.join(self.user_dir, "replace.txt")
        with open(rep_path, "w", encoding="utf-8") as f:
            f.write(text)
        self.replacements = self._load_replacements()

    def get_replacements_raw(self) -> str:
        """置換辞書の生テキストを読み出す"""
        rep_path = os.path.join(self.user_dir, "replace.txt")
        if os.path.isfile(rep_path):
            with open(rep_path, "r", encoding="utf-8") as f:
                return f.read()
        return ""

    def apply_replacements(self, text: str) -> str:
        """テキストにユーザー個別置換ルールを適用する"""
        if not text or not self.replacements:
            return text
        result = text
        for src, dst in self.replacements:
            if src and src in result:
                result = result.replace(src, dst)
        return result

    def subscribe_obs(self) -> asyncio.Queue:
        """OBS ブラウザソース用の SSE キューを登録する"""
        q = asyncio.Queue(maxsize=100)
        self.obs_subscribers.add(q)
        return q

    def unsubscribe_obs(self, q: asyncio.Queue) -> None:
        """OBS ブラウザソース用の SSE キューを解除する"""
        self.obs_subscribers.discard(q)

    async def broadcast_transcript(self, text: str, is_final: bool = True) -> None:
        """そのユーザーの接続中 OBS クライアントに字幕を配信する"""
        refined_text = self.apply_replacements(text)
        self.last_transcript = refined_text

        payload = {
            "type": "asr_final" if is_final else "asr_partial",
            "text": refined_text,
            "raw_text": text,
            "is_final": is_final,
            "user_id": self.user_id,
        }

        event_str = f"event: message\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
        dead_queues = []
        for q in list(self.obs_subscribers):
            try:
                q.put_nowait(event_str)
            except asyncio.QueueFull:
                dead_queues.append(q)

        for q in dead_queues:
            self.obs_subscribers.discard(q)


class SessionManager:
    """全ユーザーのセッションおよびトークン逆引きの管理"""

    def __init__(self):
        self.users: Dict[str, UserSession] = {}
        self.token_to_user_id: Dict[str, str] = {}
        self._lock = asyncio.Lock()

    def get_or_create_user(self, user_id: str, username: str = "", display_name: str = "") -> UserSession:
        uid = str(user_id)
        if uid not in self.users:
            sess = UserSession(uid, username, display_name)
            self.users[uid] = sess
            self.token_to_user_id[sess.obs_token] = uid
        else:
            sess = self.users[uid]
            if username:
                sess.username = username
            if display_name:
                sess.display_name = display_name
        return self.users[uid]

    def get_user_by_obs_token(self, token: str) -> Optional[UserSession]:
        if not token:
            return None
        # キャッシュから逆引き
        uid = self.token_to_user_id.get(token)
        if uid and uid in self.users:
            return self.users[uid]

        # ディスク上の既存トークンから探索
        users_root = os.path.join(DATA_DIR, "users")
        if os.path.isdir(users_root):
            for entry in os.listdir(users_root):
                user_dir = os.path.join(users_root, entry)
                if os.path.isdir(user_dir):
                    t_path = os.path.join(user_dir, "token.txt")
                    if os.path.isfile(t_path):
                        try:
                            with open(t_path, "r", encoding="utf-8") as f:
                                if f.read().strip() == token:
                                    return self.get_or_create_user(entry)
                        except Exception:
                            pass
        return None

    def refresh_token_mapping(self, user_id: str, old_token: str, new_token: str) -> None:
        self.token_to_user_id.pop(old_token, None)
        self.token_to_user_id[new_token] = str(user_id)


# シングルトンインスタンス
session_manager = SessionManager()
