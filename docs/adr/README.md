# Architecture Decision Records (ADR)

このディレクトリには、プロジェクトの主要な設計判断を記録した ADR を格納します。

## ADR とは

ADR (Architecture Decision Record) は「なぜそう決めたか」を残す短い文書です。
コードやコメントでは伝わりにくい制約・トレードオフ・却下した代替案を記録します。

## ADR 一覧

| ID | タイトル | 状態 |
|----|---------|------|
| [ADR-001](001-http-upload-protocol.md) | HTTP アップロードプロトコルの採用 | 採用 |
| [ADR-002](002-sqlite-storage.md) | SQLite をメタデータストアとして採用 | 採用 |
| [ADR-003](003-local-filesystem-storage.md) | ローカルファイルシステムを画像ストレージとして採用 | 採用 |
| [ADR-004](004-websocket-upload.md) | WebSocket によるリアルタイムアップロード追加 | 採用 |
| [ADR-005](005-api-key-authentication.md) | API キー + Bearer トークンによる認証方式 | 採用 |
| [ADR-006](006-device-polling-remote-control.md) | Device polling による ESP32-CAM 遠隔制御 | 提案 |

## 新しい ADR を追加する方法

1. 次の番号を採番する（例: `006`）
2. `docs/adr/template.md` をコピーして `docs/adr/006-<短いタイトル>.md` を作成する
3. 内容を記入する
4. この README の一覧に追加する
5. PR でレビューを受ける

## ステータスの意味

| ステータス | 意味 |
|----------|------|
| 提案 | 検討中。まだ決定していない |
| 採用 | 決定・実装済み |
| 廃止 | かつて採用したが現在は別の方法に置き換え済み |
| 却下 | 検討したが採用しなかった |
