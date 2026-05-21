# 複数カメラ対応 設計ドキュメント

## 1. 概要

本ドキュメントは、複数の ESP32-CAM デバイスからの画像を単一サーバーで管理するためのアーキテクチャ設計を記述する。

**実装状況:** 本設計の主要部分は実装完了済み。

---

## 2. ディレクトリレイアウト

### 保存構造

```
uploads/
├── <camera_id>/
│   └── images/
│       ├── image_20260101T120000000000_<uuid>.jpg
│       ├── image_20260101T120000000000_<uuid>.jpg.json   # サイドカーメタデータ
│       └── ...
└── <another_camera_id>/
    └── images/
        └── ...
```

### サイドカーメタデータ形式（`.jpg.json`）

```json
{
  "device_id": "esp32cam-front-door",
  "captured_at": "2026-01-01T12:00:00+00:00"
}
```

### ポイント

- `<camera_id>` はデバイスが送信する `X-Device-Id` ヘッダの値（空白を除去・正規化済み）
- カメラIDが不正（空文字等）の場合は `device metadata is required` エラーを返す
- レガシーデータ（カメラIDなし）は `uploads/` 直下の `.jpg` を `list_images()` でスキャンして取り込む

---

## 3. データモデル（SQLite）

### テーブル構成

```sql
cameras (
    camera_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    expected_interval_seconds INTEGER NOT NULL,  -- ヘルス判定に使用
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1
)

snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    filename TEXT NOT NULL UNIQUE,
    relative_path TEXT NOT NULL UNIQUE,
    size INTEGER NOT NULL,
    camera_id TEXT,                              -- NULL = レガシーデータ
    captured_at TEXT,
    stored_at TEXT NOT NULL,
    inference_status TEXT NOT NULL,              -- pending / processing / succeeded / failed / skipped
    inference_error TEXT,
    FOREIGN KEY(camera_id) REFERENCES cameras(camera_id)
)

device_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    camera_id TEXT NOT NULL,
    level TEXT NOT NULL,                         -- info / warning / error / critical
    message TEXT NOT NULL,
    logged_at TEXT NOT NULL,
    FOREIGN KEY(camera_id) REFERENCES cameras(camera_id) ON DELETE CASCADE
)
```

### カメラステータス判定ロジック

| 状態 | 条件 |
|------|------|
| `healthy` | 最終受信から `expected_interval_seconds × 2` 以内 |
| `stale` | 最終受信から `expected_interval_seconds × 6` 以内 |
| `offline` | それ以上、または未受信 |

デフォルト `expected_interval_seconds = 300`（環境変数 `ESP32CAM_CAMERA_EXPECTED_INTERVAL_SECONDS` で変更可能）

---

## 4. API 設計

### 画像アップロード

| エンドポイント | メソッド | 説明 |
|---|---|---|
| `/upload` | POST | JPEG バイナリをアップロード。`X-Device-Id`・`X-Captured-At` ヘッダ必須 |

**リクエスト例:**
```
POST /upload
Content-Type: image/jpeg
X-Device-Id: esp32cam-front-door
X-Captured-At: 2026-01-01T12:00:00Z

<JPEG binary>
```

### UI ページ

| エンドポイント | 説明 |
|---|---|
| `/` | カメラ一覧ダッシュボード（ステータス・最終スナップショット） |
| `/cameras` | カメラ一覧 |
| `/cameras/<camera_id>` | カメラ詳細（最新スナップショット・推論結果） |
| `/files` | 全スナップショット一覧 |

### REST API

| エンドポイント | メソッド | 説明 |
|---|---|---|
| `/api/cameras` | GET | カメラ一覧（ステータス付き）|
| `/api/cameras/<camera_id>/snapshots` | GET | カメラ別スナップショット一覧 |
| `/api/cameras/<camera_id>/upload-metrics` | GET | アップロード診断メトリクス |
| `/api/cameras/<camera_id>/logs` | GET | デバイスログ一覧 |
| `/api/detections` | GET | 検出結果一覧（`?camera_id=`・`?since=` フィルタ対応）|
| `/api/status_update` | POST | ハートビート（画像なしのオンライン通知）|
| `/api/log_error` | POST | デバイスエラーログ送信 |

### WebSocket

| エンドポイント | 説明 |
|---|---|
| `/ws/snapshots` | 全カメラのスナップショット作成イベントをリアルタイム配信 |
| `/ws/cameras/<camera_id>/snapshots` | カメラ別スナップショット作成イベント配信 |

---

## 5. 環境変数

| 変数名 | デフォルト | 説明 |
|---|---|---|
| `ESP32CAM_UPLOAD_DIR` | `./uploads` | 画像保存ルートディレクトリ |
| `ESP32CAM_DB_PATH` | `./esp32cam.sqlite3` | SQLite DB パス |
| `ESP32CAM_CAMERA_EXPECTED_INTERVAL_SECONDS` | `300` | ヘルス判定の基準間隔（秒）|
| `ESP32CAM_MAX_CONTENT_LENGTH` | `1048576` (1 MB) | アップロード上限サイズ（バイト）|
| `ESP32CAM_STORAGE_RETENTION_MAX_AGE_SECONDS` | `0`（無効）| 保持期間上限（秒）|
| `ESP32CAM_STORAGE_RETENTION_MAX_BYTES` | `0`（無効）| 保持容量上限（バイト）|

---

## 6. レガシーデータ移行方針

`uploads/` 直下に直置きされた旧形式の JPEG（`camera_id` なし）は `list_images()` の `rglob("*.jpg")` によって自動的に取り込まれ、`snapshots.camera_id = NULL` として登録される。強制的なデータ移行は不要。

---

## 7. 今後の拡張ポイント

以下は現時点では未実装。Issue として管理する。

| 課題 | 内容 | 優先度 |
|---|---|---|
| カメラの `display_name` 設定 API | 現在は `camera_id` と同一。`PATCH /api/cameras/<id>` 等で変更できると運用しやすい | Medium |
| `expected_interval_seconds` のカメラ別設定 | 現在はサーバー全体で統一。カメラごとに撮影間隔が異なる場合に対応 | Low |
| ログのページネーション | `GET /api/cameras/<id>/logs` が最新50件固定。`?limit=&offset=` 対応 | Low |
| カメラ削除 API | `DELETE /api/cameras/<id>` および関連スナップショット・ログの CASCADE 削除 | Low |
| クラウドストレージバックエンド | `ESP32CAM_STORAGE_BACKEND=s3` 等。`Storage` Protocol はすでに実装済み | Low |
