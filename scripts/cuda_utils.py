"""
scripts/cuda_utils.py
Windows CUDA / cuBLAS / cuDNN ランタイム DLL 自動探索 & 登録ユーティリティ

Windows 上の CTranslate2 (Faster-Whisper / 翻訳モデル) が
NVIDIA GPU (CUDA) 実行時に `cublas64_12.dll` や `cudnn64_9.dll` を
100% 確実に発見・ロードできるように、Python 実行時に DLL ディレクトリを
`os.add_dll_directory()` および `PATH` に動的登録します。
"""

from __future__ import annotations

import logging
import os
import sys

logger = logging.getLogger("cuda_utils")

_DLL_INITIALIZED = False


def setup_cuda_dll_path() -> list[str]:
    """
    Windows 環境において、nvidia-cublas-cu12、nvidia-cudnn-cu12、torch/lib などの
    CUDA 12 ランタイム DLL ディレクトリを `os.add_dll_directory()` と `PATH` に登録する。
    登録されたディレクトリのリストを返却する。
    """
    global _DLL_INITIALIZED
    added_dirs: list[str] = []

    if sys.platform != "win32":
        return added_dirs

    # 1. nvidia.cublas.bin および nvidia.cudnn.bin パッケージの探索
    for pkg_name in ["nvidia.cublas.bin", "nvidia.cudnn.bin"]:
        try:
            mod = __import__(pkg_name, fromlist=["__file__"])
            p = os.path.dirname(getattr(mod, "__file__", ""))
            if p and os.path.isdir(p) and p not in added_dirs:
                added_dirs.append(p)
        except Exception:
            pass

    # 2. site-packages 配下の nvidia/cublas/bin, nvidia/cudnn/bin, torch/lib 探索
    try:
        candidate_subdirs = [
            os.path.join("nvidia", "cublas", "bin"),
            os.path.join("nvidia", "cudnn", "bin"),
            os.path.join("torch", "lib"),
        ]
        # sys.path および site-packages
        search_roots = list(sys.path)
        try:
            import site
            if hasattr(site, "getsitepackages"):
                search_roots.extend(site.getsitepackages())
            if hasattr(site, "getusersitepackages"):
                usp = site.getusersitepackages()
                if isinstance(usp, str):
                    search_roots.append(usp)
        except Exception:
            pass

        for root in search_roots:
            if not root or not os.path.isdir(root):
                continue
            for sub in candidate_subdirs:
                full = os.path.join(root, sub)
                if os.path.isdir(full) and full not in added_dirs:
                    added_dirs.append(full)
    except Exception:
        pass

    # 3. システム環境変数 CUDA_PATH の bin 探索
    cuda_path = os.environ.get("CUDA_PATH")
    if cuda_path:
        cuda_bin = os.path.join(cuda_path, "bin")
        if os.path.isdir(cuda_bin) and cuda_bin not in added_dirs:
            added_dirs.append(cuda_bin)

    # 4. DLL ディレクトリの登録
    if hasattr(os, "add_dll_directory"):
        for d in added_dirs:
            try:
                os.add_dll_directory(d)
                logger.debug(f"Added DLL directory: {d}")
            except Exception as e:
                logger.debug(f"Failed to add DLL directory {d}: {e}")

    # PATH にも先頭追加 (一部のネイティブ C++ 拡張が PATH を参照するため)
    if added_dirs:
        path_entries = os.environ.get("PATH", "").split(os.pathsep)
        for d in reversed(added_dirs):
            if d not in path_entries:
                path_entries.insert(0, d)
        os.environ["PATH"] = os.pathsep.join(path_entries)

    _DLL_INITIALIZED = True
    return added_dirs


# インポート時に自動セットアップ
setup_cuda_dll_path()

