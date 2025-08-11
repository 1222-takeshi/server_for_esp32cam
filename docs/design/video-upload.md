## 動画アップロード API / 保存方式設計

### 目的
- ESP32-CAM から送信される動画（クリップ）を受け付け、保存・一覧・配信できるようにする。

### 保存レイアウト
- 動画: `uploads/<camera_id>/videos/video_<unix_ts>.<ext>`（例: mp4）
- 将来: サムネイル `uploads/<camera_id>/videos/thumbs/video_<unix_ts>.jpg`

### API 追加（案）
- `POST /upload_video`
  - 形式: `multipart/form-data`、フィールド `video`、任意 `camera_id`
  - 許可拡張子（例）: mp4,mov（環境変数 `ALLOWED_VIDEO_EXTS`）
  - サイズ上限: 例 50MB（環境変数 `MAX_VIDEO_LENGTH_MB`）
  - 応答: `{ message, filename, camera_id }`
- `GET /videos`
  - クエリ: `?camera_id=<id>`、一覧表示（ページネーション検討）
- `GET /videos/<filename>`
  - クエリ: `?camera_id=<id>`、単一配信

### セキュリティ/堅牢性
- `secure_filename`、拡張子ホワイトリスト、サイズ上限制御。
- 大容量アップロードのタイムアウト/再送制御（将来: 分割/チャンクアップロード検討）。

### 段階的実装計画
1) サーバ側 API エンドポイントと保存処理（同期 I/O）
2) `/videos` 一覧ページとダウンロード配信
3) サムネイル生成（将来: ffmpeg 等の外部ツール連携）
4) テスト拡充（正常/異常/サイズ超過）

### テスト観点
- 正常アップロード、許可外拡張子、サイズ超過 413。
- `camera_id` ごとの保存分離（一覧/配信）。

### TODO（追跡用）
- [ ] 実装Issue作成とブランチ運用
- [ ] API 実装/テンプレート更新/テスト追加
- [ ] CI 確認（flake8/pytest）
- [ ] ドキュメント更新（usage/running）

