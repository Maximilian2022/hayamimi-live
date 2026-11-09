#!/usr/bin/env bash
set -e

echo "=== hayamimi-cloud 起動準備中 ==="

MODELS_DIR="/app/models"
REAZON_DIR="$MODELS_DIR/sherpa-onnx-zipformer-ja-en-reazonspeech-2025-01-17"
HAAS_DIR="$MODELS_DIR/haas-ja-onnx-int8dyn_mm"
VAD_FILE="$MODELS_DIR/silero_vad.onnx"

mkdir -p "$MODELS_DIR" "/app/data"

# 1. 必須コアモデル (ReazonSpeech, Silero VAD)
if [ ! -d "$REAZON_DIR" ] || [ ! -f "$VAD_FILE" ]; then
    echo "[entrypoint] 必須モデルファイル (ReazonSpeech, Silero VAD) をダウンロード中..."
    python scripts/download_models.py --minimal
    echo "[entrypoint] コアモデル準備完了。"
fi

# 2. 日本語 高精度 ASR モデル (Haas-ja-v1.0 INT8 約900MB)
if [ ! -d "$HAAS_DIR" ] || [ ! -f "$HAAS_DIR/encoder.onnx" ]; then
    echo "[entrypoint] Haas-ja-v1.0 モデル (約900MB) をダウンロード中..."
    python scripts/download_models.py --haas-ja
    echo "[entrypoint] Haas-ja-v1.0 準備完了。"
fi

# 3. ローカル翻訳モデル (FuguMT & M2M-100)
if [ ! -d "$MODELS_DIR/mojicast-fugumt-ja-en-ct2" ] || [ ! -d "$MODELS_DIR/mojicast-m2m100-ct2" ]; then
    echo "[entrypoint] ローカル翻訳モデル (FuguMT & M2M-100) をダウンロード中..."
    python scripts/download_models.py --translators
    echo "[entrypoint] 翻訳モデル準備完了。"
fi

echo "[entrypoint] 全モデル確認完了。"

echo "=== hayamimi-cloud ゲートウェイを起動します (Port: 8833) ==="
exec uvicorn cloud.gateway:app --host 0.0.0.0 --port 8833 --workers 1

