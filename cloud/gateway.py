"""
cloud/gateway.py
hayamimi-cloud マルチユーザー・Discord OAuth2 認証 ASR ゲートウェイ
FastAPI + WebSocket + SSE によるリアルタイム音声認識 & OBS 字幕配信
"""

import asyncio
import logging
import os
import struct
import sys
import threading
from contextlib import asynccontextmanager
from typing import Optional

# tools / scripts をインポートパスに追加
_ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT_DIR not in sys.path:
    sys.path.insert(0, _ROOT_DIR)
_SCRIPTS_DIR = os.path.join(_ROOT_DIR, "scripts")
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

import numpy as np
from fastapi import FastAPI, Request, Response, WebSocket, WebSocketDisconnect, HTTPException, Depends
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from cloud.config import (
    BASE_URL,
    PORT,
    HOST,
    SESSION_COOKIE_NAME,
    MODELS_DIR,
    DATA_DIR,
    ALLOWED_DISCORD_IDS,
)
from cloud.auth import (
    generate_discord_oauth_url,
    exchange_code_for_token,
    fetch_discord_user,
    is_user_allowed,
    create_session_token,
    verify_session_token,
)
from cloud.session_manager import session_manager, UserSession
from cloud.engine import SharedAsrEngine, UserVadSession, shared_punctuator
from cloud.translator import SUPPORTED_LANGUAGES, translate_text

# ロギング設定
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("hayamimi.cloud.gateway")

# グローバル ASR エンジン (1 インスタンスを全ユーザーで共有)
shared_asr_engine: Optional[SharedAsrEngine] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global shared_asr_engine
    logger.info("Starting hayamimi-cloud gateway...")
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(MODELS_DIR, exist_ok=True)

    try:
        shared_asr_engine = SharedAsrEngine(models_dir=MODELS_DIR)
        logger.info("✔ Shared ASR engine ready.")
    except Exception as e:
        logger.error("Failed to initialize ASR engine: %s", e)
        logger.warning("Gateway will start, but ASR may fail until models are downloaded.")
        shared_asr_engine = None

    yield
    logger.info("Shutting down hayamimi-cloud gateway...")


app = FastAPI(title="hayamimi-cloud", lifespan=lifespan)

# テンプレートエンジン
templates_dir = os.path.join(os.path.dirname(__file__), "templates")
templates = Jinja2Templates(directory=templates_dir)


def get_current_user(request: Request) -> Optional[dict]:
    """リクエスト Cookie から現在の Discord ユーザー情報を取得・検証する"""
    session_cookie = request.cookies.get(SESSION_COOKIE_NAME)
    if not session_cookie:
        return None
    return verify_session_token(session_cookie)


# ---------------------------------------------------------------------------
# 認証ルーティング
# ---------------------------------------------------------------------------

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, error: str = ""):
    user = get_current_user(request)
    if user:
        return RedirectResponse(url="/", status_code=302)
    return templates.TemplateResponse(
        request=request, name="login.html", context={"error": error}
    )


@app.get("/login/discord")
async def login_discord():
    oauth_url = generate_discord_oauth_url()
    return RedirectResponse(url=oauth_url, status_code=302)


@app.get("/auth/callback")
async def auth_callback(request: Request, code: str = "", error: str = ""):
    if error or not code:
        logger.warning("Discord auth returned error: %s", error)
        return RedirectResponse(url="/login?error=Discord認証がキャンセルされました", status_code=302)

    # 1. 認可コードからトークンを取得
    access_token = await exchange_code_for_token(code)
    if not access_token:
        logger.error("Failed to exchange code for token")
        return RedirectResponse(url="/login?error=認証トークンの取得に失敗しました", status_code=302)

    # 2. Discord プロフィールを取得
    discord_user = await fetch_discord_user(access_token)
    if not discord_user:
        logger.error("Failed to fetch Discord user profile")
        return RedirectResponse(url="/login?error=プロフィール取得に失敗しました", status_code=302)

    uid = str(discord_user.get("id"))
    username = discord_user.get("username", "")

    # 3. ホワイトリスト判定
    if not is_user_allowed(uid):
        logger.warning("Access denied for unlisted user: %s (ID: %s)", username, uid)
        return RedirectResponse(url=f"/login?error=アクセス権限がありません (ID: {uid})", status_code=302)

    logger.info("✔ User logged in successfully: %s (%s)", username, uid)

    # 4. セッショントークン生成 & Cookie 発行
    session_token = create_session_token(discord_user)
    response = RedirectResponse(url="/", status_code=302)
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=session_token,
        max_age=30 * 86400,
        httponly=True,
        samesite="lax",
        secure=True,  # HTTPS 必須
    )
    return response


@app.get("/logout")
async def logout():
    response = RedirectResponse(url="/login", status_code=302)
    response.delete_cookie(SESSION_COOKIE_NAME)
    return response


# ---------------------------------------------------------------------------
# YouTube 辞書育成バックグラウンドタスク管理
# ---------------------------------------------------------------------------
youtube_grow_tasks: dict[str, dict] = {}
youtube_grow_lock = threading.Lock()


def _run_youtube_grow_worker(user_id: str, url: str, max_videos: int, gemini_key: str):
    """別スレッドで YouTube 解析と Gemini による置換辞書生成を実行"""
    u_sess = session_manager.get_or_create_user(user_id)
    with youtube_grow_lock:
        task = youtube_grow_tasks[user_id]
        task["status"] = "running"
        task["progress"] = 0.05
        task["message"] = "YouTube 動画のメタデータを解析中..."
        task["logs"] = ["タスク初期化: YouTube のメタデータ取得を開始します..."]

    def _append_log(msg: str):
        with youtube_grow_lock:
            task["logs"].append(msg)
            if len(task["logs"]) > 60:
                task["logs"].pop(0)

    def _status_cb(stage: str, prog: float, msg: str):
        with youtube_grow_lock:
            task["progress"] = prog
            task["message"] = msg
        _append_log(f"[{stage}] (int({prog*100})%) {msg}")

    try:
        from tools.profile_builder.pipeline import ProfileBuilderPipeline
        pipeline = ProfileBuilderPipeline(api_key=gemini_key)
        prof = pipeline.run(
            youtube_url=url,
            max_videos=max_videos,
            status_callback=_status_cb,
            log_callback=lambda lvl, text: _append_log(f"[{lvl}] {text}"),
        )

        new_tuples = [(r.pattern, r.replacement) for r in prof.replacement_rules]
        added_count = u_sess.append_replacements(new_tuples)

        with youtube_grow_lock:
            task["status"] = "completed"
            task["progress"] = 1.0
            task["added_count"] = added_count
            task["message"] = f"辞書育成が完了しました！新たに {added_count} 個の置換ルールを追加しました。"
        _append_log(f"完了: 計 {len(new_tuples)} 個の抽出ルールのうち {added_count} 個の新規ルールを replace.txt に追記しました。")
    except Exception as e:
        logger.error("YouTube grow task failed for %s: %s", user_id, e)
        with youtube_grow_lock:
            task["status"] = "error"
            task["progress"] = 0.0
            task["message"] = f"辞書育成に失敗しました: {e}"
        _append_log(f"エラー発生: {e}")


# ---------------------------------------------------------------------------
# ダッシュボード & 設定 (/dashboard)
# ---------------------------------------------------------------------------

@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard_page(request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    uid = user["id"]
    u_sess = session_manager.get_or_create_user(
        uid, username=user.get("username", ""), display_name=user.get("global_name", "")
    )

    obs_url = f"{BASE_URL}/overlay?token={u_sess.obs_token}"
    dict_text = u_sess.get_replacements_raw()
    style = u_sess.style
    settings = u_sess.settings
    gemini_key = u_sess.get_gemini_key()
    masked_key = (gemini_key[:4] + "..." + gemini_key[-4:]) if len(gemini_key) >= 8 else ("登録済み" if gemini_key else "未登録")

    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "user": user,
            "obs_url": obs_url,
            "dictionary_text": dict_text,
            "style": style,
            "settings": settings,
            "has_gemini_key": bool(gemini_key),
            "masked_key": masked_key,
            "supported_languages": SUPPORTED_LANGUAGES,
        },
    )


# ---------------------------------------------------------------------------
# Top (/) および /mic: Web MIC & STUDIO 画面
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
@app.get("/mic", response_class=HTMLResponse)
async def mic_top_page(request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    u_sess = session_manager.get_or_create_user(user["id"])
    return templates.TemplateResponse(
        request=request,
        name="mic.html",
        context={
            "user": user,
            "obs_token": u_sess.obs_token,
            "obs_url": f"{BASE_URL}/overlay?token={u_sess.obs_token}",
            "settings": u_sess.settings,
            "style": u_sess.style,
            "replacements": u_sess.replacements,
        },
    )



@app.post("/api/dictionary")
async def save_dictionary(request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")

    body = await request.json()
    text = body.get("text", "")
    u_sess = session_manager.get_or_create_user(user["id"])
    u_sess.save_replacements_raw(text)
    return {"ok": True}


@app.post("/api/regenerate-token")
async def regenerate_token(request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")

    u_sess = session_manager.get_or_create_user(user["id"])
    old_tok = u_sess.obs_token
    new_tok = u_sess.regenerate_obs_token()
    session_manager.refresh_token_mapping(user["id"], old_tok, new_tok)

    new_obs_url = f"{BASE_URL}/overlay?token={new_tok}"
    return {"ok": True, "new_obs_url": new_obs_url}


@app.get("/api/style")
async def get_style(request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    u_sess = session_manager.get_or_create_user(user["id"])
    return {"ok": True, "style": u_sess.style}


@app.post("/api/style")
async def save_style(request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    body = await request.json()
    u_sess = session_manager.get_or_create_user(user["id"])
    saved = u_sess.save_style(body)
    await u_sess.broadcast_style()
    return {"ok": True, "style": saved}


@app.get("/api/settings")
async def get_settings(request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    u_sess = session_manager.get_or_create_user(user["id"])
    return {"ok": True, "settings": u_sess.settings}


@app.post("/api/settings")
async def save_settings(request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    body = await request.json()
    u_sess = session_manager.get_or_create_user(user["id"])
    saved = u_sess.save_settings(body)
    return {"ok": True, "settings": saved}


# Standalone 互換 API エンドポイント (/config, /overlay-style, /replacements, /reset)
@app.get("/config")
async def get_config(request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    u_sess = session_manager.get_or_create_user(user["id"])
    settings = u_sess.settings
    return {
        "lang": settings.get("lang", "ja-haas"),
        "ja_tier": "haas" if settings.get("asr_model", "haas") == "haas" else "rz",
        "asr_model": settings.get("asr_model", "haas"),
        "translate": settings.get("trans_target_langs", ["en"]) if settings.get("enable_trans", False) else [],
        "translate_enabled": settings.get("enable_trans", False),
        "punctuate": settings.get("enable_punct", True),
        "itn": settings.get("enable_itn", True),
        "obs_token": u_sess.obs_token,
        "obs_url": f"{BASE_URL}/overlay?token={u_sess.obs_token}",
    }


@app.post("/config")
async def post_config(request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    body = await request.json()
    u_sess = session_manager.get_or_create_user(user["id"])
    update_dict = {}
    if "lang" in body:
        update_dict["lang"] = body["lang"]
    if "asr_model" in body:
        update_dict["asr_model"] = body["asr_model"]
    if "translate" in body:
        trans_list = body["translate"] if isinstance(body["translate"], list) else []
        update_dict["enable_trans"] = len(trans_list) > 0
        if trans_list:
            update_dict["trans_target_langs"] = trans_list
    if "translate_enabled" in body:
        update_dict["enable_trans"] = bool(body["translate_enabled"])
    if "punctuate" in body:
        update_dict["enable_punct"] = bool(body["punctuate"])
    if "enable_punct" in body:
        update_dict["enable_punct"] = bool(body["enable_punct"])
    if "itn" in body:
        update_dict["enable_itn"] = bool(body["itn"])
    if "enable_itn" in body:
        update_dict["enable_itn"] = bool(body["enable_itn"])

    saved = u_sess.save_settings(update_dict)
    return {"ok": True, "settings": saved}


@app.get("/overlay-style")
async def get_overlay_style(request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    u_sess = session_manager.get_or_create_user(user["id"])
    return u_sess.style


@app.post("/overlay-style")
async def post_overlay_style(request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    body = await request.json()
    u_sess = session_manager.get_or_create_user(user["id"])
    saved = u_sess.save_style(body)
    await u_sess.broadcast_style()
    return saved


@app.get("/replacements")
async def get_replacements_compat(request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    u_sess = session_manager.get_or_create_user(user["id"])
    return {a: b for a, b in u_sess.replacements}


@app.post("/replacements")
async def post_replacements_compat(request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    body = await request.json()
    u_sess = session_manager.get_or_create_user(user["id"])
    if isinstance(body, dict):
        lines = [f"{k}={v}" for k, v in body.items()]
        u_sess.save_replacements("\n".join(lines))
    return {"ok": True}


@app.post("/reset")
async def reset_session_endpoint(request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    u_sess = session_manager.get_or_create_user(user["id"])
    if u_sess.active_vad_session:
        u_sess.active_vad_session.reset()
    return {"ok": True}


@app.get("/api/gemini-key")
async def get_gemini_key_info(request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    u_sess = session_manager.get_or_create_user(user["id"])
    key = u_sess.get_gemini_key()
    return {
        "ok": True,
        "is_configured": bool(key),
        "masked_key": (key[:4] + "..." + key[-4:]) if len(key) >= 8 else ("登録済み" if key else ""),
    }


@app.post("/api/gemini-key")
async def set_gemini_key(request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    body = await request.json()
    key = str(body.get("api_key", "")).strip()
    u_sess = session_manager.get_or_create_user(user["id"])
    u_sess.save_gemini_key(key)
    return {"ok": True, "is_configured": bool(key)}


@app.post("/api/gemini-key/test")
async def test_gemini_key(request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    u_sess = session_manager.get_or_create_user(user["id"])
    key = u_sess.get_gemini_key()
    if not key:
        return {"ok": False, "error": "Gemini API キーが未登録です"}
    try:
        from google import genai
        client = genai.Client(api_key=key)
        # 接続確認: gemini-2.5-flash / gemini-2.0-flash / gemini-1.5-flash 等で簡易テキスト生成
        resp = client.models.generate_content(
            model="gemini-2.5-flash",
            contents="ping",
        )
        return {"ok": True, "message": "✔ Gemini API キーの接続確認に成功しました！"}
    except Exception as e:
        return {"ok": False, "error": f"接続テスト失敗: {e}"}


@app.post("/api/youtube-grow")
async def start_youtube_grow(request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    body = await request.json()
    url = str(body.get("url", "")).strip()
    max_videos = int(body.get("max_videos", 5))
    if not url:
        return JSONResponse({"ok": False, "error": "YouTube URL を入力してください"}, status_code=400)

    uid = user["id"]
    u_sess = session_manager.get_or_create_user(uid)
    key = u_sess.get_gemini_key()
    if not key:
        return JSONResponse(
            {"ok": False, "error": "Gemini API キーが登録されていません。先に上の設定欄から API キーを保存してください。"},
            status_code=400
        )

    with youtube_grow_lock:
        cur = youtube_grow_tasks.get(uid)
        if cur and cur.get("status") == "running":
            return JSONResponse({"ok": False, "error": "現在別の辞書育成タスクが実行中です"}, status_code=409)

        youtube_grow_tasks[uid] = {
            "status": "starting",
            "progress": 0.0,
            "message": "タスクを開始中...",
            "logs": [],
            "added_count": 0,
        }

    t = threading.Thread(target=_run_youtube_grow_worker, args=(uid, url, max_videos, key), daemon=True)
    t.start()
    return {"ok": True, "message": "辞書育成タスクを開始しました"}


@app.get("/api/youtube-grow/status")
async def get_youtube_grow_status(request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    uid = user["id"]
    with youtube_grow_lock:
        task = youtube_grow_tasks.get(uid, {
            "status": "idle",
            "progress": 0.0,
            "message": "待機中",
            "logs": [],
            "added_count": 0,
        })
        return {"ok": True, "task": task}


# ---------------------------------------------------------------------------
# Web マイク入力画面 & WebSocket 音声受信
# ---------------------------------------------------------------------------

@app.websocket("/ws/mic")
@app.websocket("/ingest")
async def websocket_mic_endpoint(websocket: WebSocket):
    session_cookie = websocket.cookies.get(SESSION_COOKIE_NAME)
    user = verify_session_token(session_cookie) if session_cookie else None
    if not user:
        await websocket.close(code=1008, reason="Unauthorized")
        return

    uid = user["id"]
    u_sess = session_manager.get_or_create_user(uid)
    await websocket.accept()
    logger.info("WebSocket mic connected: %s (%s)", user.get("username"), uid)

    if shared_asr_engine is None:
        await websocket.send_json({"type": "error", "message": "ASR engine is not initialized"})
        await websocket.close()
        return

    # イベントループへの安全な非同期コールバック
    loop = asyncio.get_running_loop()

    # 初期エンジン情報を通知
    asr_model = u_sess.settings.get("asr_model", "haas")
    model_disp = "Haas-ja-v1.0 (ONNX)" if asr_model == "haas" else "ReazonSpeech (Zipformer)"
    await websocket.send_json({
        "type": "engine_info",
        "engine": asr_model,
        "name": model_disp,
    })

    async def _handle_async_translation(text: str, target_langs: list, gemini_key: str):
        for lang in target_langs:
            try:
                trans_text = await loop.run_in_executor(None, translate_text, text, lang, gemini_key)
                if trans_text:
                    await u_sess.broadcast_translation(lang, trans_text, original=text)
                    try:
                        await websocket.send_json({"type": "translation", "lang": lang, "text": trans_text})
                    except Exception:
                        pass
            except Exception as e:
                logger.warning("Translation dispatch error for %s: %s", lang, e)

    def on_transcript(raw_text: str, is_final: bool = True, has_punct: bool = False, latency_ms: float = 0.0, client_ts: Optional[float] = None):
        processed = raw_text

        # 1. ITN (数値・日付・単位の正規化)
        if u_sess.settings.get("enable_itn", True):
            try:
                import itn_cjk
                processed = itn_cjk.convert(processed, "ja")
            except Exception as e:
                logger.debug("ITN failed: %s", e)

        # 2. 句読点付与 (、。) - 確定時かつモデル自前の句読点がない場合のみ
        if is_final and u_sess.settings.get("enable_punct", True) and not has_punct and shared_punctuator:
            try:
                processed = shared_punctuator.restore(processed)
            except Exception as e:
                logger.debug("Punctuation failed: %s", e)

        # 3. ユーザー固有辞書の適用と OBS への送信
        asyncio.run_coroutine_threadsafe(u_sess.broadcast_transcript(processed, is_final), loop)

        # 4. マイク画面へのフィードバック (確定または途中追随、応答ms通知)
        refined = u_sess.apply_replacements(processed)
        resp_data = {
            "type": "transcript",
            "text": refined,
            "is_final": is_final,
        }
        if is_final and latency_ms > 0:
            resp_data["latency_ms"] = round(latency_ms, 1)
        if client_ts is not None:
            resp_data["client_ts"] = client_ts

        asyncio.run_coroutine_threadsafe(
            websocket.send_json(resp_data),
            loop
        )

        # 5. 確定時の多言語翻訳 (有効化されている場合)
        if is_final and u_sess.settings.get("enable_trans", False):
            target_langs = u_sess.settings.get("trans_target_langs", ["en"])
            gemini_key = u_sess.get_gemini_key()
            if target_langs:
                asyncio.run_coroutine_threadsafe(
                    _handle_async_translation(refined, target_langs, gemini_key),
                    loop
                )

    vad_session = UserVadSession(
        uid,
        shared_asr_engine,
        on_transcript,
        model_choice=u_sess.settings.get("asr_model", "haas"),
    )
    u_sess.active_vad_session = vad_session

    try:
        while True:
            # ブラウザから送信された Float32 PCM (16kHz mono) バイナリ
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                break
            if "bytes" in message and message["bytes"]:
                raw_bytes = message["bytes"]
                client_ts = None
                # パケットヘッダー判定: b"HYM1" (4バイト) + Float64 little-endian (8バイト) = 12バイト
                if len(raw_bytes) >= 12 and raw_bytes[:4] == b"HYM1":
                    try:
                        client_ts = struct.unpack("<d", raw_bytes[4:12])[0]
                        audio_bytes = raw_bytes[12:]
                    except Exception:
                        audio_bytes = raw_bytes
                else:
                    audio_bytes = raw_bytes

                # 4バイト float32 配列としてデコード
                chunk = np.frombuffer(audio_bytes, dtype=np.float32)
                vad_session.process_audio_chunk(chunk, client_ts=client_ts)
            elif "text" in message:
                pass
    except (WebSocketDisconnect, RuntimeError):
        logger.info("WebSocket mic disconnected: %s", uid)
    except Exception as e:
        logger.warning("WebSocket mic error (%s): %s", uid, e)
    finally:
        u_sess.active_vad_session = None
        try:
            vad_session.reset()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# OBS ブラウザソース用 オーバーレイ & SSE 配信
# ---------------------------------------------------------------------------

@app.get("/overlay", response_class=HTMLResponse)
async def overlay_page(request: Request, token: str = ""):
    u_sess = session_manager.get_user_by_obs_token(token)
    if not u_sess:
        return templates.TemplateResponse(
            request=request,
            name="overlay.html",
            context={"error": "無効な OBS トークンです。ダッシュボードから正しい URL を取得してください。"},
            status_code=403,
        )
    return templates.TemplateResponse(
        request=request,
        name="overlay.html",
        context={"token": token, "style": u_sess.style, "error": None},
    )


@app.get("/events")
async def sse_events(request: Request, token: str = ""):
    u_sess = session_manager.get_user_by_obs_token(token)
    if not u_sess:
        raise HTTPException(status_code=403, detail="Invalid token")

    q = u_sess.subscribe_obs()
    logger.info("OBS client connected for %s (active clients: %d)", u_sess.user_id, len(u_sess.obs_subscribers))

    async def event_generator():
        try:
            # 初期接続確認イベント
            yield f": keepalive connected\n\n"
            while True:
                # ユーザーの発話確定イベント、または 15 秒おきの keepalive
                try:
                    event = await asyncio.wait_for(q.get(), timeout=15.0)
                    yield event
                except asyncio.TimeoutError:
                    yield f": keepalive\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            u_sess.unsubscribe_obs(q)
            logger.info("OBS client disconnected for %s", u_sess.user_id)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("cloud.gateway:app", host=HOST, port=PORT, reload=False)
