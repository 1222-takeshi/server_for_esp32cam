# 運用 Runbook

ESP32-CAM スナップショットサーバーの日常運用・障害対応・バックアップ手順書です。

> **前提**: Docker Compose でデプロイ済み。デプロイ手順は [`docs/deployment.md`](../deployment.md) を参照。

---

## 目次

1. [日常確認](#1-日常確認)
2. [起動・停止・再起動](#2-起動停止再起動)
3. [更新・ロールバック](#3-更新ロールバック)
4. [ログ確認](#4-ログ確認)
5. [メトリクス確認](#5-メトリクス確認)
6. [バックアップ](#6-バックアップ)
7. [リストア](#7-リストア)
8. [ディスク容量対応](#8-ディスク容量対応)
9. [リストア訓練記録](#9-リストア訓練記録)

---

## 1. 日常確認

### ヘルスチェック

```bash
# Liveness (プロセスが生きているか)
curl http://localhost:8080/healthz

# Readiness (DB・ストレージ・推論ワーカーが正常か)
curl http://localhost:8080/healthz/ready
```

期待されるレスポンス例:

```json
{
  "status": "ready",
  "checks": {
    "database": "ok",
    "storage": "ok",
    "inference": "ok"
  }
}
```

いずれかの `checks` が `"ok"` 以外の場合は障害対応セクションへ進む。

### コンテナ状態確認

```bash
docker compose ps
docker compose logs --tail=50 app
```

---

## 2. 起動・停止・再起動

### 起動

```bash
docker compose up -d
```

### 停止

```bash
docker compose stop app
```

### 再起動 (設定変更後など)

```bash
docker compose restart app
```

### 完全停止 (コンテナ削除・ボリューム保持)

```bash
docker compose down
# ボリュームも削除する場合 (全データ消失): docker compose down -v
```

---

## 3. 更新・ロールバック

### 通常更新

```bash
git pull
docker compose up -d --build
```

DB スキーマは起動時に自動マイグレーションされます。手動操作は不要です。

### ロールバック手順

更新後に問題が発生した場合:

```bash
# 1. 現在のコンテナを停止
docker compose stop app

# 2. 前のコミットに戻す
git log --oneline -5          # 戻したいコミットの SHA を確認
git checkout <前のコミット SHA>

# 3. 再ビルドして起動
docker compose up -d --build

# 4. ヘルスチェックで確認
curl http://localhost:8080/healthz/ready
```

> DB スキーマのロールバックは非対応です。スキーマ変更を含む更新をロールバックする場合は、バックアップからのリストア（[セクション 7](#7-リストア)）を使用してください。

---

## 4. ログ確認

### アプリケーションログ

```bash
# 直近 100 行
docker compose logs --tail=100 app

# リアルタイム追跡
docker compose logs -f app

# エラーのみ抽出
docker compose logs app 2>&1 | grep '"level":"error"'

# 特定カメラのログ
docker compose logs app 2>&1 | grep '"camera_id":"<camera-id>"'
```

### ログの場所 (ファイル出力している場合)

`stdout` / `stderr` に JSON 形式で出力されます。永続化が必要な場合は Docker の logging driver または `docker compose logs` を定期的にファイルに保存してください。

---

## 5. メトリクス確認

Prometheus メトリクスは管理者トークンが必要です:

```bash
curl -H "Authorization: Bearer <ADMIN_TOKEN>" http://localhost:8080/metrics
```

主要メトリクス:

| メトリクス | 説明 |
|-----------|------|
| `upload_total` | アップロード成功件数 (カメラ別) |
| `upload_errors_total` | アップロードエラー件数 |
| `upload_duration_seconds` | アップロード処理時間 |
| `ws_connections_active` | 現在の WebSocket 接続数 |
| `storage_bytes_total` | ストレージ使用量 |

---

## 6. バックアップ

DB と画像ファイルは**必ずセットで**バックアップしてください。DB と画像ファイルが不整合になるとスナップショットが表示されなくなります。

### 手動バックアップ (Docker 停止あり / 最も確実)

```bash
BACKUP_DIR="./backup/$(date +%Y%m%d_%H%M%S)"
mkdir -p "$BACKUP_DIR/uploads"

# サーバーを停止
docker compose stop app

# DB をコピー
docker run --rm \
  -v server_for_esp32cam_data:/src \
  -v "$(pwd)/$BACKUP_DIR":/dst \
  busybox cp /src/esp32cam.sqlite3 /dst/esp32cam.sqlite3

# 画像ファイルをコピー
docker run --rm \
  -v server_for_esp32cam_uploads:/src \
  -v "$(pwd)/$BACKUP_DIR/uploads":/dst \
  busybox sh -c "cp -r /src/. /dst/"

# サーバーを再起動
docker compose start app

echo "バックアップ完了: $BACKUP_DIR"
```

### オンラインバックアップ (停止なし / WAL モード利用)

SQLite は WAL モードが有効になっているため、停止なしでもバックアップ可能です:

```bash
BACKUP_DIR="./backup/$(date +%Y%m%d_%H%M%S)"
mkdir -p "$BACKUP_DIR/uploads"

# DB をオンラインバックアップ (WAL ファイルが統合される)
docker run --rm \
  -v server_for_esp32cam_data:/src \
  -v "$(pwd)/$BACKUP_DIR":/dst \
  busybox sh -c "cp /src/esp32cam.sqlite3 /dst/esp32cam.sqlite3; sync"

# 画像ファイルをコピー
docker run --rm \
  -v server_for_esp32cam_uploads:/src \
  -v "$(pwd)/$BACKUP_DIR/uploads":/dst \
  busybox sh -c "cp -r /src/. /dst/"

echo "バックアップ完了: $BACKUP_DIR"
```

> オンラインバックアップ中に書き込みが発生した場合、画像ファイルと DB の間に最大数秒のずれが生じる可能性があります。重要な運用では停止バックアップを推奨します。

### バックアップの確認

```bash
# バックアップ内の DB が読み取れるか確認
sqlite3 "$BACKUP_DIR/esp32cam.sqlite3" "SELECT COUNT(*) FROM snapshots;"

# 画像ファイルの件数確認
find "$BACKUP_DIR/uploads" -name "*.jpg" | wc -l
```

---

## 7. リストア

```bash
BACKUP_DIR="./backup/<使用するバックアップのディレクトリ>"

# 1. サーバーを停止
docker compose stop app

# 2. DB をリストア
docker run --rm \
  -v server_for_esp32cam_data:/dst \
  -v "$(pwd)/$BACKUP_DIR":/src \
  busybox cp /src/esp32cam.sqlite3 /dst/esp32cam.sqlite3

# 3. 画像ファイルをリストア (既存を上書き)
docker run --rm \
  -v server_for_esp32cam_uploads:/dst \
  -v "$(pwd)/$BACKUP_DIR/uploads":/src \
  busybox sh -c "cp -r /src/. /dst/"

# 4. サーバーを起動
docker compose start app

# 5. ヘルスチェックで確認
curl http://localhost:8080/healthz/ready
```

リストア後に `"status": "ready"` が返れば完了です。

---

## 8. ディスク容量対応

### 使用量の確認

```bash
# ホスト側のディスク全体
df -h

# アップロードボリュームの使用量
docker run --rm \
  -v server_for_esp32cam_uploads:/data \
  busybox du -sh /data

# DBボリュームの使用量
docker run --rm \
  -v server_for_esp32cam_data:/data \
  busybox du -sh /data
```

### 対応フロー

| 使用率 | 対応 |
|--------|------|
| 〜 84% | 通常運用。`ESP32CAM_STORAGE_RETENTION_MAX_BYTES` を確認 |
| 85〜94% | **警告**: 保存期間短縮または手動削除を検討 |
| 95% 以上 | **緊急**: 即座に古いスナップショットを削除するか、アップロードを一時停止 |

### 85〜94% 時の対応

1. 不要なカメラのスナップショットを削除する:

   ```bash
   # カメラ一覧を確認
   curl -H "Authorization: Bearer <ADMIN_TOKEN>" \
     http://localhost:8080/api/cameras

   # 不要なカメラを削除 (スナップショットも同時削除)
   curl -X DELETE \
     -H "Authorization: Bearer <ADMIN_TOKEN>" \
     "http://localhost:8080/api/cameras/<camera_id>?delete_files=true"
   ```

2. 保存期間を短縮する (.env を更新して再起動):

   ```dotenv
   ESP32CAM_STORAGE_RETENTION_MAX_AGE_SECONDS=604800   # 7日間
   ESP32CAM_STORAGE_RETENTION_MAX_BYTES=1073741824      # 1 GiB
   ```

   ```bash
   docker compose restart app
   ```

### 95% 以上の緊急対応

```bash
# アップロードを一時停止 (コンテナを停止)
docker compose stop app

# 古い画像を手動で削除
docker run --rm \
  -v server_for_esp32cam_uploads:/data \
  busybox find /data -name "*.jpg" -mtime +30 -delete

# サーバーを再起動
docker compose start app
curl http://localhost:8080/healthz/ready
```

---

## 9. リストア訓練記録

バックアップが実際に機能することを定期的に確認してください。訓練結果を以下に追記します。

### 訓練手順

1. 現在の DB と画像ファイル件数を記録する
2. バックアップを取得する（セクション 6）
3. テスト用のダミーデータを追加する（例: 1 件アップロード）
4. リストアを実行する（セクション 7）
5. DB と画像ファイルの件数がバックアップ時点に戻っていることを確認する
6. `/healthz/ready` が `ready: true` を返すことを確認する

### 訓練ログ

| 実施日 | 担当 | 結果 | RTO (分) | 備考 |
|--------|------|------|---------|------|
| （初回訓練時に記入） | — | — | — | — |

---

## 関連ドキュメント

| ドキュメント | 内容 |
|------------|------|
| [`docs/deployment.md`](../deployment.md) | Docker デプロイガイド・環境変数一覧 |
| [`docs/ja/requirements.md`](requirements.md) | 非機能要件・受け入れ基準 |
| [`docs/ja/security.md`](security.md) | 脅威モデルと運用チェックリスト |
| [`docs/ja/troubleshooting.md`](troubleshooting.md) | トラブルシューティング |
