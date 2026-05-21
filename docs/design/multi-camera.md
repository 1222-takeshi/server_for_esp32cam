# 複数カメラ対応 設計ドキュメント

## 1. 概要

本ドキュメントは、複数の ESP32-CAM デバイスからの画像を単一サーバーで管理するためのアーキテクチャ設計を記述する。

---

## 2. 実装フェーズ

### 完了済み

| 機能 | 実装ファイル |
|---|---|
| カメラ別ディレクトリ保存（`uploads/<camera_id>/images/`）| `script/storage.py` |
| SQLite カメラ管理（`cameras` テーブル）| `script/metadata.py` |
| SQLite スナップショット管理（`snapshots` テーブル）| `script/metadata.py` |
| SQLite デバイスログ管理（`device_logs` テーブル）| `script/metadata.py` |
| カメラ一覧・詳細 UI（`/cameras`, `/cameras/<id>`）| `script/routes.py`, `templates/` |
| カメラ別スナップショット API（`/api/cameras/<id>/snapshots`）| `script/routes.py` |
| カメラヘルス判定（healthy / stale / offline）| `script/metadata.py` |
| WebSocket リアルタイム配信（`/ws/snapshots`, `/ws/cameras/<id>/snapshots`）| `script/websocket_routes.py` |
| WebSocket バイナリアップロード（`/ws/cameras/<id>/upload`）| `script/websocket_routes.py` |
| ハートビートエンドポイント（`/api/status_update`）| `script/routes.py` |
| エラーログエンドポイント（`/api/log_error`）| `script/routes.py` |
| アップロード保持ポリシー（最大容量・最大保持期間）| `script/retention.py` |

### 未実装（今後の課題）

| 機能 | 優先度 | Issue |
|---|---|---|
| カメラの `display_name` 設定 API | Medium | #83 |
| `expected_interval_seconds` のカメラ別設定 | Low | #83 |
| ログのページネーション（`?limit=&offset=`）| Low | #80 |
| カメラ削除 API | Low | #84 |
| クラウドストレージバックエンド（S3 等）| Low | — |

---

## 3. CI 影響分析

複数カメラ対応の実装は段階的に main に取り込まれており、各 PR で CI がパスしていることを確認済み。

| CI ジョブ | 影響 |
|---|---|
| `pytest` | カメラ別保存・メタデータ管理・APIの各層にユニット・結合テストを追加済み。全 93 件パス |
| `ruff` | 新規コードはすべて lint パス済み |
| ESP32 ファームウェアビルド | サーバー変更の影響なし（ファームウェア側は HTTP/WebSocket の仕様変更なし）|

新機能追加時に CI を壊さないための方針：
- `tests/test_routes.py`: ルート層の正常系・異常系を網羅
- `tests/test_metadata.py`: ストア層のユニットテスト担当
- `tests/conftest.py`: `tmp_path` による一時 DB・一時アップロードディレクトリを使用（副作用なし）

---

## 4. ディレクトリレイアウト

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

UTC に正規化された ISO 8601 形式で保存される。

```json
{
  "device_id": "esp32cam-front-door",
  "captured_at": "2026-01-01T12:00:00+00:00"
}
```

### ポイント

- `<camera_id>` はデバイスが送信する `X-Device-Id` ヘッダの値（空白を除去・正規化済み）
- `X-Device-Id` または `X-Captured-At` ヘッダが欠損・空の場合は HTTP 400 `"Missing device metadata"` を返す
- レガシーデータ（カメラIDなし）は `list_images()` の `rglob("*")` によって `.jpg`/`.jpeg` 拡張子のファイルが自動的に取り込まれる

---

## 5. データモデル（SQLite）

### テーブル構成

```sql
cameras (
    camera_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,            -- 現在は camera_id と同一
    expected_interval_seconds INTEGER NOT NULL,  -- ヘルス判定に使用（サーバー全体で統一）
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1   -- 現状は常に 1。削除APIは未実装
)

snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    filename TEXT NOT NULL UNIQUE,
    relative_path TEXT NOT NULL UNIQUE,
    size INTEGER NOT NULL,
    camera_id TEXT,                        -- NULL = レガシーデータ
    captured_at TEXT,
    stored_at TEXT NOT NULL,
    inference_status TEXT NOT NULL,        -- pending / processing / succeeded / failed / skipped
    inference_error TEXT,
    FOREIGN KEY(camera_id) REFERENCES cameras(camera_id)
)

device_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    camera_id TEXT NOT NULL,
    level TEXT NOT NULL,                   -- info / warning / error / critical
    message TEXT NOT NULL,                 -- 最大 2000 文字
    logged_at TEXT NOT NULL,
    FOREIGN KEY(camera_id) REFERENCES cameras(camera_id) ON DELETE CASCADE
)

detections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_id INTEGER NOT NULL,
    label TEXT NOT NULL,
    score REAL NOT NULL,
    x1 REAL NOT NULL,
    y1 REAL NOT NULL,
    x2 REAL NOT NULL,
    y2 REAL NOT NULL,
    FOREIGN KEY(snapshot_id) REFERENCES snapshots(id) ON DELETE CASCADE
)
```

### カメラステータス判定ロジック

| 状態 | 条件 |
|------|------|
| `healthy` | 最終受信から `expected_interval_seconds × 2` 以内 |
| `stale` | 最終受信から `expected_interval_seconds × 6` 以内 |
| `offline` | それ以上、または未受信 |

デフォルト `expected_interval_seconds = 300`（環境変数 `ESP32CAM_CAMERA_EXPECTED_INTERVAL_SECONDS` で変更可能）

~~**既知の制約:** `list_camera_summaries()` はカメラ数 N に対して 2N+1 クエリが発生する（N+1 問題）。~~
**解決済み（#82）:** `ROW_NUMBER()` ウィンドウ関数と `GROUP BY` バッチクエリへの置き換えにより、固定 4 クエリに最適化済み。

---

## 6. API 設計

### 画像アップロード

| エンドポイント | メソッド | 成功レスポンス | 説明 |
|---|---|---|---|
| `/upload` | POST | `200 OK` | JPEG バイナリ。`X-Device-Id`・`X-Captured-At` ヘッダ必須 |

**リクエスト例:**
```
POST /upload
Content-Type: image/jpeg
X-Device-Id: esp32cam-front-door
X-Captured-At: 2026-01-01T12:00:00Z

<JPEG binary>
```

**主なエラーレスポンス:**

| コード | 条件 |
|---|---|
| 400 | ボディ空・JPEG 不正・ヘッダ欠損 |
| 413 | `MAX_CONTENT_LENGTH` 超過 |
| 415 | `Content-Type: image/jpeg` 以外 |

### UI ページ

| エンドポイント | 説明 |
|---|---|
| `/` | カメラ一覧ダッシュボード（ステータス・最終スナップショット） |
| `/cameras` | カメラ一覧 |
| `/cameras/<camera_id>` | カメラ詳細（最新スナップショット・推論結果） |
| `/files` | 全スナップショット一覧 |

### REST API

| エンドポイント | メソッド | 成功レスポンス | 説明 |
|---|---|---|---|
| `/healthz` | GET | `200` | サーバー稼働確認（`{"status": "ok"}`）|
| `/api/cameras` | GET | `200` | カメラ一覧（ステータス付き）|
| `/api/cameras/<camera_id>/snapshots` | GET | `200` | カメラ別スナップショット一覧（最新50件）|
| `/api/cameras/<camera_id>/upload-metrics` | GET | `200` | アップロード診断メトリクス |
| `/api/cameras/<camera_id>/logs` | GET | `200` | デバイスログ一覧（最新50件）|
| `/api/detections` | GET | `200` | 検出結果（`?camera_id=`・`?since=` フィルタ対応）|
| `/api/status_update` | POST | `200` | ハートビート（画像なしのオンライン通知）|
| `/api/log_error` | POST | `201` | デバイスエラーログ送信 |

**共通エラーレスポンス形式:**
```json
{ "message": "<エラー内容>" }
```

### WebSocket

| エンドポイント | 説明 |
|---|---|
| `/ws/cameras/<camera_id>/upload` | デバイス → サーバー: JPEG バイナリのリアルタイムアップロード |
| `/ws/snapshots` | サーバー → クライアント: 全カメラのスナップショット作成イベント配信 |
| `/ws/cameras/<camera_id>/snapshots` | サーバー → クライアント: カメラ別スナップショット作成イベント配信 |

---

## 7. 環境変数

| 変数名 | デフォルト | 説明 |
|---|---|---|
| `ESP32CAM_UPLOAD_DIR` | `./uploads` | 画像保存ルートディレクトリ |
| `ESP32CAM_DB_PATH` | `./esp32cam.sqlite3` | SQLite DB パス |
| `ESP32CAM_STORAGE_BACKEND` | `local` | ストレージバックエンド（現在 `local` のみ対応）|
| `ESP32CAM_CAMERA_EXPECTED_INTERVAL_SECONDS` | `300` | ヘルス判定の基準間隔（秒）|
| `ESP32CAM_MAX_CONTENT_LENGTH` | `1048576` (1 MB) | アップロード上限サイズ（バイト）|
| `ESP32CAM_STORAGE_RETENTION_MAX_AGE_SECONDS` | `0`（無効）| 保持期間上限（秒）|
| `ESP32CAM_STORAGE_RETENTION_MAX_BYTES` | `0`（無効）| 保持容量上限（バイト）|
| `ESP32CAM_INFERENCE_MODE` | `async` | 推論実行モード（`sync` / `async`）|
| `ESP32CAM_DETECTOR` | `disabled` | 推論バックエンド（`disabled` / `tflite_cpu`）|
| `ESP32CAM_MODEL_PATH` | `""` | TFLite モデルファイルパス |
| `ESP32CAM_LABELS_PATH` | `""` | ラベルファイルパス |
| `ESP32CAM_DETECTION_THRESHOLD` | `0.5` | 検出スコア閾値 |
| `ESP32CAM_PERSON_LABEL` | `person` | 人物ラベル名 |
| `ESP32CAM_PERSON_CLASS_ID` | `0` | 人物クラスID |
| `PORT` | `8080` | サーバーポート番号 |

---

## 8. レガシーデータ移行方針

`uploads/` 直下に直置きされた旧形式の JPEG（`camera_id` なし）は `list_images()` の `rglob("*")` によって `.jpg`/`.jpeg` 拡張子のファイルが自動的に取り込まれ、`snapshots.camera_id = NULL` として登録される。強制的なデータ移行は不要。

---

## 9. スケール・運用上の注意

| 項目 | 現状の上限目安 | 超過時の対策 |
|---|---|---|
| 同時アップロード | SQLite の書き込みロックにより事実上シリアル | PostgreSQL 等への移行を検討 |
| カメラ台数 | 固定 4 クエリで動作（#82 で N+1 解消済み）| 現在のアーキテクチャで対応不要 |
| 画像蓄積量 | 保持ポリシー（`RETENTION_MAX_BYTES`）で制御可能 | クラウドストレージへの移行（`Storage` Protocol 実装済み）|
