"""
tests/test_cuda_utils.py
scripts/cuda_utils.py の単体テスト
"""

import os
import sys
from unittest.mock import MagicMock, patch
import pytest

from scripts.cuda_utils import setup_cuda_dll_path


def test_setup_cuda_dll_path_returns_list():
    """setup_cuda_dll_path がリストを返却すること"""
    res = setup_cuda_dll_path()
    assert isinstance(res, list)


def test_setup_cuda_dll_path_registers_existing_dir(tmp_path, monkeypatch):
    """存在するディレクトリが DLL パスに登録されること"""
    if sys.platform != "win32":
        pytest.skip("Windows only test")

    fake_bin = tmp_path / "fake_nvidia_bin"
    fake_bin.mkdir()

    # site.getsitepackages のモック
    with patch("site.getsitepackages", return_value=[str(tmp_path)]):
        fake_sub = tmp_path / "nvidia" / "cublas" / "bin"
        fake_sub.mkdir(parents=True)

        added = setup_cuda_dll_path()
        assert str(fake_sub) in added
        assert str(fake_sub) in os.environ.get("PATH", "")

