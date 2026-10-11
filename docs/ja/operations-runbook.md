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
9. [動画保持ポリシー](#9-動画保持ポリシー)
10. [リストア訓練記録](#10-リストア訓練記録)

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
  "db": "ok",
  "storage": "ok",
  "disk_free_mb": 1024,
  "inference_worker": "running"
}
```

HTTP 503 または `status: degraded` の場合は DB、保存先、空き容量、推論ワーカーを確認します。

### 7 日間の安定性観測（Raspberry Pi の user systemd）

同梱の one-shot sampler はローカル `/healthz/ready` と指定カメラの
`/api/cameras/<id>/connection` を問い合わせ、ホストの空き容量・1 分負荷・
利用可能メモリを 1 行の JSON として追記します。カメラ ID は対象機のものを
指定してください。API key や認証 URL は不要で、レスポンス全体や秘密を記録しません。

```bash
mkdir -p ~/.config/systemd/user
cp scripts/systemd/esp32cam-observe.{service,timer} ~/.config/systemd/user/
# service の ExecStart に --camera-id <対象ID> を追加する（複数なら繰り返す）
systemctl --user daemon-reload
systemctl --user enable --now esp32cam-observe.timer
systemctl --user list-timers esp32cam-observe.timer
```

5 分間隔で 7 日間なら約 2016 行です。`~/.local/state/esp32cam/observations.jsonl`
と `.jsonl.1` に各最大 8 MiB を保持します。ローテーション後に古い行は消えます。
全期間を残すには 7 日終了時に両ファイルを別媒体へコピーし、行数・最初と最後の
`timestamp_utc` を確認してください。収集開始前の時間は実測期間に含めません。
readiness の `error` と各 camera の `error` は独立して記録されます。
`status: offline` は通信途絶、`error: unreachable` は監視側の問い合わせ失敗です。
`load_1m` は CPU 使用率ではなく、CPU 数と比較するための 1 分平均負荷です。
観測が終わったら `systemctl --user disable --now esp32cam-observe.timer` で停止します。

### 7 日間の記録を評価する

評価対象の `.jsonl.1` と `.jsonl` を、収集停止後に別ディレクトリへコピーして
入力を固定します。各入力は最大 8 MiB、合計 2 ファイルまでです。開始と終了は
**UTC**で指定し、期間は半開区間 `[start,end)` とします。例えば開始が
2026-10-11 12:02:49 JST なら UTC の `2026-10-11T03:02:49+00:00` です。

```bash
python3 scripts/summarize_observations.py \
  --input observation-older.jsonl --input observation-current.jsonl \
  --start 2026-10-11T03:02:49+00:00 --end 2026-10-18T03:02:49+00:00 \
  --interval-seconds 300 --camera-id <対象ID> --output observation-summary.json
```

前後の記録は `outside_before` / `outside_after` に分けて除外します。5分のカレンダー
timer と開始時刻の位相差を許容するため、開始・終了境界と記録間の最大許容間隔は
`interval_seconds + 60` 秒です。これを超えると `gap_count` に計上します。
さらに期間長から `ceil(期間秒数 / interval_seconds)` の期待件数を計算し、
一意の有効サンプル数が100%に達しない場合も不合格にします。ジッター許容を
360秒周期への変更として扱わないためです。
同じ timestamp の重複は分母に入れず、invalid 行とともに合格を防ぎます。
監視側の問い合わせ失敗は `monitor_failure`、カメラの通信途絶は `offline` として
別計数します。結果には評価設定・入力SHA-256・件数・理由を保存し、既存の出力
ファイルは上書きしません。終了時刻が未来なら `pending`（終了コード3）、期間が
7日未満や欠測などがあれば `failed`（終了コード1）です。

固定の99%基準は、取得できた有効サンプルにおける `ready` / `healthy` **比率**です。
実時間の uptime 99% を証明する指標ではありません。7日経過・欠測なし・重複なし・
invalidなしを満たした結果でも、別途サービスログと復旧記録を照合してください。

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

### オンラインバックアップ (停止なし / DB のみ整合)

ホストの `scripts/backup.sh` を使う場合は `sqlite3` CLI が必要です。
Raspberry Pi の Ubuntu では最初に `sudo apt-get update && sudo apt-get install sqlite3`
で導入し、`command -v sqlite3` で確認してください。このスクリプトは DB だけを
取得します。画像を含む完全な対が必要なら停止バックアップを使用します。

SQLite は WAL モードが有効になっているため、停止なしでもバックアップ可能です:

```bash
BACKUP_DIR="./backup/$(date +%Y%m%d_%H%M%S)"
mkdir -p "$BACKUP_DIR/uploads"

# SQLite の online backup API で WAL を含む整合した DB を取得する
docker run --rm \
  -v server_for_esp32cam_data:/src:ro \
  -v "$(pwd)/$BACKUP_DIR":/dst \
  python:3.11-slim python -c 'import sqlite3; src=sqlite3.connect("file:/src/esp32cam.sqlite3?mode=ro", uri=True); dst=sqlite3.connect("/dst/esp32cam.sqlite3"); src.backup(dst); dst.close(); src.close()'

# 画像ファイルをコピー
docker run --rm \
  -v server_for_esp32cam_uploads:/src \
  -v "$(pwd)/$BACKUP_DIR/uploads":/dst \
  busybox sh -c "cp -r /src/. /dst/"

echo "バックアップ完了: $BACKUP_DIR"
```

> DB ファイルを `cp` するだけでは WAL 内の未checkpointデータが欠落し得ます。
> online backup API が保証するのは DB 自体の整合性です。画像とのコピー時点は
> 原子的に揃わず、保存処理や retention と競合し得ます。完全な対を取得するには
> 上の停止バックアップを使ってください。

### バックアップの確認

```bash
# バックアップ内の DB が読み取れるか確認
sqlite3 "$BACKUP_DIR/esp32cam.sqlite3" "SELECT COUNT(*) FROM snapshots;"

# 画像ファイルの件数確認
find "$BACKUP_DIR/uploads" -name "*.jpg" | wc -l

# 元データを変更せず、隔離コピーした DB と画像参照を検査する
python3 scripts/check_backup.py --db "$BACKUP_DIR/esp32cam.sqlite3" \
  --uploads "$BACKUP_DIR/uploads"
```

`status: passed` は SQLite quick_check と全 snapshot/video のファイル存在・
サイズが一致したことを示します。`failed` のときは復元に使わず、JSON の
`missing_files`、`size_mismatches`、`invalid_paths`、`incomplete` を確認します。
`error: backup_read_failed` は破損DBや必要テーブル欠落などで読み取りに失敗した
ことを示します。`error: backup_input_missing_or_unsafe` は DB/画像dir欠落、
DB symlink、WAL sidecar存在を示します。正しいバックアップ一式を選び直してください。
この検査はアプリ起動や画像表示の成功までは保証しません。

---

## 7. リストア

まず前節の検査をバックアップに実行し、停止を伴う復元手順を別の隔離環境で
演習してください。本番復元の前に現在のデータを保全し、復元後は readiness と
画像の表示を確認します。readiness だけでは画像と DB の対応は検証できません。

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

### 無通信時の定期ローテーション

画像・動画の保持処理はアップロード時だけでなく、既定で1時間ごとにバックグラウンド実行されます。カメラが停止している間も、`ESP32CAM_STORAGE_RETENTION_MAX_AGE_SECONDS` / `ESP32CAM_STORAGE_RETENTION_MAX_BYTES` と動画用の同等設定が適用されます。scheduler はアプリケーションプロセスごとに1本起動するため、複数 Gunicorn worker を使う場合は各 worker が実行します。1本だけ動かす場合は `-w 1` を使用してください。

```dotenv
# 1時間ごとに実行（0でバックグラウンド実行を無効化）
ESP32CAM_RETENTION_SCHEDULER_INTERVAL_SECONDS=3600
```

削除実績とエラーはアプリケーションログで確認できます。systemd ユーザーサービスの場合は次を実行してください。

```bash
journalctl --user -u esp32cam-server --since "1 hour ago" \
  | grep -E "retention deleted|Scheduled retention failed"
```

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

## 9. 動画保持ポリシー

動画ファイルはアップロードごとに保持ポリシーが適用されます。デフォルトは無効（`0`）です。

### 容量見積もり

| 解像度 | 平均ビットレート | 1 分あたりのサイズ | 1 日 (24 時間) |
|--------|----------------|-------------------|----------------|
| 640×480 (VGA) | 500 kbps | 約 3.75 MB | 約 5.4 GB |
| 1600×1200 (UXGA) | 2 Mbps | 約 15 MB | 約 21.6 GB |

> 実際のサイズはフレームレートや映像の動きによって変動します。

### 保持ポリシーの設定例

**例 1: 7 日間保持（期間ベース）**

```dotenv
ESP32CAM_VIDEO_RETENTION_MAX_AGE_SECONDS=604800   # 7日 = 7 * 86400
```

**例 2: 合計 5 GiB 以内に収める（サイズベース）**

```dotenv
ESP32CAM_VIDEO_RETENTION_MAX_BYTES=5368709120     # 5 GiB = 5 * 1024^3
```

**例 3: 期間とサイズの両方を設定（より厳しい方が優先）**

```dotenv
ESP32CAM_VIDEO_RETENTION_MAX_AGE_SECONDS=604800   # 7日
ESP32CAM_VIDEO_RETENTION_MAX_BYTES=5368709120     # 5 GiB
```

設定変更後はサーバーを再起動してください:

```bash
docker compose restart app
```

### 保持ポリシーの動作確認

```bash
# 動画ファイルの件数と合計サイズを確認
docker run --rm \
  -v server_for_esp32cam_uploads:/data \
  busybox sh -c "find /data -name '*.mp4' | wc -l && du -sh /data"

# 動画ファイルの削除ログを確認
docker compose logs app 2>&1 | grep "Video retention deleted"
```

### アップロードレート制限の調整

デフォルトは 1 デバイスあたり 5 リクエスト/分です。変更が必要な場合:

```dotenv
ESP32CAM_VIDEO_UPLOAD_RATE_LIMIT=10 per minute
```

---

## 10. リストア訓練記録

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
