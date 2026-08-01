# 動画アップロード API / 保存方式 設計

## 1. 概要

本ドキュメントは、ESP32-CAM から短い動画クリップをサーバーへアップロードし、カメラ単位で保存・一覧・配信するための設計草案をまとめる。

現行の本線は JPEG スナップショット upload と deep sleep 前提の低電力運用である。動画機能は常時ライブ配信ではなく、スナップショットでは状況把握が足りない場面だけに限定した **短い clip upload** として追加する。

Issue: [#35 動画アップロードAPIと保存方式の設計](https://github.com/1222-takeshi/server_for_esp32cam/issues/35)

---

## 2. 目的と非目的

### 目的

- `POST /api/cameras/<camera_id>/videos` による動画アップロード API の方針を定義する。
- `uploads/<camera_id>/videos/` 配下の保存方式、メタデータ、DB スキーマを定義する。
- 動画一覧 API と動画配信 API の草案を定義する。
- 大容量アップロード時のサイズ上限、タイムアウト、エラーハンドリング方針を定義する。
- 実装 PR を小さく分割できる段階実装計画を作る。

### 非目的

- 常時ライブストリーミングを実装する。
- ブラウザでの動画編集・トリミングを実装する。
- ESP32-CAM 側で高度なエンコード制御を実装する。
- クラウドストレージや CDN を初期実装に含める。
- 長時間録画や監視カメラ NVR の代替を目指す。

---

## 3. 設計方針

### 3.1 短い動画クリップに限定する

ESP32-CAM は RAM、帯域、電力に制約があるため、MVP では以下を目安にする。

| 項目 | MVP 方針 |
|---|---|
| クリップ長 | 1〜10 秒程度 |
| 形式 | `video/mp4` を第一候補、実機制約が厳しければ `image/jpeg` 連番 + サーバー側変換を後続検討 |
| アップロード方式 | HTTP multipart/form-data または raw binary |
| 保存先 | ローカルファイルシステム |
| UI | 一覧と個別再生のみ |

実機で MP4 生成が重い場合は、ファームウェア MVP では「短時間に複数 JPEG を撮って zip/tar 風に送る」方式を別 ADR で再評価する。ただしサーバー API は最初から `videos` リソースとして扱い、内部表現を隠蔽する。

### 3.2 既存画像アップロードと同じ camera-aware 構造を使う

画像と同様に、動画もカメラ ID を必須にする。URL path に `camera_id` を含めることで、ヘッダ欠損による保存先誤りを避ける。

`camera_id` は保存パスの一部になるため、実装では既存のカメラ ID 正規化に加えて、動画 API でも `^[A-Za-z0-9_-]{1,64}$` のような安全な許可リストで検証する。`/`、`..`、空白、URL encode された path separator、制御文字を含む値は `400 Bad Request` で拒否し、ユーザー入力をそのまま `Path` に連結しない。

```text
uploads/
└── <camera_id>/
    ├── images/
    └── videos/
        ├── video_20260601T120000000000_<uuid>.mp4
        └── video_20260601T120000000000_<uuid>.mp4.json
```

### 3.3 API key の扱い

デバイスからの動画アップロードは既存のデバイス認証方針に合わせる。

- `X-API-Key` を要求する。
- `ESP32CAM_DEVICE_API_KEYS` の `camera_id` と key の対応を検証する。
- 動画アップロードは fail-closed とし、該当 `camera_id` の key が設定されていない場合は `403 Forbidden` で拒否する。
- 単一デバイス用の `ESP32CAM_DEVICE_API_KEY` を使う場合は `ESP32CAM_SHARED_KEY_DEVICE_ID` で `camera_id` を固定する。複数台の本番運用では `ESP32CAM_DEVICE_API_KEYS` を使い、共有キーを使い回さない。
- 管理者向け削除・設定変更は `Authorization: Bearer <ESP32CAM_ADMIN_TOKEN>` を使う。

API key の比較は `hmac.compare_digest` 相当の定数時間比較を使い、サーバーログ・エラーレスポンス・テスト出力へ key を出さない。HTTP 平文で `X-API-Key` を流さないため、本番デプロイでは reverse proxy などで TLS/HTTPS 終端を必須にする。key のローテーション・失効は `ESP32CAM_DEVICE_API_KEYS` の更新とデバイス再設定で扱い、将来必要になったら key version を追加する。

---

## 4. API 草案

### 4.1 動画アップロード

| 項目 | 内容 |
|---|---|
| Endpoint | `POST /api/cameras/<camera_id>/videos` |
| Auth | `X-API-Key` |
| Content-Type | `video/mp4`, `multipart/form-data` |
| 成功 | `201 Created` |

#### raw binary 例

```http
POST /api/cameras/esp32cam-01/videos HTTP/1.1
Content-Type: video/mp4
X-API-Key: <device-api-key>
X-Captured-At: 2026-06-01T12:00:00Z
X-Duration-Ms: 5000

<MP4 binary>
```

#### multipart 例

```http
POST /api/cameras/esp32cam-01/videos HTTP/1.1
Content-Type: multipart/form-data; boundary=...
X-API-Key: <device-api-key>

file=@clip.mp4;type=video/mp4
captured_at=2026-06-01T12:00:00Z
duration_ms=5000
```

#### 成功レスポンス

```json
{
  "id": 123,
  "camera_id": "esp32cam-01",
  "filename": "video_20260601T120000000000_0123456789abcdef.mp4",
  "relative_path": "esp32cam-01/videos/video_20260601T120000000000_0123456789abcdef.mp4",
  "size": 524288,
  "captured_at": "2026-06-01T12:00:00+00:00",
  "duration_ms": 5000,
  "content_type": "video/mp4"
}
```

### 4.2 動画一覧

| Endpoint | Auth | 説明 |
|---|---|---|
| `GET /api/cameras/<camera_id>/videos?limit=50&before=<id>` | admin or read-only UI session | 指定カメラの動画を新しい順に返す |
| `GET /api/videos?camera_id=<id>&limit=50` | admin or read-only UI session | 全体一覧。管理 UI 用 |

MVP では既存 UI と同じプロセス内の管理画面から使う前提とし、公開 API としての read-only token は後続検討にする。

### 4.3 動画配信

| Endpoint | Auth | 説明 |
|---|---|---|
| `GET /api/cameras/<camera_id>/videos/<video_id>/content` | admin or UI | `send_file` / Range request 対応で動画を返す |

動画タグでの再生を考えると、Range request (`Range: bytes=...`) に対応できる実装が望ましい。Flask の `send_file(..., conditional=True)` を使う場合でも、HTTP request からファイルパスを直接受け取らない。必ず `video_id` と `camera_id` で DB メタデータを引き、storage layer が管理する `relative_path` だけを解決する。

`/videos/<path:relative_path>` のような相対パス直接指定のショートカットは MVP では作らない。path traversal、カメラ境界の迂回、DB メタデータを通らない配信を避けるため、配信 API は `video_id` ベースに統一する。

---

## 5. 保存方式

### 5.1 ファイル名

```text
video_<captured_at_utc_compact>_<uuid>.<ext>
```

例:

```text
video_20260601T120000000000_0123456789abcdef.mp4
```

- `captured_at` が不正または未指定の場合はサーバー受信時刻を使う。
- 拡張子は content sniffing と許可リストの両方で決める。
- ユーザー入力由来のファイル名は保存名に使わない。
- 受信中は一時ファイルへ書き込み、サイズ・形式・ハッシュ検証後にサーバー生成名へ atomic rename する。
- 検証失敗、上限超過、DB 登録失敗時は一時ファイルを削除する。

### 5.2 許可形式

MVP の許可リスト:

| 拡張子 | MIME type | 備考 |
|---|---|---|
| `.mp4` | `video/mp4` | 第一候補 |
| `.mjpeg` | `multipart/x-mixed-replace` または `video/x-motion-jpeg` | 実機制約がある場合のみ後続で検討 |

初期実装は `.mp4` のみでよい。MJPEG はブラウザ互換性・メタデータ・ファイルサイズが扱いづらいため、実機検証で必要になってから追加する。

クライアントが送る `Content-Type` やファイル拡張子だけを信頼しない。少なくとも MP4 の `ftyp` box などのマジックバイト/コンテナ識別を確認し、許可リスト外の形式は保存前に拒否する。

### 5.3 サイドカーメタデータ

```json
{
  "camera_id": "esp32cam-01",
  "captured_at": "2026-06-01T12:00:00+00:00",
  "stored_at": "2026-06-01T12:00:02+00:00",
  "duration_ms": 5000,
  "content_type": "video/mp4",
  "size": 524288,
  "sha256": "..."
}
```

サイドカーは DB 復旧やローカルファイル確認に有用なため、画像と同様に残す。ただし authoritative source は SQLite とする。

---

## 6. データモデル

新規テーブル `videos` を追加する。

```sql
videos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    filename TEXT NOT NULL UNIQUE,
    relative_path TEXT NOT NULL UNIQUE,
    size INTEGER NOT NULL,
    camera_id TEXT NOT NULL,
    captured_at TEXT,
    stored_at TEXT NOT NULL,
    duration_ms INTEGER,
    content_type TEXT NOT NULL,
    sha256 TEXT,
    processing_status TEXT NOT NULL DEFAULT 'skipped',
    processing_error TEXT,
    FOREIGN KEY(camera_id) REFERENCES cameras(camera_id) ON DELETE CASCADE
)
```

インデックス:

```sql
CREATE INDEX idx_videos_camera_stored_at ON videos(camera_id, stored_at DESC);
CREATE INDEX idx_videos_stored_at ON videos(stored_at DESC);
```

`processing_status` は将来のサムネイル生成・動画解析に備えたフィールドで、MVP では `skipped` 固定でよい。

---

## 7. サイズ上限・タイムアウト・レート制限

### 7.1 環境変数

| 変数 | デフォルト | 説明 |
|---|---:|---|
| `ESP32CAM_MAX_VIDEO_CONTENT_LENGTH` | `10485760` (10 MB) | 1 動画あたりの最大サイズ |
| `ESP32CAM_VIDEO_UPLOAD_RATE_LIMIT` | `6 per minute` | カメラ単位の動画 upload 制限 |
| `ESP32CAM_VIDEO_ALLOWED_EXTENSIONS` | `mp4` | 許可拡張子 |
| `ESP32CAM_VIDEO_RETENTION_MAX_AGE_SECONDS` | `2592000` (30 days) | 動画保持期間 |
| `ESP32CAM_VIDEO_RETENTION_MAX_BYTES` | `0` (無効) | 動画総容量上限 |

画像用の `ESP32CAM_MAX_CONTENT_LENGTH` は 1MB であり、動画には小さすぎるため動画専用の上限を分ける。

### 7.2 受信時の防御

- `Content-Length` が上限超過なら読み込み前に `413 Payload Too Large` を返す。
- `Content-Length` がない場合もストリーミング読み込み中に上限を超えた時点で中断する。
- 空ボディは `400 Bad Request`。
- 許可外 MIME / 拡張子は `415 Unsupported Media Type`。
- `camera_id` が無効形式なら `400 Bad Request`。
- `camera_id` に `/`、`..`、空白、制御文字、許可リスト外文字が含まれる場合は `400 Bad Request`。
- `X-API-Key` がない場合は `401 Unauthorized`。
- `camera_id` に対応する key が未設定、または key が一致しない場合は `403 Forbidden`。
- ディスク容量不足は `507 Insufficient Storage`。

---

## 8. 実装分割計画

### PR 1: 設計ドキュメント

- `docs/design/video-upload.md` を追加する。
- `docs/ja/roadmap.md` からリンクする。
- 静的テストで主要セクションと endpoint 草案を確認する。

### PR 2: ストレージとメタデータ層

- `script/storage.py` に `save_video(...)` / `list_videos(...)` 相当を追加する。
- `script/metadata.py` に `videos` テーブルと CRUD を追加する。
- `tests/test_storage.py` / `tests/test_metadata.py` に動画保存・一覧・削除連動のテストを追加する。

### PR 3: アップロード API

- `POST /api/cameras/<camera_id>/videos` を追加する。
- device API key、サイズ上限、MIME validation、エラー応答を実装する。
- `tests/test_routes.py` に正常系・認証・サイズ超過・形式不正を追加する。
- OpenAPI を更新する。

### PR 4: 一覧・配信 API と UI

- `GET /api/cameras/<camera_id>/videos` を追加する。
- `GET /api/cameras/<camera_id>/videos/<video_id>/content` を追加する。
- カメラ詳細 UI に最新動画一覧を追加する。
- Range request / path traversal regression test を追加する。

### PR 5: 保持ポリシーと運用ドキュメント

- 動画向け retention を画像と同じ retention framework に統合する。
- `docs/ja/operations-runbook.md` と `docs/deployment.md` に動画用環境変数を追加する。
- 容量上限に達したときの古い動画削除をテストする。

---

## 9. CI 影響

| CI / 検証 | 方針 |
|---|---|
| `ruff check .` | 新規 Python 実装 PR で必須 |
| `pytest -q` | 既存画像 upload / WebSocket upload の regression を含めて実行 |
| coverage | `script` package の coverage 80% 以上を維持 |
| firmware build | サーバー API のみの PR では不要。ファームウェアが動画送信を実装する PR で PlatformIO build を追加 |
| `act` | route/storage 実装 PR では `act pull_request -j test --container-architecture linux/amd64` を可能なら実行 |

動画アップロードはファイルサイズが大きくなりやすいため、テストでは `tmp_path` と小さなダミーバイナリを使い、実リポジトリ配下に大きな fixture を置かない。

---

## 10. 未解決事項

- ESP32-CAM 側で MP4 を生成できるか、MJPEG / JPEG 連番方式にするか。
- ブラウザ UI でサムネイルを生成するか、最初は動画タグだけにするか。
- 動画を人物検知や猫検知の入力に使うか、スナップショット検知とは分離するか。
- Raspberry Pi Zero を対象に含める場合の現実的な動画サイズ・頻度。
- 長期保存を Google Drive / NAS / S3 に逃がすタイミング。

MVP 実装前に、まずは Mac ローカル環境で 1〜3 秒の小さな MP4 ダミー upload を通し、サーバー側の保存・一覧・配信だけを確定させる。
