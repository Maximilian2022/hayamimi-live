"""tools/profile_builder/youtube_fetcher.py の SAC 対策・ffmpeg 無複製解決の単体テスト."""

import os
import sys
from pathlib import Path

import pytest

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from tools.profile_builder import youtube_fetcher as yf  # noqa: E402


def test_resolve_yt_dlp_cmd_prefers_python_module_over_unsigned_shim(monkeypatch, tmp_path):
    """yt_dlp パッケージが存在する場合、未署名 yt-dlp.exe シムより sys.executable -m yt_dlp を優先する."""
    fake_shim = tmp_path / "yt-dlp.exe"
    fake_shim.write_bytes(b"MZ_FAKE")
    monkeypatch.setattr(yf.shutil, "which", lambda name: str(fake_shim))

    cmd = yf.resolve_yt_dlp_cmd()
    assert cmd == [sys.executable, "-m", "yt_dlp"]


def test_resolve_ffmpeg_cmd_uses_binary_in_place_without_copying(monkeypatch, tmp_path):
    """imageio-ffmpeg 等の別名バイナリを 80MB コピーせず、そのままフルパスで返す."""
    bin_dir = tmp_path / "binaries"
    bin_dir.mkdir()
    orig_bin = bin_dir / "ffmpeg-win-x86_64-v7.1.exe"
    orig_bin.write_bytes(b"FAKE_FFMPEG")

    monkeypatch.setattr(yf.shutil, "which", lambda name: None)
    monkeypatch.setattr(yf, "_probe_ffmpeg_executable", lambda p: (True, False))

    class _DummyImageioFfmpeg:
        @staticmethod
        def get_ffmpeg_exe():
            return str(orig_bin)

    monkeypatch.setitem(sys.modules, "imageio_ffmpeg", _DummyImageioFfmpeg)

    resolved = yf.resolve_ffmpeg_cmd()
    assert os.path.normcase(resolved) == os.path.normcase(str(orig_bin.resolve()))
    # 同フォルダに ffmpeg.exe の複製を作っていないこと
    assert not (bin_dir / "ffmpeg.exe").exists()


def test_sac_block_detection_and_guidance_message():
    """WinError 4551 (Smart App Control ブロック) を正しく検知し日本語案内を生成する."""
    err = OSError(22, "An Application Control policy has blocked this file.")
    err.winerror = 4551
    assert yf._is_sac_block_error(err) is True
    assert yf._is_sac_block_error("OSError: [WinError 4551] スマート アプリ コントロール") is True
    assert yf._is_sac_block_error("normal file not found") is False

    msg = yf._sac_ffmpeg_guidance_message("C:/path/to/ffmpeg.exe")
    assert "スマート アプリ コントロール" in msg
    assert "4551" in msg
    assert "winget install Gyan.FFmpeg" in msg


def test_resolve_ffmpeg_cmd_skips_sac_blocked_candidate_and_uses_working_fallback(monkeypatch, tmp_path):
    """先頭候補が SAC ブロックされても、後続の動作する候補へフォールバックする."""
    blocked_bin = tmp_path / "blocked_ffmpeg.exe"
    blocked_bin.write_bytes(b"BLOCKED")
    working_bin = tmp_path / "working_ffmpeg.exe"
    working_bin.write_bytes(b"WORKING")

    monkeypatch.setattr(yf.shutil, "which", lambda name: str(blocked_bin))

    def fake_probe(path: str):
        if os.path.normcase(path) == os.path.normcase(str(blocked_bin)):
            return (False, True)
        if os.path.normcase(path) == os.path.normcase(str(working_bin)):
            return (True, False)
        return (False, False)

    monkeypatch.setattr(yf, "_probe_ffmpeg_executable", fake_probe)

    class _DummyImageioFfmpeg:
        @staticmethod
        def get_ffmpeg_exe():
            return str(working_bin)

    monkeypatch.setitem(sys.modules, "imageio_ffmpeg", _DummyImageioFfmpeg)

    resolved = yf.resolve_ffmpeg_cmd()
    assert os.path.normcase(resolved) == os.path.normcase(str(working_bin.resolve()))
