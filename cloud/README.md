# hayamimi-cloud (早耳 クラウド版)

知人（2〜3人）向けに、Windows 環境構築やアップデートなしで、ブラウザ（Webマイク）と OBS（ブラウザソース）だけでリアルタイム音声認識字幕を利用できるマルチテナント版です。

---

## 特徴
1. **Discord OAuth2 認証**:
   - Bot 作成や Discord サーバーへの導入は一切不要（`identify` スコープのみ）。
   - 事前に登録した Discord ユーザー ID のみアクセスを許可するホワイトリスト制御。
2. **省メモリ・高効率モデル共有型 ASR (ReazonSpeech CPU)**:
   - 1 つの ReazonSpeech モデル（約 250MB）をメモリ上で共有。
   - 複数人が同時に話しても、メモリ消費量は約 350MB（1人分と同等）に抑えられます。
3. **OBS 字幕 URL の完全分離**:
   - ユーザーごとに秘密の永続トークン付き URL（`/overlay?token=obs_sec_...`）を発行。
   - 自分の発話字幕だけが OBS に透過表示されます。
4. **個別置換辞書**:
   - ダッシュボードからユーザーごとに独自の置換辞書（`誤認識 => 正しい表記`）を編集・保存可能。

---

## サーバーへのデプロイ手順 (`mastercv.kit-a.net`)

### 1. リポジトリの取得
```bash
git clone https://github.com/Maximilian2022/hayamimi-live.git ~/hayamimi
cd ~/hayamimi
```

### 2. 環境変数設定 (`cloud/.env`)
`cloud/.env` を作成または確認します：
```bash
cp cloud/.env.example cloud/.env
# 必要に応じて nano cloud/.env で Discord Client Secret 等を設定
```

### 3. コンテナのビルド & 起動
```bash
cd ~/hayamimi/cloud
docker compose build
docker compose up -d
```
起動ログの確認：
```bash
docker compose logs -f
```
※ 初回起動時に、ReazonSpeech モデルと Silero VAD（約 250MB）が自動ダウンロードされます。

### 4. Cloudflare Tunnel の設定
Cloudflare Zero Trust ダッシュボード（Networks ➔ Tunnels）を開き、稼働中のトンネル（または新規トンネル）に Public Hostname を追加します：
- **Subdomain**: `hayamimi`
- **Domain**: `kit-a.net`
- **Type**: `HTTP`
- **URL**: `localhost:8833`
- **Additional application settings** ➔ **HTTP Settings**:
  - `No TLS Verify`: 必要に応じて ON
  - WebSocket は Cloudflare 既定でそのまま透過されます。

これで `https://hayamimi.kit-a.net` にアクセスすればログイン画面が表示されます！
