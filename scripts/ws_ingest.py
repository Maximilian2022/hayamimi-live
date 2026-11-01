"""WebSocket audio ingest for realtime_transcribe.py's --input ws mode.

Protocol:
  1. HTTP upgrade to ws://host:port/ingest (RFC 6455 handshake).
  2. First WS message: one JSON text frame,
     {"sr": 16000, "format": "pcm_s16le", "channels": 1}.
  3. After that: binary frames of raw PCM (little-endian s16), one message
     per chunk, sent continuously for the life of the connection.
  4. Server replies with JSON text frames carrying the same subtitle events
     the SSE dashboard sees (partial/final/translation/refine), so a
     stackchan-class client can show its own results if it wants to. This
     also means audio sent to /ingest shows up on the existing /dashboard
     and OBS overlay for free -- both read off the same SubtitleServer.

Only one audio-producing client is accepted at a time; a second connection
gets a JSON {"type": "error", ...} frame and is closed immediately, keeping
the server dead simple (no need to mix multiple mic streams). A client that
disconnects just leaves the ingest queue idle -- the pipeline and server
keep running, and the next connection resumes feeding it.
"""
import sys
# Windows Defender の「コントロールされたフォルダー アクセス」(Controlled Folder Access)
# による OneDrive/保護フォルダー配下での __pycache__ 書き込みブロックを根本から防止
sys.dont_write_bytecode = True

# Windows cmd.exe リダイレクト環境での日本語ログ文字化け (CP932) を完全防止
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
if hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import asyncio
import json
import queue
import socket
import threading

import numpy as np

from audio_utils import decode_pcm16, resample_linear
from ws_protocol import (OP_BINARY, OP_CLOSE, OP_PING, OP_PONG, OP_TEXT,
                         build_handshake_response, decode_frame, encode_frame,
                         parse_handshake_request)

READ_CHUNK = 4096
MAX_HEADER_BYTES = 16384
MAX_BUFFER_BYTES = 8 * 1024 * 1024  # 8MB max buffer limit against slowloris / memory bomb
INGEST_PATH = "/ingest"

# Sentinel pushed onto audio_q when a streaming client disconnects, so the
# pipeline can flush its in-progress VAD segment instead of waiting on
# silence that may never come once the client is gone (see
# realtime_transcribe.run_stream(), which treats any non-ndarray item on the
# queue as "flush now"). Any object works; identity is never compared.
FLUSH = object()


AUDIO_QUEUE_MAXSIZE = 200


import ipaddress

def _is_safe_origin(origin: str | None, host: str | None = None) -> bool:
    """Allow connections without an Origin header (non-browser clients like ESP32/curl)
    or matching localhost, 127.0.0.1, or RFC1918 private IP literals.
    Rejects cross-site WebSocket hijacking from third-party websites (e.g. 10.evil.example).
    """
    if not origin:
        return True
    from urllib.parse import urlparse
    try:
        parsed = urlparse(origin)
        ohost = (parsed.hostname or "").lower().strip("[]")
        if ohost in ("localhost", "127.0.0.1", "::1"):
            return True
        ip = ipaddress.ip_address(ohost)
        return ip.is_private or ip.is_loopback
    except Exception:
        return False


class IngestServer:
    """Accepts PCM over a WS connection, exposes it as a chunk queue.

    `audio_q` yields whatever-sized float32 mono arrays the network handed
    us, already resampled to `sample_rate`; realtime_transcribe.py's
    ws_chunks() re-chunks that into fixed VAD windows.
    """

    def __init__(self, host: str, port: int, sample_rate: int = 16000, subtitle_server=None):
        self.host = host
        self.port = port
        self.sample_rate = sample_rate
        self.subtitle_server = subtitle_server
        self.audio_q: "queue.Queue[np.ndarray]" = queue.Queue(maxsize=AUDIO_QUEUE_MAXSIZE)
        self._active = False
        self._current_writer: asyncio.StreamWriter | None = None
        self._session_id = 0
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None

    def _push_audio(self, item):
        """Put into audio_q without blocking forever; drop oldest chunk if full."""
        try:
            self.audio_q.put_nowait(item)
        except queue.Full:
            try:
                self.audio_q.get_nowait()
            except queue.Empty:
                pass
            try:
                self.audio_q.put_nowait(item)
            except queue.Full:
                pass

    def start(self) -> "IngestServer":
        ready = threading.Event()
        error: list[Exception] = []

        def run():
            try:
                self._loop = asyncio.new_event_loop()
                asyncio.set_event_loop(self._loop)
                self._loop.create_task(self._serve(ready))
                self._loop.run_forever()
            except Exception as exc:  # surfaced to the caller via ready.wait
                error.append(exc)
                ready.set()

        threading.Thread(target=run, daemon=True, name="ws-ingest").start()
        ready.wait(timeout=5)
        if error:
            raise error[0]
        return self

    async def _serve(self, ready: threading.Event):
        server = await asyncio.start_server(self._handle, self.host, self.port)
        ready.set()
        async with server:
            await server.serve_forever()

    async def _read_handshake_head(self, reader: asyncio.StreamReader) -> bytes | None:
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = await reader.read(READ_CHUNK)
            if not chunk:
                return None
            buf += chunk
            if len(buf) > MAX_HEADER_BYTES:  # runaway header: bail
                return None
        return buf.split(b"\r\n\r\n", 1)[0]

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            sock = writer.get_extra_info("socket")
            if sock is not None:
                try:
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
                except Exception:
                    pass

            head = await self._read_handshake_head(reader)
            if head is None:
                writer.close()
                return
            req = parse_handshake_request(head)
            if req is None or req["path"] != INGEST_PATH:
                writer.write(b"HTTP/1.1 404 Not Found\r\n\r\n")
                await writer.drain()
                writer.close()
                return
            if not _is_safe_origin(req.get("origin"), req.get("host")):
                writer.write(b"HTTP/1.1 403 Forbidden\r\n\r\nCross-Origin WebSocket not allowed\r\n")
                await writer.drain()
                writer.close()
                return
            writer.write(build_handshake_response(req["key"]))
            await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError, OSError):
            writer.close()
            return

        # 最新の接続を最優先 (Preemption / Takeover)
        # 前回の接続が残っている場合 (リロード、別タブ、ネットワーク瞬断によるゾンビ等)、
        # 古い接続を安全に切断して新しいクライアントを受け入れる
        old_writer = None
        with self._lock:
            self._session_id += 1
            my_session = self._session_id
            if self._active and self._current_writer is not None:
                old_writer = self._current_writer
            self._current_writer = writer
            self._active = True

        if old_writer is not None and old_writer != writer:
            try:
                await self._send_json(old_writer, {
                    "type": "error",
                    "message": "新しいマイククライアントが接続されたため切断されました (preempted)"
                })
            except (ConnectionError, OSError):
                pass
            try:
                old_writer.close()
            except Exception:
                pass

        forward_task = None
        got_handshake = False
        try:
            if self.subtitle_server is not None:
                forward_task = asyncio.ensure_future(self._forward_events(writer))

            buf = bytearray()
            channels = 1
            src_rate = self.sample_rate

            while True:
                data = await reader.read(READ_CHUNK)
                if not data:
                    break
                buf += data
                if len(buf) > MAX_BUFFER_BYTES:
                    break  # runaway payload / buffer overflow guard
                while True:
                    try:
                        result = decode_frame(bytes(buf))
                    except ValueError:
                        return  # frame declared oversized -> close immediately
                    if result is None:
                        break
                    opcode, payload, consumed = result
                    del buf[:consumed]

                    if opcode == OP_CLOSE:
                        return
                    elif opcode == OP_PING:
                        writer.write(encode_frame(payload, opcode=OP_PONG))
                        await writer.drain()
                    elif opcode == OP_PONG:
                        pass
                    elif opcode == OP_TEXT and not got_handshake:
                        try:
                            cfg = json.loads(payload.decode("utf-8"))
                            src_rate = int(cfg.get("sr", self.sample_rate))
                            channels = max(1, int(cfg.get("channels", 1)))
                            fmt = cfg.get("format", "pcm_s16le")
                        except (ValueError, UnicodeDecodeError, TypeError):
                            await self._send_json(writer, {"type": "error",
                                                            "message": "bad handshake JSON"})
                            return
                        if fmt != "pcm_s16le":
                            await self._send_json(writer, {"type": "error",
                                                            "message": f"unsupported format "
                                                                       f"{fmt!r}, only pcm_s16le"})
                            return
                        got_handshake = True
                        await self._send_json(writer, {"type": "ready", "sr": self.sample_rate})
                    elif opcode == OP_BINARY:
                        if not got_handshake:
                            continue  # audio before the JSON handshake: drop and wait
                        samples = decode_pcm16(payload, channels)
                        if len(samples) == 0:
                            continue
                        if src_rate != self.sample_rate:
                            samples = resample_linear(samples, src_rate, self.sample_rate)
                        self._push_audio(samples)
                    # OP_TEXT after handshake / OP_CONT: ignored, not part of this protocol
        except (ConnectionError, asyncio.IncompleteReadError, OSError):
            pass
        finally:
            if forward_task is not None:
                forward_task.cancel()
            with self._lock:
                if self._session_id == my_session:
                    self._active = False
                    self._current_writer = None
                    if got_handshake:
                        self._push_audio(FLUSH)
            try:
                writer.close()
            except Exception:
                pass

    async def _send_json(self, writer: asyncio.StreamWriter, obj: dict):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        writer.write(encode_frame(data, opcode=OP_TEXT))
        await writer.drain()

    async def _forward_events(self, writer: asyncio.StreamWriter):
        """Mirror the SubtitleServer's broadcast (partial/final/...) onto
        this WS connection, the same events the SSE dashboard receives.

        Uses polling with timeout + closed flag to ensure background threads
        do not leak or block indefinitely in ThreadPoolExecutor upon disconnect.
        """
        q = self.subtitle_server.subscribe()
        loop = asyncio.get_event_loop()
        closed = False

        def get_item():
            while not closed:
                try:
                    return q.get(timeout=0.5)
                except queue.Empty:
                    continue
            return None

        try:
            while True:
                data = await loop.run_in_executor(None, get_item)
                if data is None:
                    break
                writer.write(encode_frame(data.encode("utf-8"), opcode=OP_TEXT))
                await writer.drain()
        except (ConnectionError, asyncio.CancelledError, OSError):
            pass
        finally:
            closed = True
            try:
                q.put_nowait(None)
            except Exception:
                pass
            self.subtitle_server.unsubscribe(q)
