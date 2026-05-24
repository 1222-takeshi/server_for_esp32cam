# ESP32-CAM 遠隔制御API設計

- 関連 Issue: #137
- 関連文書: [`docs/ja/business-scenarios.md`](../ja/business-scenarios.md), [`docs/ja/security.md`](../ja/security.md), [`docs/adr/006-device-polling-remote-control.md`](../adr/006-device-polling-remote-control.md)

## 1. 目的

ペット監視MVPでは、ESP32-CAMを現地でUSB接続して再書き込みしなくても、LAN内のサーバー画面またはAPIから撮影条件を調整できることを優先する。

最初の遠隔制御は、リアルタイムな即時命令ではなく、**サーバーに desired config を保存し、ESP32-CAM が起床時または周期処理時に取得して反映する方式**とする。

## 2. スコープ

### 2.1 MVPで扱う設定

| 項目 | キー | 型 | 範囲・候補 | 備考 |
|------|-----|----|-----------|------|
| 撮影間隔 | `capture_interval_seconds` | integer | 60〜86400 | deep sleepのsleep時間に利用する |
| 解像度 | `frame_size` | string | `QVGA`, `VGA`, `SVGA`, `XGA`, `SXGA`, `UXGA` | ESP32-CAM/OV2640で実装可能な値に限定する |
| JPEG品質 | `jpeg_quality` | integer | 10〜63 | 小さいほど高品質。帯域・保存容量とのトレードオフ |
| 垂直反転 | `vflip` | boolean | `true` / `false` | 設置向き補正用 |
| 水平反転 | `hmirror` | boolean | `true` / `false` | 設置向き補正用 |
| 再起動要求 | `restart_requested` | boolean | `true` / `false` | デバイスが受理後にサーバーへackする |

### 2.2 MVPで扱わないこと

- インターネット越しの即時制御
- 多人数利用・詳細な権限管理
- モーターやサーボなど可動部の制御
- 常時WebSocket接続を前提にした低遅延コマンド配信
- 任意コマンド実行、任意URLアクセス、任意ファームウェア更新

## 3. 制約

- ESP32-CAMはdeep sleep運用を前提にするため、睡眠中はサーバーから直接到達できない。
- MVPは自宅LAN内・月額0円・Raspberry Pi / Raspberry Pi Zeroでの常時稼働を優先する。
- 認証は既存方針に合わせ、デバイス向けは `X-API-Key: <DEVICE_API_KEY>`、管理者向けは `Authorization: Bearer <ADMIN_TOKEN>` を使う。
- 設定値はデバイスが安全に扱えるホワイトリスト制にする。自由形式のコマンド文字列は受け付けない。

## 4. 方式比較

| 方式 | 概要 | メリット | デメリット | 判定 |
|------|------|----------|------------|------|
| Device polling | デバイスが起床時にサーバーから設定を取得する | deep sleepと相性がよい。HTTP実装が単純。NAT/LAN構成に依存しにくい | 反映は次回起床時まで遅れる | 採用 |
| Server push / WebSocket | サーバーから接続中デバイスへ命令を送る | 接続中なら低遅延 | deep sleep中は届かない。接続維持・再接続・上限管理が必要 | MVPでは不採用 |
| Device HTTP server | ESP32-CAM側にHTTPサーバーを立てる | ブラウザ等から直接操作できる | IP探索、認証、到達性、sleepとの相性が悪い | MVPでは不採用 |
| MQTT | broker経由でdesired stateを配信する | IoTでは一般的。QoSやretainが使える | broker運用が増え、現行Flask構成より複雑 | 将来候補 |

## 5. API案

### 5.1 管理者: desired config取得

```http
GET /api/cameras/{camera_id}/desired-config
Authorization: Bearer <ADMIN_TOKEN>
```

レスポンス例:

```json
{
  "camera_id": "living-room-cam",
  "version": 3,
  "updated_at": "2026-05-24T12:00:00Z",
  "updated_by": "admin",
  "config": {
    "capture_interval_seconds": 300,
    "frame_size": "SVGA",
    "jpeg_quality": 12,
    "vflip": false,
    "hmirror": false,
    "restart_requested": false
  },
  "last_acknowledged_version": 2,
  "last_acknowledged_at": "2026-05-24T11:30:00Z"
}
```

### 5.2 管理者: desired config更新

```http
PUT /api/cameras/{camera_id}/desired-config
Authorization: Bearer <ADMIN_TOKEN>
Content-Type: application/json
```

リクエスト例:

```json
{
  "capture_interval_seconds": 300,
  "frame_size": "SVGA",
  "jpeg_quality": 12,
  "vflip": false,
  "hmirror": false,
  "restart_requested": false
}
```

成功時は `200 OK` で更新後のconfigを返す。入力不正時は `400 Bad Request`、認証失敗時は `401 Unauthorized`、存在しない/不正な `camera_id` は `404 Not Found` または `400 Bad Request` とする。

### 5.3 デバイス: desired config取得

```http
GET /api/devices/{device_id}/desired-config?current_version=2
X-API-Key: <DEVICE_API_KEY>
```

レスポンス例:

```json
{
  "device_id": "living-room-cam",
  "version": 3,
  "changed": true,
  "config": {
    "capture_interval_seconds": 300,
    "frame_size": "SVGA",
    "jpeg_quality": 12,
    "vflip": false,
    "hmirror": false,
    "restart_requested": false
  }
}
```

`current_version` が最新と一致する場合でも、初期実装では `changed: false` と現在値を返してよい。帯域削減が必要になったら `304 Not Modified` を検討する。

### 5.4 デバイス: config反映結果のack

```http
POST /api/devices/{device_id}/desired-config/ack
X-API-Key: <DEVICE_API_KEY>
Content-Type: application/json
```

リクエスト例:

```json
{
  "version": 3,
  "applied": true,
  "message": "applied",
  "firmware_version": "0.3.0",
  "free_heap": 182000
}
```

エラー例:

```json
{
  "version": 3,
  "applied": false,
  "message": "unsupported frame_size: UXGA",
  "firmware_version": "0.3.0",
  "free_heap": 178000
}
```

## 6. データモデル案

SQLite metadata store に次のテーブルを追加する。

```sql
CREATE TABLE camera_desired_configs (
    camera_id TEXT PRIMARY KEY,
    version INTEGER NOT NULL DEFAULT 1,
    config_json TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    updated_by TEXT NOT NULL DEFAULT 'admin',
    last_acknowledged_version INTEGER,
    last_acknowledged_at TEXT,
    last_acknowledged_message TEXT,
    last_acknowledged_success INTEGER
);
```

初期configは、サーバー側のデフォルト値から生成する。config JSONにはホワイトリスト済みのキーのみ保存する。

## 7. ESP32-CAM側の処理フロー

1. 起床する。
2. Wi-Fiへ接続する。
3. `GET /api/devices/{device_id}/desired-config?current_version=<n>` を呼ぶ。
4. 取得した `version` がローカル保存値より新しければ、範囲チェック後に設定を反映する。
5. `POST /api/devices/{device_id}/desired-config/ack` で反映結果を送る。
6. 反映後の設定で撮影・アップロードする。
7. `capture_interval_seconds` に従ってdeep sleepへ戻る。

## 8. セキュリティ・安全性

- 管理者APIとデバイスAPIの認証を分離する。
- 設定値はサーバー側とファームウェア側の両方で範囲チェックする。
- `restart_requested` はack後にサーバー側で自動的に `false` に戻すか、`restart_request_id` 方式へ拡張する。初期実装では二重再起動を避けるため、ack成功時のクリアを推奨する。
- 任意コマンド実行・任意パス保存・任意URL取得は禁止する。
- ログにはAPIキー、Bearer token、Wi-Fiパスワードを出力しない。
- 将来モーター制御へ進む場合は、別Issueで可動範囲、速度制限、非常停止、物理的ストッパーを要件化する。

## 9. 実装ステップ案

1. OpenAPIに `desired-config` 系エンドポイントを追加する。
2. SQLite storeに desired config のCRUDとack更新を追加する。
3. Flask routesに管理者/デバイスAPIを追加する。
4. pytestで認証、入力検証、version更新、ack記録を確認する。
5. ESP32-CAMサンプル側に polling とackの最小実装を追加する。
6. README/日本語docsに運用手順を追加する。

## 10. 受け入れ基準

- 管理者がカメラ単位で desired config を取得・更新できる。
- デバイスが起床時に desired config を取得できる。
- デバイスが適用結果をackでき、管理者APIで最終ack状態を確認できる。
- 不正な設定値は `400 Bad Request` で拒否される。
- 認証なしでは管理者API・デバイスAPIとも利用できない。
- 既存の画像アップロード、heartbeat、ログ収集、WebSocket uploadのテストが退行しない。
