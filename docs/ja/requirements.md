# 要件

## ハードウェア要件

このサーバーアプリケーションは、標準的なコンピューターまたはサーバー環境 (Windows, Linux, macOS) で動作します。

現在のマイルストーンを意図通りに利用するためには、通常以下のものが必要です:

*   **画像キャプチャデバイス:**
    *   **ESP32-CAM:** 主対象です。JPEG 画像をキャプチャし、HTTP POST を使ってこのサーバーの `/upload` エンドポイントへ送信するようにプログラムします。
    *   **JPEG を HTTP で送れる他のデバイス:** テストには使えますが、主対象ではありません。
*   **ネットワーク接続:** ESP32-CAM とサーバーが同一ネットワーク上にあるか、サーバーがデバイスから到達可能である必要があります。
*   **ストレージ容量:** JPEG スナップショットを保存するための十分なローカルディスク容量が必要です。

## ソフトウェア要件

### サーバーサイド:

*   **Python:** Python 3.10 以上。
*   **Flask:** サーバーで使用する Web フレームワーク。
*   **pytest:** 自動テストに使用します。
*   **オペレーティングシステム:** Linux, macOS, Windows。

### クライアントサイド (例: ESP32-CAM):

*   **ESP32-CAMの場合:**
    *   **Arduino IDE** または **PlatformIO IDE**
    *   **ESP32 ボードサポートパッケージ:** Arduino IDEまたはPlatformIOにインストールする必要があります。
    *   **関連ライブラリ:** カメラ制御、Wi-Fi、JPEG upload 用の HTTP クライアント機能が必要です。

### オプション (将来フェーズ):

*   **NAS アクセス用ライブラリまたは mount 設定:** ネットワークストレージを保存先に使う場合。
*   **Google Drive API 用ライブラリ:** cloud-backed storage を追加する場合。
*   **OpenCV / ML フレームワーク:** snapshot upload と storage path が安定した後に検討します。

---

## 非機能要件

### スケール上限

| 項目 | ローカル開発 | Raspberry Pi 運用 | 将来の本番運用 |
|------|------------|------------------|--------------|
| 同時接続カメラ台数 | 制限なし (テスト用) | 最大 10 台 | 50 台以上 (要 Redis) |
| アップロード間隔 (推奨) | 任意 | 5〜300 秒 | 1〜300 秒 |
| 最大 JPEG サイズ | `ESP32CAM_MAX_CONTENT_LENGTH` (デフォルト 1 MiB) | 1 MiB | 設定変更可 |
| 最大画像寸法 | `ESP32CAM_UPLOAD_MAX_IMAGE_DIMENSION` (デフォルト 4096 px) | 4096 px | 設定変更可 |
| 同時 WebSocket 接続数 | 制限なし (テスト用) | `ESP32CAM_MAX_WS_CONNECTIONS` (デフォルト 50) | 50+ |
| ストレージ上限 | 任意 | `ESP32CAM_STORAGE_RETENTION_MAX_BYTES` で設定 | 要外部ボリューム |
| 保存期間 | 任意 | `ESP32CAM_STORAGE_RETENTION_MAX_AGE_SECONDS` で設定 | 30 日〜 (設定による) |

### 可用性・復旧目標

| 項目 | ローカル開発 | Raspberry Pi 運用 |
|------|------------|------------------|
| 目標稼働率 | — | 99% (月間ダウンタイム ≦ 7 時間) |
| RTO (復旧時間目標) | — | 15 分以内 |
| RPO (データ損失許容) | — | 最大 1 日分 (日次バックアップ前提) |
| ヘルスチェック | `GET /healthz` / `GET /healthz/ready` | 同左 |

サーバーはシングルプロセスで動作します。高可用性が必要な場合は、Docker Compose の `restart: always` またはシステムの systemd ユニットを利用してください。

### 認証要件

| 項目 | ローカル開発 | Raspberry Pi 運用 (LAN 内) | インターネット公開時 |
|------|------------|--------------------------|------------------|
| デバイス API キー (`X-API-Key`) | 省略可 | **必須** | **必須** |
| 管理者トークン (Bearer) | 省略可 | **必須** | **必須** |
| HTTPS | 不要 | 不要 | **必須** (nginx / Caddy 等で終端) |
| `ESP32CAM_DEVICE_API_KEY` | 未設定 = 認証無効 | 32 バイト以上のランダム値 | 同左 |
| `ESP32CAM_ADMIN_TOKEN` | 未設定 = 認証無効 | 32 バイト以上のランダム値 | 同左 |

詳細: [`docs/ja/security.md`](security.md)（脅威モデルと運用チェックリスト）

### パフォーマンス

| 項目 | 目標値 |
|------|--------|
| HTTP アップロード応答時間 (P99) | 500 ms 以内 (推論無効時) |
| WebSocket フレーム処理遅延 | 200 ms 以内 |
| `GET /api/cameras` 応答時間 | 200 ms 以内 (カメラ 10 台) |
| ディスク書き込み失敗時の挙動 | HTTP 507 または WebSocket エラーレスポンスを返し、処理を中断する |

### アップロードレート制限

| エンドポイント | デフォルト制限 | 設定変数 |
|-------------|-------------|---------|
| `POST /upload` (HTTP) | 10 回/分 (デバイス API キー単位) | `ESP32CAM_UPLOAD_RATE_LIMIT` |
| WebSocket フレーム | 1 秒に 1 フレーム/接続 | `ESP32CAM_UPLOAD_WS_MIN_FRAME_INTERVAL_SECONDS` |

---

## 受け入れ基準

以下の基準を満たすことで「利用可能」と判断します。

### ヘルスチェック

- `GET /healthz` が `{"status": "ok"}` を返す
- `GET /healthz/ready` が DB・ストレージ・推論ワーカーの状態を含む JSON を返す
- DB または ストレージが異常な場合、`ready` が `false` になる

### HTTP アップロード API (`POST /upload`)

- 有効な JPEG を受け付け、ファイルをディスクに保存する
- 不正な JPEG (壊れたヘッダ、非画像データ) を 400 で拒否する
- 画像寸法が上限を超えた場合を 400 で拒否する
- ペイロードが `MAX_CONTENT_LENGTH` を超えた場合を 413 で拒否する
- `X-API-Key` が設定済みの場合、認証なしのリクエストを 401 で拒否する
- レート制限超過を 429 で拒否する

### WebSocket アップロード

- バイナリフレームとして JPEG を受け付け、保存する
- フレーム間隔がしきい値未満の場合、`snapshot.error` でエラーを返す
- 接続数が上限に達した場合、新規接続を拒否する

### カメラ管理 API

- `GET /api/cameras` がカメラ一覧と直近アップロード時刻を返す
- `PATCH /api/cameras/<id>` が `display_name` / `expected_interval_seconds` を更新する
- `DELETE /api/cameras/<id>` がカメラとスナップショットを削除する
- 管理者トークンが設定済みの場合、認証なしのリクエストを 401 で拒否する

### 人物検知 (オプション)

- `ESP32CAM_DETECTOR=tflite_cpu` 設定時、アップロードごとに推論が実行される
- 検知結果が DB に保存される
- 推論エラーがアップロード応答を妨げない (非同期モード)

### Webhook 通知 (オプション)

- `ESP32CAM_WEBHOOK_URL` 設定時、検知スコアが閾値を超えたら POST する
- Webhook 送信失敗がアップロード処理を中断しない

### バックアップ・リストア

- SQLite DB と画像ファイルをコピーしてバックアップを取得できる
- バックアップから復元した後、`/healthz/ready` が `ready: true` を返す
- 詳細手順: [`docs/ja/operations-runbook.md`](operations-runbook.md)

---

## 関連ドキュメント

| ドキュメント | 内容 |
|------------|------|
| [`docs/ja/setup.md`](setup.md) | 開発環境セットアップ |
| [`docs/ja/running.md`](running.md) | サーバー起動方法 |
| [`docs/ja/usage.md`](usage.md) | API 利用方法 |
| [`docs/ja/troubleshooting.md`](troubleshooting.md) | トラブルシューティング |
| [`docs/ja/security.md`](security.md) | 脅威モデルと運用チェックリスト |
| [`docs/ja/operations-runbook.md`](operations-runbook.md) | 運用 Runbook |
| [`docs/ja/roadmap.md`](roadmap.md) | 将来の開発計画 |
| [`docs/deployment.md`](../deployment.md) | Docker デプロイガイド |
| [`docs/openapi.yaml`](../openapi.yaml) | OpenAPI 3.1 仕様 |
