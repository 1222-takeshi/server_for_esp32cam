## 複数カメラ対応 保存レイアウト設計

### 目的
- 複数の ESP32-CAM クライアントからの画像を安全に識別・保存し、一覧/参照できるようにする。

### 保存レイアウト
- 画像: `uploads/<camera_id>/images/image_<unix_ts>.<ext>`
- 最新: `uploads/<camera_id>/images/latest.<ext>`
- 将来拡張: 日付パーティション `uploads/<camera_id>/images/<YYYY-MM-DD>/...`

### API 変更（案）
- `POST /upload`
  - 既存: `image` フィールド（multipart/form-data）
  - 追加: 任意 `camera_id`（未指定は `default`）
  - 応答: `{ message, filename, camera_id }`
- `GET /files`
  - 既存: 一覧表示
  - 追加: クエリ `?camera_id=<id>`（省略時 `default`）
- `GET /files/<filename>`
  - 既存: 単一配信
  - 追加: クエリ `?camera_id=<id>`（省略時 `default`）

### UI（案）
- `/files` に `camera_id` セレクタを追加（既存 `default` を初期値）。
- URL パラメータで選択状態を保持。

### バリデーション/セキュリティ
- `secure_filename` によるファイル名正規化。
- `camera_id` は英数・ダッシュ・アンダースコアのみ許可（サブディレクトリ侵入対策）。
- 既存の拡張子ホワイトリスト/サイズ上限を継承。

### 段階的実装計画
1) サーバ保存先を `<upload_root>/<camera_id>/images` に切り替え（デフォルト `default`）。
2) `/files` 一覧の `camera_id` 切替 UI 追加。
3) `/files/<filename>` の `camera_id` パラメータ対応。
4) テスト拡充（`camera_id` ごとの保存/一覧/取得）。

### テスト観点
- `camera_id` 未指定/指定での保存先分岐。
- 異常系: 不正な `camera_id`、ディレクトリ作成失敗時のハンドリング。
- UI: 空/ありの表示、選択保持。

### 影響と移行
- 既存 `uploads/` 直下のファイルは `uploads/default/images/` へ移行する簡易スクリプトを用意（任意）。
- CI は既存 pytest を拡張（ケース追加）。

### TODO（追跡用）
- [ ] 実装Issue作成とブランチ運用
- [ ] サーバ実装/テンプレート更新/テスト追加
- [ ] CI 確認（flake8/pytest）
- [ ] ドキュメント更新（usage/running）

