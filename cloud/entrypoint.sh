#!/usr/bin/env bash
set -e

echo "=== hayamimi-cloud 起動準備中 ==="

MODELS_DIR="/app/models"
REAZON_DIR="$MODELS_DIR/sherpa-onnx-zipformer-ja-en-reazonspeech-2025-01-17"
VAD_FILE="$MODELS_DIR/silero_vad.onnx"

mkdir -p "$MODELS_DIR" "/app/data"

# ReazonSpeech または Silero VAD が未ダウンロードの場合、自動取得
if [ ! -d "$REAZON_DIR" ] || [ ! -f "$VAD_FILE" ]; then
    echo "[entrypoint] 必要なモデルファイル (ReazonSpeech & Silero VAD) をダウンロード中..."
    python scripts/download_models.py --minimal
    echo "[entrypoint] モデル準備完了。"
else
    echo "[entrypoint] 既存モデルを確認: $REAZON_DIR"
fi

echo "=== hayamimi-cloud ゲートウェイを起動します (Port: 8833) ==="
exec uvicorn cloud.gateway:app --host 0.0.0.0 --port 8833 --workers 1

