"""
cloud/gateway.py
hayamimi-cloud マルチユーザー・Discord OAuth2 認証 ASR ゲートウェイ
FastAPI + WebSocket + SSE によるリアルタイム音声認識 & OBS 字幕配信
"""

import asyncio
import logging
import os
import sys
from contextlib import asynccontextmanager
from typing import Optional

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
from cloud.engine import SharedAsrEngine, UserVadSession

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
# ダッシュボード & 設定
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    uid = user["id"]
    u_sess = session_manager.get_or_create_user(
        uid, username=user.get("username", ""), display_name=user.get("global_name", "")
    )

    obs_url = f"{BASE_URL}/overlay?token={u_sess.obs_token}"
    dict_text = u_sess.get_replacements_raw()

    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "user": user,
            "obs_url": obs_url,
            "dictionary_text": dict_text,
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


# ---------------------------------------------------------------------------
# Web マイク入力画面 & WebSocket 音声受信
# ---------------------------------------------------------------------------

@app.get("/mic", response_class=HTMLResponse)
async def mic_page(request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    return templates.TemplateResponse(
        request=request, name="mic.html", context={"user": user}
    )


@app.websocket("/ws/mic")
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

    def on_transcript(raw_text: str, is_final: bool = True):
        # ユーザー固有辞書の適用と OBS への送信
        asyncio.run_coroutine_threadsafe(u_sess.broadcast_transcript(raw_text, is_final), loop)
        # マイク画面へのフィードバック
        refined = u_sess.apply_replacements(raw_text)
        asyncio.run_coroutine_threadsafe(
            websocket.send_json({"type": "transcript", "text": refined, "is_final": is_final}),
            loop
        )

    vad_session = UserVadSession(uid, shared_asr_engine, on_transcript)

    try:
        while True:
            # ブラウザから送信された Float32 PCM (16kHz mono) バイナリ
            message = await websocket.receive()
            if "bytes" in message and message["bytes"]:
                raw_bytes = message["bytes"]
                # 4バイト float32 配列としてデコード
                chunk = np.frombuffer(raw_bytes, dtype=np.float32)
                vad_session.process_audio_chunk(chunk)
            elif "text" in message:
                pass
    except WebSocketDisconnect:
        logger.info("WebSocket mic disconnected: %s", uid)
    except Exception as e:
        logger.warning("WebSocket mic error (%s): %s", uid, e)
    finally:
        vad_session.reset()


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
        request=request, name="overlay.html", context={"token": token, "error": None}
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
