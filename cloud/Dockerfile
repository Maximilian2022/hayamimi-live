FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    DEBIAN_FRONTEND=noninteractive \
    PYTHONPATH=/app

WORKDIR /app

# 必要なOSパッケージをインストール
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    git \
    libsndfile1 \
    libportaudio2 \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Python依存パッケージのインストール
COPY cloud/requirements.txt requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# アプリケーションコードのコピー
COPY scripts/ /app/scripts/
COPY cloud/ /app/cloud/

# entrypoint スクリプトの改行コード正規化と権限付与
RUN sed -i 's/\r$//' /app/cloud/entrypoint.sh && chmod +x /app/cloud/entrypoint.sh

EXPOSE 8833

VOLUME ["/app/data", "/app/models"]

ENTRYPOINT ["/app/cloud/entrypoint.sh"]
