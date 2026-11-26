"""cloud/translator.py と cloud/gateway.py (/ws/stream 音声変換 & POST /api/translate) の単体テスト."""

import asyncio
import importlib
import importlib.util
import struct
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pytest

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from cloud import translator as tr  # noqa: E402


# ---------------------------------------------------------------------------
# cloud.translator テスト
# ---------------------------------------------------------------------------


def test_detect_language_basic():
    assert tr.detect_language("こんにちは、今日の配信へようこそ！") == "ja"
    assert tr.detect_language("カタカナテスト") == "ja"
    assert tr.detect_language("안녕하세요 반갑습니다") == "ko"
    assert tr.detect_language("你好世界，今天天气很好") == "zh"
    assert tr.detect_language("Hello everyone, welcome to the stream!") == "en"
    assert tr.detect_language("Hola amigos, muchas gracias por venir") == "es"
    assert tr.detect_language("Bonjour tout le monde, merci beaucoup") == "fr"


def test_digits_consistent():
    assert tr.digits_consistent("今日は123個の注文です", "There are 123 orders today")
    assert not tr.digits_consistent("スコアは100点です", "Score is 90 points")
    assert tr.digits_consistent("数字を含まない文", "A sentence without digits")


def test_normalize_lang():
    pool = tr.LocalTranslatorPool()
    assert pool._normalize_lang("en") == "en"
    assert pool._normalize_lang("ja") == "ja"
    assert pool._normalize_lang("zh-CN") == "zh"
    assert pool._normalize_lang("zh_TW") == "zh_tw"
    assert pool._normalize_lang("KO") == "ko"


def test_translate_bidirectional_ja_to_multi(monkeypatch):
    calls = []

    def fake_translate(text: str, target_lang: str, source_lang: str = "ja") -> str:
        calls.append((text, target_lang, source_lang))
        return f"[{source_lang}->{target_lang}] {text}"

    monkeypatch.setattr(tr.local_translator_pool, "translate", fake_translate)

    res = tr.translate_bidirectional("おはようございます", target_langs=["en", "ko"])
    assert res["source_lang"] == "ja"
    assert "translations" in res
    assert res["translations"]["en"] == "[ja->en] おはようございます"
    assert res["translations"]["ko"] == "[ja->ko] おはようございます"
    assert len(calls) == 2


def test_translate_bidirectional_default_target_and_foreign_to_ja(monkeypatch):
    calls = []

    def fake_translate(text: str, target_lang: str, source_lang: str = "ja") -> str:
        calls.append((text, target_lang, source_lang))
        return f"[{source_lang}->{target_lang}] {text}"

    monkeypatch.setattr(tr.local_translator_pool, "translate", fake_translate)

    # 日本語かつ target_langs 未指定は ["en"]
    res_ja = tr.translate_bidirectional("テストです")
    assert res_ja["source_lang"] == "ja"
    assert list(res_ja["translations"].keys()) == ["en"]

    # 英語入力は自動判定で ja へ
    calls.clear()
    res_en = tr.translate_bidirectional("Good morning everyone")
    assert res_en["source_lang"] == "en"
    assert res_en["translations"]["ja"] == "[en->ja] Good morning everyone"

    # source_lang 明示指定
    calls.clear()
    res_explicit = tr.translate_bidirectional("Hello", source_lang="fr")
    assert res_explicit["source_lang"] == "fr"
    assert res_explicit["translations"]["ja"] == "[fr->ja] Hello"


# ---------------------------------------------------------------------------
# cloud.gateway テスト (fastapi 未導入環境向けのスタブローダ付き)
# ---------------------------------------------------------------------------


def _load_gateway_module(monkeypatch):
    """fastapi が無い環境でも cloud.gateway をインポートできるようにする。"""
    if importlib.util.find_spec("fastapi") is not None:
        return importlib.import_module("cloud.gateway")

    class _DummyFastAPI:
        def __init__(self, *args, **kwargs):
            self.routes = {}

        def add_middleware(self, *args, **kwargs):
            return None

        def mount(self, *args, **kwargs):
            return None

        def on_event(self, *args, **kwargs):
            def _dec(fn):
                return fn
            return _dec

        def middleware(self, *args, **kwargs):
            def _dec(fn):
                return fn
            return _dec

        def _route_dec(self, path, *args, **kwargs):
            def _dec(fn):
                self.routes[path] = fn
                return fn
            return _dec

        get = _route_dec
        post = _route_dec
        websocket = _route_dec

    class _DummyJSONResponse(dict):
        def __init__(self, content=None, status_code=200, **kwargs):
            super().__init__(content=content, status_code=status_code)
            self.content = content
            self.status_code = status_code

    class _DummyHTTPException(Exception):
        def __init__(self, status_code: int, detail: str = ""):
            super().__init__(detail)
            self.status_code = status_code
            self.detail = detail

    class _DummyWebSocketDisconnect(Exception):
        pass

    fastapi_mod = types.ModuleType("fastapi")
    fastapi_mod.__path__ = []
    fastapi_mod.FastAPI = _DummyFastAPI
    fastapi_mod.WebSocket = object
    fastapi_mod.WebSocketDisconnect = _DummyWebSocketDisconnect
    fastapi_mod.Request = object
    fastapi_mod.Response = object
    fastapi_mod.HTTPException = _DummyHTTPException
    fastapi_mod.Query = lambda default=None, **kwargs: default
    fastapi_mod.Header = lambda default=None, **kwargs: default
    fastapi_mod.Depends = lambda dep=None: dep
    fastapi_mod.UploadFile = object
    fastapi_mod.File = lambda default=None, **kwargs: default
    fastapi_mod.Form = lambda default=None, **kwargs: default
    fastapi_mod.status = types.SimpleNamespace(
        WS_1008_POLICY_VIOLATION=1008,
        HTTP_400_BAD_REQUEST=400,
        HTTP_401_UNAUTHORIZED=401,
    )

    fastapi_mw = types.ModuleType("fastapi.middleware")
    fastapi_cors = types.ModuleType("fastapi.middleware.cors")
    fastapi_cors.CORSMiddleware = object

    fastapi_resp = types.ModuleType("fastapi.responses")
    fastapi_resp.JSONResponse = _DummyJSONResponse
    fastapi_resp.RedirectResponse = object
    fastapi_resp.StreamingResponse = object
    fastapi_resp.PlainTextResponse = object
    fastapi_resp.HTMLResponse = object

    fastapi_ws = types.ModuleType("fastapi.websockets")
    fastapi_ws.WebSocketState = types.SimpleNamespace(CONNECTED=1)

    fastapi_static = types.ModuleType("fastapi.staticfiles")
    fastapi_static.StaticFiles = lambda *args, **kwargs: None

    fastapi_tpl = types.ModuleType("fastapi.templating")
    fastapi_tpl.Jinja2Templates = lambda *args, **kwargs: types.SimpleNamespace(TemplateResponse=lambda *a, **k: None)

    if importlib.util.find_spec("httpx") is None:
        monkeypatch.setitem(sys.modules, "httpx", types.ModuleType("httpx"))

    for name, mod in [
        ("fastapi", fastapi_mod),
        ("fastapi.middleware", fastapi_mw),
        ("fastapi.middleware.cors", fastapi_cors),
        ("fastapi.responses", fastapi_resp),
        ("fastapi.websockets", fastapi_ws),
        ("fastapi.staticfiles", fastapi_static),
        ("fastapi.templating", fastapi_tpl),
    ]:
        monkeypatch.setitem(sys.modules, name, mod)

    # 先にダミーモジュールがあれば再ロード
    sys.modules.pop("cloud.gateway", None)
    mod = importlib.import_module("cloud.gateway")
    return mod


def test_convert_stream_audio_chunk_48k_stereo_s16le(monkeypatch):
    gw = _load_gateway_module(monkeypatch)
    # 0.1 秒 @ 48kHz stereo s16le = 4800 frames * 2 channels
    t = np.arange(4800, dtype=np.float32) / 48000.0
    sine = (np.sin(2.0 * np.pi * 440.0 * t) * 16000.0).astype(np.int16)
    stereo = np.column_stack([sine, sine]).reshape(-1)
    raw = stereo.tobytes()

    out = gw._convert_stream_audio_chunk(raw, sample_rate=48000, channels=2, audio_format="s16le")
    assert isinstance(out, np.ndarray)
    assert out.dtype == np.float32
    assert out.ndim == 1
    # 48kHz 0.1s -> 16kHz 0.1s (= 1600 samples)
    assert abs(len(out) - 1600) <= 2
    assert np.max(np.abs(out)) > 0.1


def test_convert_stream_audio_chunk_16k_mono_f32le(monkeypatch):
    gw = _load_gateway_module(monkeypatch)
    samples = np.array([0.25, -0.5, 0.75, -0.25], dtype=np.float32)
    raw = struct.pack("<4f", *samples.tolist())

    out = gw._convert_stream_audio_chunk(raw, sample_rate=16000, channels=1, audio_format="f32le")
    assert out.dtype == np.float32
    assert np.allclose(out, samples, atol=1e-6)


def test_handle_translate_api_success_and_validation(monkeypatch):
    gw = _load_gateway_module(monkeypatch)

    monkeypatch.setattr(
        gw,
        "translate_bidirectional",
        lambda text, target_langs=None, source_lang=None: {
            "source_lang": source_lang or "ja",
            "translations": {"en": "Hello"},
        },
        raising=False,
    )
    # cloud.translator 直接参照されている場合に備える
    monkeypatch.setattr(
        tr,
        "translate_bidirectional",
        lambda text, target_langs=None, source_lang=None: {
            "source_lang": source_lang or "ja",
            "translations": {"en": "Hello"},
        },
    )

    class _MockRequest:
        def __init__(self, payload):
            self._payload = payload
            self.headers = {}

        async def json(self):
            return self._payload

    res = asyncio.run(gw.handle_translate_api(_MockRequest({"text": "こんにちは", "target_langs": ["en"]})))
    # JSONResponse または dict のどちらにも対応
    body = getattr(res, "content", None) if hasattr(res, "content") else res
    if hasattr(res, "body"):
        import json
        body = json.loads(res.body.decode("utf-8"))
    assert body["source_lang"] == "ja"
    assert body["translations"]["en"] == "Hello"

    # 空テキスト時は 400 になること
    try:
        err_res = asyncio.run(gw.handle_translate_api(_MockRequest({"text": "   "})))
        status_code = getattr(err_res, "status_code", 200)
        assert status_code == 400
    except Exception as exc:
        assert getattr(exc, "status_code", None) == 400
