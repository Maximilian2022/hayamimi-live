#!/usr/bin/env bash
set -e

# 異常終了時のトラップハンドラ (原因究明用)
trap 'rc=$?; if [ $rc -ne 0 ]; then echo "[entrypoint ERROR] コンテナ内プロセスが異常終了しました (ExitCode: $rc)" >&2; fi' EXIT

echo "=== hayamimi (早耳) 起動準備中 ==="
echo "[entrypoint] 引数: $*"

# モデルディレクトリの確認とダウンロード判定
MODELS_DIR="/app/models"
DOWNLOAD_ARGS=("--minimal")
NEED_DOWNLOAD=0

if [ ! -d "$MODELS_DIR" ] || [ -z "$(ls -A "$MODELS_DIR" 2>/dev/null)" ] || [ ! -d "$MODELS_DIR/sherpa-onnx-zipformer-ja-en-reazonspeech-2025-01-17" ]; then
    NEED_DOWNLOAD=1
fi

# 翻訳モデル (WebUIやダッシュボードで後から動的に有効化されるため標準準備)
DOWNLOAD_ARGS+=("--translators")
if [ ! -d "$MODELS_DIR/mojicast-fugumt-ja-en-ct2" ] || [ ! -d "$MODELS_DIR/mojicast-m2m100-ct2" ]; then
    NEED_DOWNLOAD=1
fi

if [[ "$*" == *"--en-tier v2"* ]] || [[ "$*" == *"--en-tier=v2"* ]]; then
    DOWNLOAD_ARGS+=("--en-parakeet-v2")
    if [ ! -d "$MODELS_DIR/sherpa-onnx-nemo-parakeet-tdt-0.6b-v2-int8" ]; then
        NEED_DOWNLOAD=1
    fi
fi

if [[ "$*" == *"--punct-model 4class"* ]] || [[ "$*" == *"--punct-model=4class"* ]]; then
    DOWNLOAD_ARGS+=("--punct-4class")
    if [ ! -d "$MODELS_DIR/punct-ja-4class-permissive" ]; then
        NEED_DOWNLOAD=1
    fi
fi

if [ $NEED_DOWNLOAD -eq 1 ]; then
    echo "セットアップ: モデルファイルをダウンロードしています (${DOWNLOAD_ARGS[*]})..."
    python scripts/download_models.py "${DOWNLOAD_ARGS[@]}"
    echo "モデルファイルの準備が完了しました。"
else
    echo "既存のモデルディレクトリを確認しました: $MODELS_DIR"
    echo "[entrypoint] 検出されたモデル一覧: $(ls -1 "$MODELS_DIR" 2>/dev/null | tr '\n' ' ')"
fi

if [ -d "/app/config" ]; then
    echo "[entrypoint] 永続化設定ディレクトリ: /app/config"
fi

echo "=== hayamimi サーバーを起動します ==="
echo "  OBS オーバーレイ:      http://0.0.0.0:8833/"
echo "  Webマイク入力画面:    http://0.0.0.0:8833/mic (HTTPS経由推奨: https://<HOST>:8843/mic)"
echo "  ダッシュボード:        http://0.0.0.0:8833/dashboard"
echo "  WebSocket 音声入力:    ws://0.0.0.0:8766/ingest"

echo "[entrypoint] realtime_transcribe.py を起動中..."
exec python scripts/realtime_transcribe.py --input ws --serve 8833 --serve-host 0.0.0.0 --ws-host 0.0.0.0 --ws-port 8766 "$@"
