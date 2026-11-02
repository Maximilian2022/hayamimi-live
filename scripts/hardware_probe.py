"""
scripts/hardware_probe.py
ハードウェア診断および DirectML デバイス解決エンジン

Windows DXGI API (ctypes) および WMI を活用し、メイン GPU (dGPU: NVIDIA GeForce 等) を
完全保護 (負荷 0%) しながら、音声認識用の内蔵 GPU (iGPU: Intel / AMD) の DirectML Device ID を自動特定します。
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import json
import logging
import os
import platform
import subprocess
import sys
from dataclasses import asdict, dataclass
from typing import Optional

logger = logging.getLogger("hardware_probe")

# ベンダー ID 定義
VENDOR_INTEL = 0x8086
VENDOR_AMD = 0x1002
VENDOR_NVIDIA = 0x10DE
VENDOR_MICROSOFT = 0x1414

# DXGI フラグ
DXGI_ADAPTER_FLAG_NONE = 0
DXGI_ADAPTER_FLAG_REMOTE = 1
DXGI_ADAPTER_FLAG_SOFTWARE = 2


@dataclass
class GpuDevice:
    """検出された GPU デバイス情報"""
    index: int
    name: str
    vendor_id: int
    vendor_name: str
    device_id: int
    is_integrated: bool
    is_discrete: bool
    is_software: bool
    vram_mb: int
    shared_ram_mb: int = 0
    directml_device_id: Optional[int] = None

    def __post_init__(self):
        if self.directml_device_id is None:
            self.directml_device_id = self.index


@dataclass
class NpuDevice:
    """検出された NPU デバイス情報"""
    name: str
    vendor_name: str
    device_id: str
    status: str
    is_available: bool



class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_byte * 8),
    ]


class LUID(ctypes.Structure):
    _fields_ = [
        ("LowPart", wintypes.DWORD),
        ("HighPart", wintypes.LONG),
    ]


class DXGI_ADAPTER_DESC1(ctypes.Structure):
    _fields_ = [
        ("Description", wintypes.WCHAR * 128),
        ("VendorId", wintypes.UINT),
        ("DeviceId", wintypes.UINT),
        ("SubSysId", wintypes.UINT),
        ("Revision", wintypes.UINT),
        ("DedicatedVideoMemory", ctypes.c_size_t),
        ("DedicatedSystemMemory", ctypes.c_size_t),
        ("SharedSystemMemory", ctypes.c_size_t),
        ("AdapterLuid", LUID),
        ("Flags", wintypes.UINT),
    ]


def _get_vendor_name(vendor_id: int) -> str:
    if vendor_id == VENDOR_INTEL:
        return "Intel"
    elif vendor_id == VENDOR_AMD:
        return "AMD"
    elif vendor_id == VENDOR_NVIDIA:
        return "NVIDIA"
    elif vendor_id == VENDOR_MICROSOFT:
        return "Microsoft"
    return "Unknown"


def _classify_gpu(vendor_id: int, name: str, vram_mb: int, flags: int) -> tuple[bool, bool]:
    """
    GPU が内蔵 (iGPU) か専用 (dGPU) かを正確に判定する。
    """
    if flags & DXGI_ADAPTER_FLAG_SOFTWARE:
        return False, False

    name_lower = name.lower()

    # NVIDIA は外付け・専用 GPU (配信・ゲーム専用)
    if vendor_id == VENDOR_NVIDIA or "geforce" in name_lower or "rtx" in name_lower:
        return False, True

    # Intel
    if vendor_id == VENDOR_INTEL or "intel" in name_lower:
        # Intel Arc (A770, A750, A580, A380, A310, B580, B570 等) の外付け専用 GPU
        if "arc" in name_lower and ("a7" in name_lower or "a5" in name_lower or "b5" in name_lower or vram_mb >= 6000):
            return False, True
        # それ以外の Intel (UHD, HD, Iris Xe, Xe-LPG, Core Ultra 等) はすべて内蔵 GPU
        return True, False

    # AMD
    if vendor_id == VENDOR_AMD or "amd" in name_lower or "radeon" in name_lower:
        # 内蔵 GPU パターン
        if any(pat in name_lower for pat in ["graphics", "radeon(tm) graphics", "780m", "890m", "680m", "vega"]):
            return True, False
        # VRAM が 2GB 以下で明確な RX 型番がなければ内蔵とみなす
        if vram_mb <= 2048 and not any(rx in name_lower for rx in ["rx 6", "rx 7", "rx 5", "rx 4", "xt"]):
            return True, False
        return False, True

    # フォールバック (VRAM 判定)
    if vram_mb <= 1024:
        return True, False
    return False, True


def probe_gpu_devices_dxgi() -> list[GpuDevice]:
    """
    Windows DXGI API を用いてシステム内の全 GPU アダプター情報を取得する。
    """
    devices: list[GpuDevice] = []
    if sys.platform != "win32":
        return devices

    try:
        dxgi = ctypes.windll.dxgi
        # IID_IDXGIFactory1: {770aae78-f26f-4dba-a829-253c83d1b387}
        iid_factory1 = GUID(
            0x770AAE78,
            0xF26F,
            0x4DBA,
            (ctypes.c_byte * 8)(0xA8, 0x29, 0x25, 0x3C, 0x83, 0xD1, 0xB3, 0x87),
        )
        factory = ctypes.c_void_p()
        hr = dxgi.CreateDXGIFactory1(ctypes.byref(iid_factory1), ctypes.byref(factory))
        if hr != 0 or not factory:
            return []

        vtable_ptr = ctypes.cast(factory, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))
        vtable = vtable_ptr.contents

        # IDXGIFactory1::EnumAdapters1 (index 12)
        enum_adapters1 = ctypes.WINFUNCTYPE(
            ctypes.c_long, ctypes.c_void_p, wintypes.UINT, ctypes.POINTER(ctypes.c_void_p)
        )(vtable[12])
        release_factory = ctypes.WINFUNCTYPE(wintypes.ULONG, ctypes.c_void_p)(vtable[2])

        idx = 0
        while True:
            adapter = ctypes.c_void_p()
            hr = enum_adapters1(factory, idx, ctypes.byref(adapter))
            if hr != 0 or not adapter:
                break

            adapter_vtable_ptr = ctypes.cast(
                adapter, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))
            )
            adapter_vtable = adapter_vtable_ptr.contents
            get_desc1 = ctypes.WINFUNCTYPE(
                ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(DXGI_ADAPTER_DESC1)
            )(adapter_vtable[10])
            release_adapter = ctypes.WINFUNCTYPE(wintypes.ULONG, ctypes.c_void_p)(
                adapter_vtable[2]
            )

            desc = DXGI_ADAPTER_DESC1()
            if get_desc1(adapter, ctypes.byref(desc)) == 0:
                is_sw = bool(desc.Flags & DXGI_ADAPTER_FLAG_SOFTWARE)
                vram_mb = int(desc.DedicatedVideoMemory // (1024 * 1024))
                shared_mb = int(desc.SharedSystemMemory // (1024 * 1024))
                vendor_name = _get_vendor_name(desc.VendorId)
                is_igpu, is_dgpu = _classify_gpu(
                    desc.VendorId, desc.Description, vram_mb, desc.Flags
                )

                dev = GpuDevice(
                    index=idx,
                    name=str(desc.Description),
                    vendor_id=int(desc.VendorId),
                    vendor_name=vendor_name,
                    device_id=int(desc.DeviceId),
                    is_integrated=is_igpu,
                    is_discrete=is_dgpu,
                    is_software=is_sw,
                    vram_mb=vram_mb,
                    shared_ram_mb=shared_mb,
                    directml_device_id=idx,
                )
                devices.append(dev)

            release_adapter(adapter)
            idx += 1

        release_factory(factory)
    except Exception as e:
        logger.warning(f"DXGI GPU 列挙中に例外が発生しました: {e}")

    return devices


def probe_gpu_devices_wmi() -> list[GpuDevice]:
    """
    DXGI が利用できない環境向けの WMI フォールバック走査。
    """
    devices: list[GpuDevice] = []
    if sys.platform != "win32":
        return devices

    try:
        cmd = [
            "powershell",
            "-NoProfile",
            "-Command",
            "Get-CimInstance Win32_VideoController | Select-Object Name, PNPDeviceID, AdapterRAM | ConvertTo-Json -Compress",
        ]
        res = subprocess.run(
            cmd, capture_output=True, text=True, timeout=5, creationflags=0x08000000
        )
        if res.returncode == 0 and res.stdout.strip():
            raw = json.loads(res.stdout.strip())
            items = raw if isinstance(raw, list) else [raw]
            for idx, item in enumerate(items):
                name = item.get("Name", "")
                pnp = item.get("PNPDeviceID", "")
                vram_bytes = item.get("AdapterRAM") or 0
                vram_mb = int(vram_bytes // (1024 * 1024))

                vendor_id = 0
                if "VEN_8086" in pnp:
                    vendor_id = VENDOR_INTEL
                elif "VEN_1002" in pnp:
                    vendor_id = VENDOR_AMD
                elif "VEN_10DE" in pnp:
                    vendor_id = VENDOR_NVIDIA

                is_igpu, is_dgpu = _classify_gpu(vendor_id, name, vram_mb, 0)
                dev = GpuDevice(
                    index=idx,
                    name=name,
                    vendor_id=vendor_id,
                    vendor_name=_get_vendor_name(vendor_id),
                    device_id=0,
                    is_integrated=is_igpu,
                    is_discrete=is_dgpu,
                    is_software=False,
                    vram_mb=vram_mb,
                    directml_device_id=idx,
                )
                devices.append(dev)
    except Exception as e:
        logger.warning(f"WMI GPU 列挙中に例外が発生しました: {e}")

    return devices


def probe_gpu_devices() -> list[GpuDevice]:
    """
    システム内の全 GPU デバイスを取得する。DXGI を最優先し、失敗時は WMI でフォールバック。
    """
    devs = probe_gpu_devices_dxgi()
    if not devs:
        devs = probe_gpu_devices_wmi()
    return devs


def resolve_igpu_directml_device_id() -> Optional[int]:
    """
    DirectML で使用すべき内蔵 GPU (iGPU) の Device ID を自動解決する。
    メイン GPU (NVIDIA GeForce RTX 等の dGPU) はゲーム・配信用に完全保護し、
    Intel 内蔵 GPU または AMD 内蔵 GPU のみを厳密に特定して返却する。
    内蔵 GPU が存在しない場合は None を返す。
    """
    devices = probe_gpu_devices()
    valid_devices = [d for d in devices if not d.is_software]

    # 1. Intel 内蔵 GPU を優先探索
    for d in valid_devices:
        if d.is_integrated and d.vendor_id == VENDOR_INTEL:
            return d.directml_device_id

    # 2. AMD 内蔵 GPU を探索
    for d in valid_devices:
        if d.is_integrated and d.vendor_id == VENDOR_AMD:
            return d.directml_device_id

    return None


def probe_npu_device() -> Optional[NpuDevice]:
    """
    システム内の NPU (Neural Processing Unit) を検出する。
    1. OpenVINO の available_devices を最優先確認。
    2. Windows PnP / WMI (Win32_PnPEntity) でフォールバック確認。
    """
    # 1. OpenVINO による直接検出
    try:
        import openvino as ov
        core = ov.Core()
        if "NPU" in core.available_devices:
            try:
                name = core.get_property("NPU", "FULL_DEVICE_NAME")
            except Exception:
                name = "Intel(R) AI Boost"
            return NpuDevice(
                name=name,
                vendor_name="Intel",
                device_id="NPU",
                status="OK",
                is_available=True,
            )
    except Exception:
        pass

    # 2. Windows PnP / WMI による検出 (Windows のみ)
    if platform.system() == "Windows":
        try:
            ps_cmd = (
                "Get-CimInstance Win32_PnPEntity | "
                "Where-Object { "
                "($_.Name -match 'AI Boost|\\bNPU\\b|\\bIPU\\b' -or $_.PNPClass -eq 'ComputeAccelerator') -and "
                "$_.PNPClass -notmatch 'Mouse|Keyboard|HIDClass|Media' -and "
                "$_.Name -notmatch 'Input|Receiver|Mouse|Keyboard|Audio|Controller' "
                "} | "
                "Select-Object -First 1 Name, DeviceID, Status | "
                "ConvertTo-Json -Compress"
            )
            out = subprocess.check_output(
                ["powershell", "-NoProfile", "-Command", ps_cmd],
                text=True,
                creationflags=0x08000000 if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
                timeout=4,
            ).strip()
            if out:
                data = json.loads(out)
                name = data.get("Name", "NPU Device")
                status = data.get("Status", "OK")
                dev_id = data.get("DeviceID", "")
                vendor = "Intel" if "8086" in dev_id else ("AMD" if "1002" in dev_id else "Unknown")
                return NpuDevice(
                    name=name,
                    vendor_name=vendor,
                    device_id=dev_id,
                    status=status,
                    is_available=(status == "OK"),
                )
        except Exception as e:
            logger.debug(f"PnP NPU query error: {e}")

    return None


def get_hardware_summary() -> dict:
    """
    UI やランチャー向けのハードウェア診断サマリーを返却する。
    """
    devices = probe_gpu_devices()
    igpu_id = resolve_igpu_directml_device_id()
    npu_dev = probe_npu_device()

    igpu_dev = next((d for d in devices if d.directml_device_id == igpu_id), None)
    main_gpu = next((d for d in devices if d.is_discrete and not d.is_software), None)
    nvidia_gpu = next((d for d in devices if d.vendor_id == VENDOR_NVIDIA and not d.is_software), None)

    return {
        "os": platform.system(),
        "total_gpus": len(devices),
        "has_igpu": igpu_id is not None,
        "igpu_directml_id": igpu_id,
        "igpu_name": igpu_dev.name if igpu_dev else None,
        "igpu_vendor": igpu_dev.vendor_name if igpu_dev else None,
        "has_npu": npu_dev is not None and npu_dev.is_available,
        "npu_name": npu_dev.name if npu_dev else None,
        "npu_device": asdict(npu_dev) if npu_dev else None,
        "has_nvidia_gpu": nvidia_gpu is not None,
        "nvidia_gpu_name": nvidia_gpu.name if nvidia_gpu else None,
        "main_gpu_name": main_gpu.name if main_gpu else None,
        "recommended_device": "npu" if (npu_dev and npu_dev.is_available) else "cpu",
        "recommended_engine": "reazonspeech",
        "devices": [asdict(d) for d in devices],
    }


def main():
    summary = get_hardware_summary()
    print(json.dumps(summary, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
