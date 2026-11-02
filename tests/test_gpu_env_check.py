"""
tests/test_gpu_env_check.py
scripts/check_gpu_env.py の単体テストスイート
"""

import json
import os
import subprocess
import tempfile
from unittest.mock import MagicMock, patch

import pytest

from scripts.check_gpu_env import (
    CURRENT_GPU_ENV_VERSION,
    get_gpu_python_path,
    get_gpu_venv_dir,
    get_marker_file_path,
    read_marker_version,
    run_gpu_env_probe,
    write_marker_version,
)


def test_paths():
    vdir = get_gpu_venv_dir()
    assert os.path.basename(vdir) == ".venv_gpu"
    py_path = get_gpu_python_path()
    assert ".venv_gpu" in py_path
    marker_path = get_marker_file_path()
    assert os.path.basename(marker_path) == ".gpu_env_version"


def test_marker_read_write(tmp_path, monkeypatch):
    test_marker = str(tmp_path / ".gpu_env_version")
    monkeypatch.setattr("scripts.check_gpu_env.get_marker_file_path", lambda: test_marker)
    monkeypatch.setattr("scripts.check_gpu_env.get_gpu_venv_dir", lambda: str(tmp_path))

    assert read_marker_version() is None
    assert write_marker_version("test_v1.0") is True
    assert read_marker_version() == "test_v1.0"


def test_probe_not_installed(tmp_path, monkeypatch):
    non_existent = str(tmp_path / "bin" / "python.exe")
    monkeypatch.setattr("scripts.check_gpu_env.get_gpu_python_path", lambda: non_existent)

    res = run_gpu_env_probe()
    assert res["status"] == "not_installed"
    assert res["ready"] is False
    assert "存在しません" in res["message"]


def test_probe_incompatible_directml(tmp_path, monkeypatch):
    fake_py = tmp_path / "python.exe"
    fake_py.write_text("#!/bin/sh\n")
    monkeypatch.setattr("scripts.check_gpu_env.get_gpu_python_path", lambda: str(fake_py))

    fake_output = {
        "torch_version": "2.4.0",
        "cuda_available": True,
        "directml_detected": True,
        "faster_whisper": True,
        "peft": True,
        "ctranslate2": True,
    }

    mock_proc = MagicMock()
    mock_proc.returncode = 0
    mock_proc.stdout = json.dumps(fake_output)
    mock_proc.stderr = ""

    with patch("subprocess.run", return_value=mock_proc):
        res = run_gpu_env_probe()
        assert res["status"] == "incompatible_directml"
        assert res["ready"] is False
        assert "DirectML" in res["message"]


def test_probe_no_cuda(tmp_path, monkeypatch):
    fake_py = tmp_path / "python.exe"
    fake_py.write_text("#!/bin/sh\n")
    monkeypatch.setattr("scripts.check_gpu_env.get_gpu_python_path", lambda: str(fake_py))

    fake_output = {
        "torch_version": "2.4.0+cpu",
        "cuda_available": False,
        "directml_detected": False,
        "faster_whisper": True,
        "peft": True,
        "ctranslate2": True,
    }

    mock_proc = MagicMock()
    mock_proc.returncode = 0
    mock_proc.stdout = json.dumps(fake_output)
    mock_proc.stderr = ""

    with patch("subprocess.run", return_value=mock_proc):
        res = run_gpu_env_probe()
        assert res["status"] == "no_cuda"
        assert res["ready"] is False
        assert "CUDA が利用できません" in res["message"]


def test_probe_incomplete_packages(tmp_path, monkeypatch):
    fake_py = tmp_path / "python.exe"
    fake_py.write_text("#!/bin/sh\n")
    monkeypatch.setattr("scripts.check_gpu_env.get_gpu_python_path", lambda: str(fake_py))

    fake_output = {
        "torch_version": "2.4.0+cu124",
        "cuda_available": True,
        "directml_detected": False,
        "faster_whisper": False,
        "peft": True,
        "ctranslate2": True,
    }

    mock_proc = MagicMock()
    mock_proc.returncode = 0
    mock_proc.stdout = json.dumps(fake_output)
    mock_proc.stderr = ""

    with patch("subprocess.run", return_value=mock_proc):
        res = run_gpu_env_probe()
        assert res["status"] == "incomplete"
        assert res["ready"] is False
        assert "faster-whisper" in res["message"]


def test_probe_ready_success(tmp_path, monkeypatch):
    fake_py = tmp_path / "python.exe"
    fake_py.write_text("#!/bin/sh\n")
    monkeypatch.setattr("scripts.check_gpu_env.get_gpu_python_path", lambda: str(fake_py))
    monkeypatch.setattr("scripts.check_gpu_env.get_marker_file_path", lambda: str(tmp_path / ".gpu_env_version"))
    monkeypatch.setattr("scripts.check_gpu_env.get_gpu_venv_dir", lambda: str(tmp_path))

    fake_output = {
        "torch_version": "2.4.0+cu124",
        "cuda_available": True,
        "device_name": "NVIDIA GeForce RTX 5090",
        "device_count": 1,
        "directml_detected": False,
        "faster_whisper": True,
        "peft": True,
        "ctranslate2": True,
        "ctranslate2_cuda": True,
    }

    mock_proc = MagicMock()
    mock_proc.returncode = 0
    mock_proc.stdout = json.dumps(fake_output)
    mock_proc.stderr = ""

    with patch("subprocess.run", return_value=mock_proc):
        res = run_gpu_env_probe()
        assert res["status"] == "ready"
        assert res["ready"] is True
        assert res["device_name"] == "NVIDIA GeForce RTX 5090"
        assert read_marker_version() == CURRENT_GPU_ENV_VERSION

