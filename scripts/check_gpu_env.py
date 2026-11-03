"""
scripts/check_gpu_env.py
GPU (CUDA) 仮想環境 (.venv_gpu) 健全性診断 & 自己修復判定スクリプト

過去バージョン (v2.0.0 以前の DirectML 実験環境等) からのアップデートユーザー環境を安全に保護し、
DirectML の残留や CUDA 不整合を瞬時に検知してランチャー GUI / CLI に診断結果 (JSON) を提供します。
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import shutil
import subprocess
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


CURRENT_GPU_ENV_VERSION = "cuda_v2.3.3_cu128"
REQUIRED_TORCH_BUILD = "+cu128"


def get_project_root() -> str:
    return os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def get_gpu_venv_dir() -> str:
    return os.path.join(get_project_root(), ".venv_gpu")


def get_gpu_python_path() -> str:
    venv_dir = get_gpu_venv_dir()
    if sys.platform == "win32":
        return os.path.join(venv_dir, "Scripts", "python.exe")
    return os.path.join(venv_dir, "bin", "python")


def get_marker_file_path(python_exe: str | None = None) -> str:
    if python_exe:
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(python_exe)))
        return os.path.join(base_dir, ".gpu_env_version")
    return os.path.join(get_gpu_venv_dir(), ".gpu_env_version")


def read_marker_version(python_exe: str | None = None) -> str | None:
    try:
        marker = get_marker_file_path(python_exe)
    except TypeError:
        marker = get_marker_file_path()
    if os.path.isfile(marker):
        try:
            with open(marker, "r", encoding="utf-8") as f:
                return f.read().strip()
        except Exception:
            pass
    return None


def write_marker_version(ver: str = CURRENT_GPU_ENV_VERSION, python_exe: str | None = None) -> bool:
    try:
        try:
            marker = get_marker_file_path(python_exe)
        except TypeError:
            marker = get_marker_file_path()
        os.makedirs(os.path.dirname(marker), exist_ok=True)
        with open(marker, "w", encoding="utf-8") as f:
            f.write(ver.strip())
        return True
    except Exception:
        return False


def remove_stale_torchaudio() -> bool:
    """Delete torchaudio left over from v2.3.0 or earlier. Returns False if it could not be removed.

    v2.3.1 で依存から外したが既存の環境には残る。cu124 のまま torch だけ cu128 に上がると、
    transformers が自動で読み込む際に Windows のエラーダイアログが出てアプリが起動できない。
    pip を持たない uv 製の環境もあるため、ファイルを直接削除する。
    """
    site_packages = os.path.join(get_gpu_venv_dir(), "Lib", "site-packages")
    targets = [
        os.path.join(site_packages, "torchaudio"),
        os.path.join(site_packages, "torio"),
        *glob.glob(os.path.join(glob.escape(site_packages), "torchaudio-*.dist-info")),
    ]
    found = [t for t in targets if os.path.isdir(t)]
    for target in found:
        shutil.rmtree(target, ignore_errors=True)
    return not any(os.path.exists(t) for t in found)


def run_gpu_env_probe(force: bool = False) -> dict:
    """
    .venv_gpu 内の Python を用いて、CUDA, DirectML, faster-whisper, peft の整合性を診断する。
    マーカーファイルが最新で python.exe が存在する場合、force=True でない限り Fast Path (即座に ready) を返す。
    """
    python_exe = get_gpu_python_path()
    if not os.path.isfile(python_exe):
        return {
            "status": "not_installed",
            "ready": False,
            "message": "GPU仮想環境 (.venv_gpu) が存在しません。",
            "gpu_python": python_exe,
            "current_version": CURRENT_GPU_ENV_VERSION,
            "installed_version": None,
        }

    # Fast Path や診断より先に行う。残っていると診断の import 自体がダイアログで止まる
    if not remove_stale_torchaudio():
        return {
            "status": "stale_torchaudio",
            "ready": False,
            "message": "古い torchaudio を削除できませんでした。アプリを終了してからもう一度起動してください。",
            "gpu_python": python_exe,
            "current_version": CURRENT_GPU_ENV_VERSION,
        }

    installed_version = read_marker_version(python_exe)
    if not force and installed_version == CURRENT_GPU_ENV_VERSION:
        return {
            "status": "ready",
            "ready": True,
            "message": "GPU (CUDA) 環境は正常にセットアップされています (Fast Path)。",
            "gpu_python": python_exe,
            "installed_version": CURRENT_GPU_ENV_VERSION,
            "fast_path": True,
        }

    probe_code = (
        "import sys, os, json\n"
        "try:\n"
        "    from scripts.cuda_utils import setup_cuda_dll_path\n"
        "    setup_cuda_dll_path()\n"
        "except Exception:\n"
        "    pass\n"
        "res = {}\n"
        "res['python_version'] = f'{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}'\n"
        "res['python_major_minor'] = f'{sys.version_info.major}.{sys.version_info.minor}'\n"
        "try:\n"
        "    import torch\n"
        "    res['torch_version'] = torch.__version__\n"
        "    res['cuda_available'] = torch.cuda.is_available()\n"
        "    if res['cuda_available']:\n"
        "        res['device_name'] = torch.cuda.get_device_name(0)\n"
        "        res['device_count'] = torch.cuda.device_count()\n"
        "except Exception as e:\n"
        "    res['torch_error'] = str(e)\n"
        "    res['cuda_available'] = False\n"
        "try:\n"
        "    import torch_directml\n"
        "    res['directml_detected'] = True\n"
        "except ImportError:\n"
        "    res['directml_detected'] = False\n"
        "try:\n"
        "    import faster_whisper\n"
        "    res['faster_whisper'] = True\n"
        "except Exception:\n"
        "    res['faster_whisper'] = False\n"
        "try:\n"
        "    import peft\n"
        "    res['peft'] = True\n"
        "except Exception:\n"
        "    res['peft'] = False\n"
        "try:\n"
        "    import ctypes\n"
        "    ctypes.CDLL('cublas64_12.dll')\n"
        "    res['cublas_available'] = True\n"
        "except Exception as e:\n"
        "    res['cublas_available'] = False\n"
        "    res['cublas_error'] = str(e)\n"
        "try:\n"
        "    import ctranslate2\n"
        "    res['ctranslate2'] = True\n"
        "    types = ctranslate2.get_supported_compute_types('cuda')\n"
        "    res['ctranslate2_cuda'] = len(types) > 0 and res.get('cublas_available', False)\n"
        "    res['ctranslate2_types'] = list(types)\n"
        "except Exception as e:\n"
        "    res['ctranslate2'] = False\n"
        "    res['ctranslate2_cuda'] = False\n"
        "    res['ctranslate2_error'] = str(e)\n"
        "print(json.dumps(res))\n"
    )

    try:
        proc = subprocess.run(
            [python_exe, "-B", "-c", probe_code],
            capture_output=True,
            text=True,
            timeout=30,
            creationflags=0x08000000 if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
        )
        if proc.returncode != 0:
            return {
                "status": "error",
                "ready": False,
                "message": f"診断スクリプトが異常終了しました (code {proc.returncode}): {proc.stderr.strip()[:200]}",
                "gpu_python": python_exe,
            }

        diag = json.loads(proc.stdout.strip())
    except subprocess.TimeoutExpired:
        return {
            "status": "timeout",
            "ready": False,
            "message": "GPU環境の診断がタイムアウトしました。",
            "gpu_python": python_exe,
        }
    except Exception as e:
        return {
            "status": "error",
            "ready": False,
            "message": f"診断実行中に例外が発生しました: {e}",
            "gpu_python": python_exe,
        }

    installed_version = read_marker_version()

    # 0. Python バージョン互換性チェック (PyTorch CUDA は 3.9〜3.13 のみ対応。3.14+ は wheel なし)
    py_ver = diag.get("python_major_minor", "")
    if py_ver and (py_ver not in ["3.9", "3.10", "3.11", "3.12", "3.13"]):
        return {
            "status": "incompatible_python",
            "ready": False,
            "message": f"GPU 仮想環境の Python バージョン ({py_ver}) は PyTorch CUDA 未対応です。Python 3.12 で再構築が必要です。",
            "diag": diag,
            "installed_version": installed_version,
            "current_version": CURRENT_GPU_ENV_VERSION,
        }

    # 1. 旧 DirectML 残留チェック
    if diag.get("directml_detected"):
        return {
            "status": "incompatible_directml",
            "ready": False,
            "message": "旧バージョンの DirectML (torch-directml) が残留しています。CUDA 環境への再構築が必要です。",
            "diag": diag,
            "installed_version": installed_version,
            "current_version": CURRENT_GPU_ENV_VERSION,
        }

    # 2. CUDA 利用可否チェック
    if not diag.get("cuda_available"):
        torch_ver = diag.get("torch_version", "")
        return {
            "status": "no_cuda",
            "ready": False,
            "message": f"PyTorch で CUDA が利用できません (検出: {torch_ver or 'CPU版'})。CUDA 12.8 版 (torch==2.11.0+cu128) の再セットアップが必要です。",
            "diag": diag,
            "installed_version": installed_version,
            "current_version": CURRENT_GPU_ENV_VERSION,
        }

    # 3. 必須ライブラリチェック
    missing = []
    if not diag.get("faster_whisper"):
        missing.append("faster-whisper")
    if not diag.get("peft"):
        missing.append("peft")
    if not diag.get("ctranslate2"):
        missing.append("ctranslate2")
    elif diag.get("cuda_available") and not diag.get("ctranslate2_cuda"):
        missing.append("nvidia-cublas-cu12 (CUDA 12 DLLs)")

    if missing:
        return {
            "status": "incomplete",
            "ready": False,
            "message": f"必須ライブラリが不足しています: {', '.join(missing)}。再セットアップが必要です。",
            "diag": diag,
            "installed_version": installed_version,
            "current_version": CURRENT_GPU_ENV_VERSION,
        }

    # 4. PyTorch ビルドチェック (旧ビルドは is_available() が True でも RTX 50 系で演算できない)
    torch_ver = diag.get("torch_version") or ""
    if not torch_ver.endswith(REQUIRED_TORCH_BUILD):
        return {
            "status": "outdated_torch",
            "ready": False,
            "message": f"PyTorch ({torch_ver}) が古いため LoRA 学習を実行できない場合があります。torch==2.11.0+cu128 への更新が必要です。",
            "diag": diag,
            "installed_version": installed_version,
            "current_version": CURRENT_GPU_ENV_VERSION,
        }

    # 健全性確認完了 -> マーカーファイルを最新化
    write_marker_version(CURRENT_GPU_ENV_VERSION, python_exe)

    return {
        "status": "ready",
        "ready": True,
        "message": "GPU (CUDA) 環境は正常にセットアップされています。",
        "device_name": diag.get("device_name"),
        "torch_version": diag.get("torch_version"),
        "ctranslate2_cuda": diag.get("ctranslate2_cuda", False),
        "installed_version": CURRENT_GPU_ENV_VERSION,
        "diag": diag,
    }


def main():
    parser = argparse.ArgumentParser(description="GPU Venv Health Checker")
    parser.add_argument("--json", action="store_true", default=True, help="Output JSON")
    parser.add_argument("--write-marker", action="store_true", help="Write version marker")
    parser.add_argument("--force", action="store_true", help="Force deep probe even if marker is present")
    args = parser.parse_args()

    if args.write_marker:
        write_marker_version(CURRENT_GPU_ENV_VERSION)
        print(f"Marker written: {CURRENT_GPU_ENV_VERSION}")
        return

    result = run_gpu_env_probe(force=args.force)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not result.get("ready"):
        sys.exit(1)


if __name__ == "__main__":
    main()
