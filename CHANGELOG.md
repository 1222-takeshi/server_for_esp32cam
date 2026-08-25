# Changelog

バージョンおよびリリースの変更履歴です。
[Keep a Changelog](https://keepachangelog.com/ja/1.1.0/) 形式に準拠します。

---

## バージョニングルール

- **セマンティックバージョニング** (`MAJOR.MINOR.PATCH`) を採用します
  - `MAJOR`: 後方互換性のない API 変更
  - `MINOR`: 後方互換性のある機能追加
  - `PATCH`: 後方互換性のあるバグ修正
- Git タグ: `v0.1.0` 形式
- PR ラベル: `breaking`, `feat`, `fix`, `docs`, `chore`, `security`

---

## [Unreleased]

### Fixed
- `/static/*`（vendored swagger-ui JS/CSS、`base.css` など）が `send_file` の `direct_passthrough` によりgzip圧縮をスキップしていた問題を修正。Range リクエストは従来通り無圧縮のまま扱う

### Added
- OpenAPI 3.1 仕様 (`docs/openapi.yaml`) と Swagger UI (`GET /api/docs`)
- 脅威モデルとセキュリティ運用チェックリスト (`docs/ja/security.md`)
- 運用 Runbook (`docs/ja/operations-runbook.md`)
- ADR ディレクトリ (`docs/adr/`) と初期 ADR 5 件
- 非機能要件と受け入れ基準 (`docs/ja/requirements.md`)
- 導入シナリオと MVP KPI (`docs/ja/roadmap.md`)

---

## [0.1.0] - 2024-09-01

### Added
- API キー + Bearer トークンによる認証 (`ESP32CAM_DEVICE_API_KEY`, `ESP32CAM_ADMIN_TOKEN`)
- WebSocket アップロードの接続数制限 (`ESP32CAM_MAX_WS_CONNECTIONS`)
- WebSocket フレームレート制限 (`ESP32CAM_UPLOAD_WS_MIN_FRAME_INTERVAL_SECONDS`)
- JPEG バリデーション (Pillow による寸法・形式チェック)
- HTTP アップロードのレート制限 (Flask-Limiter)
- ストレージ保存期間・容量上限による自動削除
- カメラ削除 API (`DELETE /api/cameras/<id>?delete_files=true`)
- カメラ設定 PATCH API (`PATCH /api/cameras/<id>`)
- デバイスログ API (`POST /api/log_error`, `GET /api/cameras/<id>/logs`)
- Prometheus メトリクス (`GET /metrics`)
- Docker Compose デプロイ構成
- `GET /healthz` / `GET /healthz/ready` ヘルスチェック

### Fixed
- `device_id` 入力バリデーション強化（長さ上限・文字種チェック）
- エラーレスポンスから内部スタックトレースを除去

---

## [0.0.1] - 2024-01-01

### Added
- `POST /upload` による JPEG スナップショット HTTP アップロード
- `GET /files/<filename>` によるスナップショット閲覧
- WebSocket アップロード (`/upload/ws`)
- SQLite によるスナップショットメタデータ管理
- マルチカメラ対応 (`device_id` によるカメラ管理)
- TFLite による人物検知 (オプション)
- Webhook 通知 (人物検知時)
- ブラウザダッシュボード

[Unreleased]: https://github.com/1222-takeshi/server_for_esp32cam/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/1222-takeshi/server_for_esp32cam/compare/v0.0.1...v0.1.0
[0.0.1]: https://github.com/1222-takeshi/server_for_esp32cam/releases/tag/v0.0.1
