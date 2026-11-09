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
from typing import Any, Dict, List, Optional, Set
from cloud.config import DATA_DIR

logger = logging.getLogger("hayamimi.cloud.session")

DEFAULT_OVERLAY_STYLE: Dict[str, Any] = {
    "font": "'Zen Kaku Gothic New', sans-serif",
    "size": 42,
    "color": "#ffffff",
    "strokeColor": "#000000",
    "strokeWidth": 3.0,
    "effect": "stroke",  # "stroke" | "double" | "glow"
    "align": "center",   # "left" | "center" | "right"
    "bottom": 4.0,       # vh
    "bgColor": "#000000",
    "bgOpacity": 0,      # 0 to 100%
    "radius": 8,         # px
    # 翻訳字幕スタイル
    "showTrans": True,
    "transSize": 28,
    "transColor": "#ffd166",
    "transStrokeColor": "#000000",
    "transStrokeWidth": 2.5,
    "transEffect": "stroke",
}

DEFAULT_SETTINGS: Dict[str, Any] = {
    "asr_model": "haas",  # "haas" (Haas-ja-v1.0) or "reazon" (ReazonSpeech)
    "lang": "ja-haas",    # "ja-haas" or "ja" (Standalone互換)
    "enable_punct": True,
    "enable_itn": True,
    "enable_trans": False,
    "trans_target_langs": ["en"],
}


def sanitize_style(style: dict) -> dict:
    """スタイル辞書のバリデーションとクランプ"""
    res = dict(DEFAULT_OVERLAY_STYLE)
    if not isinstance(style, dict):
        return res

    if "font" in style and isinstance(style["font"], str):
        res["font"] = style["font"][:100]
    if "size" in style:
        try:
            res["size"] = max(12, min(160, int(style["size"])))
        except (ValueError, TypeError):
            pass
    if "color" in style and isinstance(style["color"], str):
        res["color"] = style["color"][:20]
    if "strokeColor" in style and isinstance(style["strokeColor"], str):
        res["strokeColor"] = style["strokeColor"][:20]
    if "strokeWidth" in style:
        try:
            res["strokeWidth"] = max(0.0, min(20.0, float(style["strokeWidth"])))
        except (ValueError, TypeError):
            pass
    if "effect" in style and style["effect"] in ("stroke", "double", "glow"):
        res["effect"] = style["effect"]
    if "align" in style and style["align"] in ("left", "center", "right"):
        res["align"] = style["align"]
    if "bottom" in style:
        try:
            res["bottom"] = max(0.0, min(50.0, float(style["bottom"])))
        except (ValueError, TypeError):
            pass
    if "bgColor" in style and isinstance(style["bgColor"], str):
        res["bgColor"] = style["bgColor"][:20]
    if "bgOpacity" in style:
        try:
            res["bgOpacity"] = max(0, min(100, int(style["bgOpacity"])))
        except (ValueError, TypeError):
            pass
    if "radius" in style:
        try:
            res["radius"] = max(0, min(50, int(style["radius"])))
        except (ValueError, TypeError):
            pass
    if "showTrans" in style:
        res["showTrans"] = bool(style["showTrans"])
    if "transSize" in style:
        try:
            res["transSize"] = max(10, min(120, int(style["transSize"])))
        except (ValueError, TypeError):
            pass
    if "transColor" in style and isinstance(style["transColor"], str):
        res["transColor"] = style["transColor"][:20]
    if "transStrokeColor" in style and isinstance(style["transStrokeColor"], str):
        res["transStrokeColor"] = style["transStrokeColor"][:20]
    if "transStrokeWidth" in style:
        try:
            res["transStrokeWidth"] = max(0.0, min(20.0, float(style["transStrokeWidth"])))
        except (ValueError, TypeError):
            pass
    if "transEffect" in style and style["transEffect"] in ("stroke", "double", "glow"):
        res["transEffect"] = style["transEffect"]
    return res


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
        self.style: Dict[str, Any] = self._load_style()
        self.settings: Dict[str, Any] = self._load_settings()
        self.obs_subscribers: Set[asyncio.Queue] = set()
        self.last_transcript: str = ""
        self.active_vad_session: Any = None

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

    def append_replacements(self, new_rules: List[tuple[str, str]]) -> int:
        """新しい置換ルールを replace.txt に追記（重複排除）し、追加件数を返す"""
        if not new_rules:
            return 0
        existing_srcs = {src for src, _ in self.replacements}
        added = []
        for src, dst in new_rules:
            src = src.strip()
            dst = dst.strip()
            if not src or not dst or src in existing_srcs:
                continue
            added.append((src, dst))
            existing_srcs.add(src)

        if not added:
            return 0

        rep_path = os.path.join(self.user_dir, "replace.txt")
        with open(rep_path, "a", encoding="utf-8") as f:
            f.write("\n# YouTube 自動抽出ルール\n")
            for src, dst in added:
                f.write(f"{src} => {dst}\n")

        self.replacements = self._load_replacements()
        return len(added)

    def _load_style(self) -> Dict[str, Any]:
        """ユーザー専用字幕スタイル (style.json) をロードする"""
        path = os.path.join(self.user_dir, "style.json")
        if os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    return sanitize_style(data)
            except Exception as e:
                logger.warning("Failed to load style.json for %s: %s", self.user_id, e)
        return dict(DEFAULT_OVERLAY_STYLE)

    def save_style(self, style: dict) -> Dict[str, Any]:
        """スタイルを保存し、メモリを更新して OBS へ通知する"""
        sanitized = sanitize_style(style)
        path = os.path.join(self.user_dir, "style.json")
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(sanitized, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error("Failed to save style.json for %s: %s", self.user_id, e)

        self.style = sanitized
        return sanitized

    async def broadcast_style(self) -> None:
        """接続中の OBS クライアントにスタイル変更を通知して即時反映させる"""
        payload = {
            "type": "style",
            "style": self.style,
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

    def _load_settings(self) -> Dict[str, Any]:
        """ASR 設定 (settings.json: 句読点・ITN 等) をロードする"""
        path = os.path.join(self.user_dir, "settings.json")
        if os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    return {**DEFAULT_SETTINGS, **data}
            except Exception as e:
                logger.warning("Failed to load settings.json for %s: %s", self.user_id, e)
        return dict(DEFAULT_SETTINGS)

    def save_settings(self, settings: dict) -> Dict[str, Any]:
        """ASR 設定を保存する"""
        cur = dict(self.settings)
        if "asr_model" in settings:
            model = str(settings["asr_model"]).lower().strip()
            cur["asr_model"] = "reazon" if model in ("reazon", "rz", "ja") else "haas"
            cur["lang"] = "ja" if cur["asr_model"] == "reazon" else "ja-haas"
        elif "lang" in settings:
            lang = str(settings["lang"]).lower().strip()
            cur["lang"] = lang
            cur["asr_model"] = "reazon" if lang == "ja" else "haas"

        if "enable_punct" in settings:
            cur["enable_punct"] = bool(settings["enable_punct"])
        if "enable_itn" in settings:
            cur["enable_itn"] = bool(settings["enable_itn"])
        if "enable_trans" in settings:
            cur["enable_trans"] = bool(settings["enable_trans"])
        if "trans_target_langs" in settings and isinstance(settings["trans_target_langs"], list):
            cur["trans_target_langs"] = [str(x) for x in settings["trans_target_langs"] if str(x).strip()]

        path = os.path.join(self.user_dir, "settings.json")
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(cur, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error("Failed to save settings.json for %s: %s", self.user_id, e)

        self.settings = cur

        # アクティブな VAD セッションがあればモデルを即時切り替え
        if self.active_vad_session is not None and hasattr(self.active_vad_session, "set_model_choice"):
            try:
                self.active_vad_session.set_model_choice(cur["asr_model"])
                logger.info("Switched live ASR model for user %s to %s", self.user_id, cur["asr_model"])
            except Exception as e:
                logger.warning("Failed to update active VAD session model: %s", e)

        return cur

    def get_gemini_key(self) -> str:
        """ユーザーの Gemini API キーを取得する"""
        path = os.path.join(self.user_dir, "gemini_key.txt")
        if os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return f.read().strip()
            except Exception:
                pass
        return ""

    def save_gemini_key(self, key: str) -> None:
        """ユーザーの Gemini API キーを安全に保存する"""
        path = os.path.join(self.user_dir, "gemini_key.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write(key.strip())

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

    async def broadcast_translation(self, lang: str, text: str, original: str = "") -> None:
        """接続中 OBS クライアントに翻訳字幕を配信する"""
        payload = {
            "type": "translation",
            "lang": lang,
            "text": text,
            "original": original,
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

