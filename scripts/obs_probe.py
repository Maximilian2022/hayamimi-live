"""
scripts/obs_probe.py
OBS Studio WebSocket v5 疎通診断 & 認証検証モジュール

OBS WebSocket v5 (OBS Studio 28 以降標準搭載) の稼働状態、ポート到達性、
およびパスワード認証の整合性を Python 標準ライブラリのみで高速診断します。
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import socket
import struct
import sys
import urllib.parse
from typing import Any, Dict, Optional, Tuple

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

logger = logging.getLogger("hayamimi.obs_probe")


def compute_obs_auth(password: str, salt: str, challenge: str) -> str:
    """OBS WebSocket v5 の SHA-256 認証ハッシュを計算する。"""
    s1 = base64.b64encode(hashlib.sha256((password + salt).encode("utf-8")).digest()).decode("utf-8")
    s2 = base64.b64encode(hashlib.sha256((s1 + challenge).encode("utf-8")).digest()).decode("utf-8")
    return s2


def parse_ws_url(url: str) -> Tuple[str, int]:
    """ws:// または wss:// URL からホスト名とポート番号を抽出する。"""
    raw = url.strip()
    if not raw.startswith("ws://") and not raw.startswith("wss://") and not raw.startswith("http://") and not raw.startswith("https://"):
        raw = "ws://" + raw
    parsed = urllib.parse.urlparse(raw)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or (443 if parsed.scheme in ("wss", "https") else 4455)
    return host, port


class WsReader:
    """バッファリング対応 WebSocket フレームリーダー"""

    def __init__(self, sock: socket.socket, initial_buf: bytes = b""):
        self.sock = sock
        self.buf = bytearray(initial_buf)

    def recv_exact(self, n: int) -> bytes:
        while len(self.buf) < n:
            chunk = self.sock.recv(max(4096, n - len(self.buf)))
            if not chunk:
                raise ConnectionResetError("Socket closed prematurely")
            self.buf.extend(chunk)
        res = bytes(self.buf[:n])
        del self.buf[:n]
        return res

    def read_frame(self) -> Tuple[int, bytes]:
        """WebSocket フレームを1つ読み取って (opcode, payload) を返す。"""
        h = self.recv_exact(2)
        b0, b1 = h[0], h[1]
        opcode = b0 & 0x0F
        is_masked = bool(b1 & 0x80)
        pay_len = b1 & 0x7F

        if pay_len == 126:
            ext = self.recv_exact(2)
            pay_len = struct.unpack("!H", ext)[0]
        elif pay_len == 127:
            ext = self.recv_exact(8)
            pay_len = struct.unpack("!Q", ext)[0]

        mask_key = self.recv_exact(4) if is_masked else None
        payload = self.recv_exact(pay_len)

        if mask_key:
            unmasked = bytearray(pay_len)
            for i in range(pay_len):
                unmasked[i] = payload[i] ^ mask_key[i % 4]
            payload = bytes(unmasked)

        return opcode, payload


def _send_ws_text_frame(sock: socket.socket, text: str) -> None:
    """クライアントからサーバーへの WebSocket テキストフレーム（マスク必須）を送信する。"""
    data = text.encode("utf-8")
    pay_len = len(data)
    frame = bytearray()
    frame.append(0x81)  # FIN=1, Opcode=1 (Text)

    mask_key = os.urandom(4)
    if pay_len < 126:
        frame.append(0x80 | pay_len)
    elif pay_len < 65536:
        frame.append(0x80 | 126)
        frame.extend(struct.pack("!H", pay_len))
    else:
        frame.append(0x80 | 127)
        frame.extend(struct.pack("!Q", pay_len))

    frame.extend(mask_key)
    masked = bytearray(pay_len)
    for i in range(pay_len):
        masked[i] = data[i] ^ mask_key[i % 4]
    frame.extend(masked)
    sock.sendall(frame)


def probe_obs_connection(
    url: str = "ws://127.0.0.1:4455",
    password: str = "",
    timeout: float = 2.0,
    log_to_stderr: bool = True,
) -> Dict[str, Any]:
    """
    OBS WebSocket v5 サーバーへの疎通および認証を診断する。
    
    戻り値:
        {
            "ok": bool,
            "status": str,
            "host": str,
            "port": int,
            "obs_version": str | None,
            "rpc_version": int | None,
            "auth_required": bool,
            "message": str,
        }
    """
    try:
        host, port = parse_ws_url(url)
    except Exception as e:
        msg = f"URLの書式が無効です ({url}): {e}"
        if log_to_stderr:
            sys.stderr.write(f"[OBS-Probe] ❌ {msg}\n")
        return {
            "ok": False,
            "status": "invalid_url",
            "host": "unknown",
            "port": 0,
            "obs_version": None,
            "rpc_version": None,
            "auth_required": False,
            "message": msg,
        }

    res: Dict[str, Any] = {
        "ok": False,
        "status": "error",
        "host": host,
        "port": port,
        "obs_version": None,
        "rpc_version": None,
        "auth_required": False,
        "message": "",
    }

    if log_to_stderr:
        sys.stderr.write(f"[OBS-Probe] 接続診断を開始します: {host}:{port} (timeout={timeout}s)...\n")

    # 1. TCP ソケット接続テスト
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect((host, port))
    except socket.timeout:
        sock.close()
        msg = f"ポート {port} への接続がタイムアウトしました。ファイアウォールまたは OBS の設定を確認してください。"
        res["status"] = "timeout"
        res["message"] = msg
        if log_to_stderr:
            sys.stderr.write(f"[OBS-Probe] ⏱️ {msg}\n")
        return res
    except (ConnectionRefusedError, socket.error) as e:
        sock.close()
        err_str = str(e)
        if (
            isinstance(e, ConnectionRefusedError)
            or "refused" in err_str.lower()
            or "拒否" in err_str
            or "無効" in err_str
            or getattr(e, "errno", None) in (10061, 111)
            or getattr(e, "winerror", None) == 10061
        ):
            msg = (
                f"ポート {port} への接続が拒否されました。"
                f"OBS Studio が起動していないか、[ツール] -> [WebSocket サーバー設定] で"
                f"「WebSocket サーバーを有効にする」が無効になっている可能性があります。"
            )
            res["status"] = "connection_refused"
        else:
            msg = f"ポート {port} への接続に失敗しました: {err_str}"
            res["status"] = "connect_error"
        res["message"] = msg
        if log_to_stderr:
            sys.stderr.write(f"[OBS-Probe] ❌ {msg}\n")
        return res

    # 2. HTTP WebSocket Upgrade ハンドシェイク
    try:
        handshake = (
            f"GET / HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            f"Upgrade: websocket\r\n"
            f"Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
            f"Sec-WebSocket-Version: 13\r\n"
            f"\r\n"
        )
        sock.sendall(handshake.encode("utf-8"))

        # HTTP レスポンスヘッダー受信
        resp_buf = bytearray()
        while b"\r\n\r\n" not in resp_buf:
            c = sock.recv(1024)
            if not c:
                break
            resp_buf.extend(c)
            if len(resp_buf) > 8192:
                break

        header_part, _, remaining = bytes(resp_buf).partition(b"\r\n\r\n")
        header_text = header_part.decode("utf-8", errors="replace")
        if "101" not in header_text.splitlines()[0]:
            sock.close()
            msg = f"OBS WebSocket サーバーから予期しないレスポンスが返されました (HTTP 101 以外): {header_text.splitlines()[0]}"
            res["status"] = "handshake_failed"
            res["message"] = msg
            if log_to_stderr:
                sys.stderr.write(f"[OBS-Probe] ❌ {msg}\n")
            return res

        # 3. OpCode 0 (Hello) フレームの読み取り (残余バッファ引き継ぎ)
        reader = WsReader(sock, initial_buf=remaining)
        sock.settimeout(timeout)
        opcode, payload = reader.read_frame()
        hello_data = json.loads(payload.decode("utf-8"))

        if hello_data.get("op") != 0 or "d" not in hello_data:
            sock.close()
            msg = f"OBS WebSocket v5 の Hello メッセージを解釈できませんでした: {payload[:100]}"
            res["status"] = "protocol_error"
            res["message"] = msg
            if log_to_stderr:
                sys.stderr.write(f"[OBS-Probe] ❌ {msg}\n")
            return res

        d = hello_data["d"]
        res["obs_version"] = d.get("obsWebSocketVersion")
        res["rpc_version"] = d.get("rpcVersion")

        auth_info = d.get("authentication")
        if auth_info:
            res["auth_required"] = True
            if not password:
                sock.close()
                msg = (
                    f"OBS WebSocket サーバー (v{res['obs_version'] or '5'}) は正常稼働していますが、"
                    f"OBS 側でパスワード認証が有効です。"
                    f"OBS Studio の [ツール] -> [WebSocket サーバー設定] に表示されているサーバーパスワードを"
                    f"Webマイク画面の OBS 設定に入力してください。"
                )
                res["status"] = "auth_required"
                res["message"] = msg
                if log_to_stderr:
                    sys.stderr.write(f"[OBS-Probe] ⚠️ {msg}\n")
                return res

            # パスワード検証: Identify (op 1) 送信
            auth_token = compute_obs_auth(password, auth_info["salt"], auth_info["challenge"])
            identify_payload = {
                "op": 1,
                "d": {
                    "rpcVersion": d.get("rpcVersion", 1),
                    "authentication": auth_token,
                    "eventSubscriptions": 0,
                },
            }
            _send_ws_text_frame(sock, json.dumps(identify_payload))

            # OpCode 2 (Identified) の受信待ち
            opcode_id, payload_id = reader.read_frame()
            id_data = json.loads(payload_id.decode("utf-8"))
            sock.close()

            if id_data.get("op") == 2:
                msg = f"✔ OBS WebSocket v5 (OBS Studio v{res['obs_version'] or '5'}) に接続・パスワード認証が成功しました！"
                res["ok"] = True
                res["status"] = "connected"
                res["message"] = msg
                if log_to_stderr:
                    sys.stderr.write(f"[OBS-Probe] {msg}\n")
                return res
            else:
                msg = f"❌ OBS WebSocket パスワード認証に失敗しました。パスワードが正しいか確認してください。"
                res["status"] = "auth_failed"
                res["message"] = msg
                if log_to_stderr:
                    sys.stderr.write(f"[OBS-Probe] {msg}\n")
                return res
        else:
            # 認証不要
            sock.close()
            msg = f"✔ OBS WebSocket v5 (OBS Studio v{res['obs_version'] or '5'}) に接続成功しました（パスワード認証なし）。"
            res["ok"] = True
            res["status"] = "connected"
            res["message"] = msg
            if log_to_stderr:
                sys.stderr.write(f"[OBS-Probe] {msg}\n")
            return res

    except Exception as e:
        try:
            sock.close()
        except Exception:
            pass
        msg = f"OBS WebSocket ハンドシェイク中にエラーが発生しました: {e}"
        res["status"] = "error"
        res["message"] = msg
        if log_to_stderr:
            sys.stderr.write(f"[OBS-Probe] ❌ {msg}\n")
        return res


def main():
    import argparse
    parser = argparse.ArgumentParser(description="OBS Studio WebSocket v5 Connectivity Probe")
    parser.add_argument("--url", default="ws://127.0.0.1:4455", help="OBS WebSocket URL (default: ws://127.0.0.1:4455)")
    parser.add_argument("--password", default="", help="OBS WebSocket password")
    parser.add_argument("--timeout", type=float, default=2.0, help="Socket timeout in seconds")
    parser.add_argument("--json", action="store_true", help="Output only JSON")
    args = parser.parse_args()

    result = probe_obs_connection(url=args.url, password=args.password, timeout=args.timeout, log_to_stderr=not args.json)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        status_symbol = "✔" if result["ok"] else ("⚠️" if result["status"] == "auth_required" else "❌")
        print(f"\n{status_symbol} 判定結果: {result['status']}")
        print(f"メッセージ: {result['message']}")
    sys.exit(0 if result["ok"] else 1)


if __name__ == "__main__":
    main()
