"""
tests/test_hardware_probe.py
ハードウェア診断および DirectML デバイス解決エンジンの単体テスト
"""

import pytest
from unittest.mock import patch, MagicMock
from scripts.hardware_probe import (
    GpuDevice,
    NpuDevice,
    probe_gpu_devices,
    probe_npu_device,
    resolve_igpu_directml_device_id,
    get_hardware_summary,
    _classify_gpu,
)


def test_classify_gpu_nvidia():
    """NVIDIA GPU はディスクリート GPU (dGPU) と判定されること"""
    is_igpu, is_dgpu = _classify_gpu(0x10DE, "NVIDIA GeForce RTX 5090", 32187, 0)
    assert not is_igpu
    assert is_dgpu


def test_classify_gpu_intel_integrated():
    """Intel 内蔵 GPU は iGPU と判定されること"""
    is_igpu, is_dgpu = _classify_gpu(0x8086, "Intel(R) Graphics", 128, 0)
    assert is_igpu
    assert not is_dgpu


def test_classify_gpu_intel_arc():
    """Intel Arc dGPU は dGPU と判定されること"""
    is_igpu, is_dgpu = _classify_gpu(0x8086, "Intel(R) Arc(TM) A770 Graphics", 16384, 0)
    assert not is_igpu
    assert is_dgpu


def test_classify_gpu_amd_integrated():
    """AMD 内蔵 GPU は iGPU と判定されること"""
    is_igpu, is_dgpu = _classify_gpu(0x1002, "AMD Radeon(TM) 780M Graphics", 512, 0)
    assert is_igpu
    assert not is_dgpu


def test_classify_gpu_amd_discrete():
    """AMD 外付け GPU は dGPU と判定されること"""
    is_igpu, is_dgpu = _classify_gpu(0x1002, "AMD Radeon RX 7900 XTX", 24576, 0)
    assert not is_igpu
    assert is_dgpu


def test_resolve_igpu_device_dual_gpu():
    """メイン GPU (NVIDIA) と Intel iGPU のデュアル環境で iGPU の DirectML ID が選択されること"""
    devices = [
        GpuDevice(index=0, name="NVIDIA GeForce RTX 5090", vendor_id=0x10DE, vendor_name="NVIDIA",
                  device_id=0x2B85, is_integrated=False, is_discrete=True, is_software=False, vram_mb=32187),
        GpuDevice(index=1, name="Intel(R) Graphics", vendor_id=0x8086, vendor_name="Intel",
                  device_id=0x7D67, is_integrated=True, is_discrete=False, is_software=False, vram_mb=128),
    ]
    with patch("scripts.hardware_probe.probe_gpu_devices", return_value=devices):
        dml_id = resolve_igpu_directml_device_id()
        assert dml_id == 1


def test_resolve_igpu_device_dgpu_only():
    """内蔵 GPU が存在しない環境 (dGPU のみ) では None が返ること"""
    devices = [
        GpuDevice(index=0, name="NVIDIA GeForce RTX 4070", vendor_id=0x10DE, vendor_name="NVIDIA",
                  device_id=0x2786, is_integrated=False, is_discrete=True, is_software=False, vram_mb=12288),
    ]
    with patch("scripts.hardware_probe.probe_gpu_devices", return_value=devices):
        dml_id = resolve_igpu_directml_device_id()
        assert dml_id is None


def test_resolve_igpu_device_intel_and_amd():
    """Intel iGPU と AMD iGPU の両方がある場合は Intel を優先すること"""
    devices = [
        GpuDevice(index=0, name="AMD Radeon 780M", vendor_id=0x1002, vendor_name="AMD",
                  device_id=0x15BF, is_integrated=True, is_discrete=False, is_software=False, vram_mb=512),
        GpuDevice(index=1, name="Intel(R) Graphics", vendor_id=0x8086, vendor_name="Intel",
                  device_id=0x7D67, is_integrated=True, is_discrete=False, is_software=False, vram_mb=128),
    ]
    with patch("scripts.hardware_probe.probe_gpu_devices", return_value=devices):
        dml_id = resolve_igpu_directml_device_id()
        assert dml_id == 1


def test_hardware_summary():
    """ハードウェアサマリーが正しく辞書形式で返却されること"""
    devices = [
        GpuDevice(index=0, name="NVIDIA GeForce RTX 5090", vendor_id=0x10DE, vendor_name="NVIDIA",
                  device_id=0x2B85, is_integrated=False, is_discrete=True, is_software=False, vram_mb=32187),
        GpuDevice(index=1, name="Intel(R) Graphics", vendor_id=0x8086, vendor_name="Intel",
                  device_id=0x7D67, is_integrated=True, is_discrete=False, is_software=False, vram_mb=128),
    ]
    with patch("scripts.hardware_probe.probe_gpu_devices", return_value=devices):
        summary = get_hardware_summary()
        assert summary["has_igpu"] is True
        assert summary["igpu_directml_id"] == 1
        assert summary["igpu_name"] == "Intel(R) Graphics"
        assert summary["main_gpu_name"] == "NVIDIA GeForce RTX 5090"
        assert "has_npu" in summary
        assert "recommended_device" in summary


def test_probe_npu_mock():
    """NPU デバイスのモック判定テスト"""
    mock_npu = NpuDevice(
        name="Intel(R) AI Boost",
        vendor_name="Intel",
        device_id="NPU",
        status="OK",
        is_available=True,
    )
    with patch("scripts.hardware_probe.probe_npu_device", return_value=mock_npu):
        summary = get_hardware_summary()
        assert summary["has_npu"] is True
        assert summary["npu_name"] == "Intel(R) AI Boost"

    with patch("scripts.hardware_probe.probe_npu_device", return_value=None):
        summary = get_hardware_summary()
        assert summary["has_npu"] is False
        assert summary["npu_name"] is None


def test_npu_does_not_match_usb_input_devices():
    """Logitech 等の USB 入力デバイス (Input を含む名称) が NPU として誤検知されないこと"""
    import subprocess
    import json
    
    # Logitech USB Input Device を模した PnP レスポンス
    fake_pnp_json = json.dumps({
        "Name": "Logitech USB Input Device",
        "DeviceID": "USB\\VID_046D&PID_C52B\\5&2A3B4C5D&0&1",
        "Status": "OK"
    })
    
    # 正常なフィルタリング条件の検証 (Intel NPU のみ許可、AMD NPU は除外)
    import re
    def is_npu(name: str, pnp_class: str, pnp_id: str = "") -> bool:
        has_npu_kw = bool(re.search(r"AI Boost|\bNPU\b|\bIPU\b", name)) or pnp_class == "ComputeAccelerator"
        is_not_input = not bool(re.search(r"Mouse|Keyboard|HIDClass|Media", pnp_class)) and not bool(re.search(r"Input|Receiver|Mouse|Keyboard|Audio|Controller", name))
        is_intel = "8086" in pnp_id or "Intel" in name or "AI Boost" in name
        is_not_amd = "AMD" not in name and "1002" not in pnp_id
        return has_npu_kw and is_not_input and is_intel and is_not_amd

    assert not is_npu("Logitech USB Input Device", "HIDClass")
    assert not is_npu("USB Input Device", "HIDClass")
    assert is_npu("Intel(R) AI Boost", "ComputeAccelerator", "PCI\\VEN_8086&DEV_7D1D")
    assert not is_npu("AMD NPU Device", "ComputeAccelerator", "PCI\\VEN_1002&DEV_1502")
    assert not is_npu("AMD IPU Device", "ComputeAccelerator", "PCI\\VEN_1002&DEV_17F0")


def test_amd_npu_suppressed_in_summary():
    """AMD NPU (AMD IPU / Ryzen AI) は OpenVINO 非対応のため has_npu=False となり CPU 推論が推奨されること"""
    amd_npu = NpuDevice(
        name="AMD IPU Device",
        vendor_name="AMD",
        device_id="PCI\\VEN_1002&DEV_17F0",
        status="Unsupported (AMD NPU is not supported by OpenVINO)",
        is_available=False,
    )
    devices = [
        GpuDevice(index=0, name="AMD Radeon 780M", vendor_id=0x1002, vendor_name="AMD",
                  device_id=0x15BF, is_integrated=True, is_discrete=False, is_software=False, vram_mb=512),
    ]
    with patch("scripts.hardware_probe.probe_gpu_devices", return_value=devices), \
         patch("scripts.hardware_probe.probe_npu_device", return_value=amd_npu):
        summary = get_hardware_summary()
        assert summary["has_npu"] is False
        assert summary["has_nvidia_gpu"] is False
        assert summary["recommended_device"] == "cpu"


def test_amd_gpu_only_environment():
    """AMD Radeon GPU のみの環境では has_nvidia_gpu=False となり CPU 推論が推奨されること"""
    devices = [
        GpuDevice(index=0, name="AMD Radeon RX 7900 XTX", vendor_id=0x1002, vendor_name="AMD",
                  device_id=0x744C, is_integrated=False, is_discrete=True, is_software=False, vram_mb=24576),
    ]
    with patch("scripts.hardware_probe.probe_gpu_devices", return_value=devices), \
         patch("scripts.hardware_probe.probe_npu_device", return_value=None):
        summary = get_hardware_summary()
        assert summary["has_nvidia_gpu"] is False
        assert summary["has_npu"] is False
        assert summary["recommended_device"] == "cpu"



def test_real_system_probe():
    """実機環境でのハードウェア走査がクラッシュせず実行できること"""
    devices = probe_gpu_devices()
    assert isinstance(devices, list)
    assert len(devices) > 0
    summary = get_hardware_summary()
    assert "has_igpu" in summary
    assert "has_npu" in summary
    assert "recommended_device" in summary

