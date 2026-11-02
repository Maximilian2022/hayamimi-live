"""
tests/test_obs_probe.py
OBS Studio WebSocket v5 疎通診断モジュール (scripts/obs_probe.py) の単体テスト
"""

import base64
import hashlib
import json
import socket
import struct
import threading
import pytest

from scripts.obs_probe import (
    compute_obs_auth,
    parse_ws_url,
    probe_obs_connection,
)


def _make_ws_server_frame(payload_bytes: bytes) -> bytes:
    """テスト用: サーバーからクライアントへの WebSocket フレーム生成 (Maskなし)"""
    n = len(payload_bytes)
    frame = bytearray([0x81])
    if n < 126:
        frame.append(n)
    elif n < 65536:
        frame.append(126)
        frame.extend(struct.pack("!H", n))
    else:
        frame.append(127)
        frame.extend(struct.pack("!Q", n))
    frame.extend(payload_bytes)
    return bytes(frame)


def test_parse_ws_url():
    h, p = parse_ws_url("ws://127.0.0.1:4455")
    assert h == "127.0.0.1" and p == 4455

    h, p = parse_ws_url("192.168.1.50:4455")
    assert h == "192.168.1.50" and p == 4455

    h, p = parse_ws_url("wss://obs.example.com")
    assert h == "obs.example.com" and p == 443

    h, p = parse_ws_url("ws://localhost")
    assert h == "localhost" and p == 4455


def test_compute_obs_auth():
    # OBS WebSocket v5 仕様に基づくハッシュ計算の整合性
    password = "secret_password"
    salt = "dGVzdF9zYWx0"
    challenge = "dGVzdF9jaGFsbGVuZ2U="

    s1 = base64.b64encode(hashlib.sha256((password + salt).encode("utf-8")).digest()).decode("utf-8")
    s2 = base64.b64encode(hashlib.sha256((s1 + challenge).encode("utf-8")).digest()).decode("utf-8")

    calc = compute_obs_auth(password, salt, challenge)
    assert calc == s2


def test_probe_connection_refused():
    # 未使用ポートを探す
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()

    res = probe_obs_connection(f"ws://127.0.0.1:{port}", timeout=0.2, log_to_stderr=False)
    assert res["ok"] is False
    assert res["status"] in ("connection_refused", "timeout", "connect_error")


def test_probe_mock_obs_no_auth():
    """認証なし OBS WebSocket v5 モックサーバーへの接続テスト"""
    server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_sock.bind(("127.0.0.1", 0))
    port = server_sock.getsockname()[1]
    server_sock.listen(1)

    def run_mock():
        conn, _ = server_sock.accept()
        try:
            req = conn.recv(1024).decode("utf-8", errors="replace")
            assert "Upgrade: websocket" in req
            resp = (
                "HTTP/1.1 101 Switching Protocols\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                "Sec-WebSocket-Accept: s3pPLMBiTxaQ9kYGzzhZRbK+xOo=\r\n\r\n"
            )
            conn.sendall(resp.encode("utf-8"))

            hello = {
                "op": 0,
                "d": {
                    "obsWebSocketVersion": "5.3.0",
                    "rpcVersion": 1,
                }
            }
            conn.sendall(_make_ws_server_frame(json.dumps(hello).encode("utf-8")))
        finally:
            conn.close()
            server_sock.close()

    th = threading.Thread(target=run_mock, daemon=True)
    th.start()

    res = probe_obs_connection(f"ws://127.0.0.1:{port}", timeout=2.0, log_to_stderr=False)
    th.join(timeout=2.0)

    assert res["ok"] is True
    assert res["status"] == "connected"
    assert res["obs_version"] == "5.3.0"
    assert res["auth_required"] is False


def test_probe_mock_obs_auth_required_and_success():
    """認証必須 OBS WebSocket v5 モックサーバーへの接続・認証テスト"""
    server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_sock.bind(("127.0.0.1", 0))
    port = server_sock.getsockname()[1]
    server_sock.listen(1)

    salt = "somesalt=="
    challenge = "somechallenge=="
    correct_password = "my_obs_password"
    expected_auth = compute_obs_auth(correct_password, salt, challenge)

    def run_mock():
        conn, _ = server_sock.accept()
        try:
            req = conn.recv(1024).decode("utf-8", errors="replace")
            assert "Upgrade: websocket" in req
            resp = (
                "HTTP/1.1 101 Switching Protocols\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                "Sec-WebSocket-Accept: s3pPLMBiTxaQ9kYGzzhZRbK+xOo=\r\n\r\n"
            )
            conn.sendall(resp.encode("utf-8"))

            hello = {
                "op": 0,
                "d": {
                    "obsWebSocketVersion": "5.4.1",
                    "rpcVersion": 1,
                    "authentication": {
                        "salt": salt,
                        "challenge": challenge,
                    }
                }
            }
            conn.sendall(_make_ws_server_frame(json.dumps(hello).encode("utf-8")))

            # クライアントの Identify (op 1) を受信
            h = conn.recv(2)
            pay_len = h[1] & 0x7F
            mask = conn.recv(4)
            masked_data = conn.recv(pay_len)
            data = bytes([masked_data[i] ^ mask[i % 4] for i in range(pay_len)])
            ident = json.loads(data.decode("utf-8"))

            if ident.get("op") == 1 and ident.get("d", {}).get("authentication") == expected_auth:
                resp_op = {"op": 2, "d": {"negotiatedRpcVersion": 1}}
            else:
                resp_op = {"op": 0}

            conn.sendall(_make_ws_server_frame(json.dumps(resp_op).encode("utf-8")))
        finally:
            conn.close()
            server_sock.close()

    th = threading.Thread(target=run_mock, daemon=True)
    th.start()

    res = probe_obs_connection(f"ws://127.0.0.1:{port}", password=correct_password, timeout=2.0, log_to_stderr=False)
    th.join(timeout=2.0)

    assert res["ok"] is True
    assert res["status"] == "connected"
    assert res["obs_version"] == "5.4.1"
    assert res["auth_required"] is True


def test_probe_mock_obs_auth_required_without_password():
    """認証必須 OBS WebSocket v5 モックサーバーにパスワードなしで接続した場合"""
    server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_sock.bind(("127.0.0.1", 0))
    port = server_sock.getsockname()[1]
    server_sock.listen(1)

    def run_mock():
        conn, _ = server_sock.accept()
        try:
            req = conn.recv(1024).decode("utf-8", errors="replace")
            assert "Upgrade: websocket" in req
            resp = (
                "HTTP/1.1 101 Switching Protocols\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                "Sec-WebSocket-Accept: s3pPLMBiTxaQ9kYGzzhZRbK+xOo=\r\n\r\n"
            )
            conn.sendall(resp.encode("utf-8"))

            hello = {
                "op": 0,
                "d": {
                    "obsWebSocketVersion": "5.4.1",
                    "rpcVersion": 1,
                    "authentication": {
                        "salt": "somesalt==",
                        "challenge": "somechallenge==",
                    }
                }
            }
            conn.sendall(_make_ws_server_frame(json.dumps(hello).encode("utf-8")))
        finally:
            conn.close()
            server_sock.close()

    th = threading.Thread(target=run_mock, daemon=True)
    th.start()

    res = probe_obs_connection(f"ws://127.0.0.1:{port}", password="", timeout=2.0, log_to_stderr=False)
    th.join(timeout=2.0)

    assert res["ok"] is False
    assert res["status"] == "auth_required"
    assert res["auth_required"] is True
    assert "パスワード" in res["message"]
