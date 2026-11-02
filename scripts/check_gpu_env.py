"""
scripts/check_gpu_env.py
GPU (CUDA) 仮想環境 (.venv_gpu) 健全性診断 & 自己修復判定スクリプト

過去バージョン (v2.0.0 以前の DirectML 実験環境等) からのアップデートユーザー環境を安全に保護し、
DirectML の残留や CUDA 不整合を瞬時に検知してランチャー GUI / CLI に診断結果 (JSON) を提供します。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


CURRENT_GPU_ENV_VERSION = "cuda_v2.2.0"


def get_project_root() -> str:
    return os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def get_gpu_venv_dir() -> str:
    return os.path.join(get_project_root(), ".venv_gpu")


def get_gpu_python_path() -> str:
    venv_dir = get_gpu_venv_dir()
    if sys.platform == "win32":
        return os.path.join(venv_dir, "Scripts", "python.exe")
    return os.path.join(venv_dir, "bin", "python")


def get_marker_file_path() -> str:
    return os.path.join(get_gpu_venv_dir(), ".gpu_env_version")


def read_marker_version() -> str | None:
    marker = get_marker_file_path()
    if os.path.isfile(marker):
        try:
            with open(marker, "r", encoding="utf-8") as f:
                return f.read().strip()
        except Exception:
            pass
    return None


def write_marker_version(ver: str = CURRENT_GPU_ENV_VERSION) -> bool:
    try:
        os.makedirs(get_gpu_venv_dir(), exist_ok=True)
        with open(get_marker_file_path(), "w", encoding="utf-8") as f:
            f.write(ver.strip())
        return True
    except Exception:
        return False


def run_gpu_env_probe() -> dict:
    """
    .venv_gpu 内の Python を用いて、CUDA, DirectML, faster-whisper, peft の整合性を診断する。
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

    probe_code = (
        "import sys, json\n"
        "res = {}\n"
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
        "    import ctranslate2\n"
        "    res['ctranslate2'] = True\n"
        "    res['ctranslate2_cuda'] = 'cuda' in ctranslate2.get_supported_compute_types('cuda')\n"
        "except Exception:\n"
        "    res['ctranslate2'] = False\n"
        "    res['ctranslate2_cuda'] = False\n"
        "print(json.dumps(res))\n"
    )

    try:
        proc = subprocess.run(
            [python_exe, "-B", "-c", probe_code],
            capture_output=True,
            text=True,
            timeout=8,
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
        return {
            "status": "no_cuda",
            "ready": False,
            "message": "PyTorch で CUDA が利用できません (CPU版またはCUDAドライバ未検出)。再構築が必要です。",
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

    if missing:
        return {
            "status": "incomplete",
            "ready": False,
            "message": f"必須ライブラリが不足しています: {', '.join(missing)}。再セットアップが必要です。",
            "diag": diag,
            "installed_version": installed_version,
            "current_version": CURRENT_GPU_ENV_VERSION,
        }

    # 健全性確認完了 -> マーカーファイルを最新化
    write_marker_version(CURRENT_GPU_ENV_VERSION)

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
    args = parser.parse_args()

    if args.write_marker:
        write_marker_version(CURRENT_GPU_ENV_VERSION)
        print(f"Marker written: {CURRENT_GPU_ENV_VERSION}")
        return

    result = run_gpu_env_probe()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not result.get("ready"):
        sys.exit(1)


if __name__ == "__main__":
    main()
