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
| 撮影間隔 | `capture_interval_seconds` | integer | 5〜86400 | WebSocket連続送信ではフレーム間隔。deep sleep構成ではsleep時間に利用する |
| 解像度 | `frame_size` | string | `QVGA`, `VGA`, `SVGA`, `XGA`, `SXGA`, `UXGA` | ESP32-CAM/OV2640で実装可能な値に限定する |
| JPEG品質 | `jpeg_quality` | integer | 10〜63 | 小さいほど高品質。帯域・保存容量とのトレードオフ |
| 垂直反転 | `vflip` | boolean | `true` / `false` | 設置向き補正用 |
| 水平反転 | `hmirror` | boolean | `true` / `false` | 設置向き補正用 |
| 再起動要求 | `restart_requested` | boolean | `true` / `false` | デバイスが受理後にサーバーへackする |
| OTA待受要求 | `ota_update_requested` | boolean | `true` / `false` | 起床時にOTA待受ウィンドウへ入る |
| OTA待受時間 | `ota_window_seconds` | integer | 30〜600 | ArduinoOTA等の更新を受け付ける最大時間 |

### 2.2 MVPで扱わないこと

- インターネット越しの即時制御
- 多人数利用・詳細な権限管理
- モーターやサーボなど可動部の制御
- 常時WebSocket接続を前提にした低遅延コマンド配信
- 任意コマンド実行、任意URLアクセス、任意URL/未検証バイナリからのファームウェア更新

## 3. 制約

- ESP32-CAMはdeep sleep運用を前提にするため、睡眠中はサーバーから直接到達できない。
- MVPは自宅LAN内・月額0円・Raspberry Pi / Raspberry Pi Zeroでの常時稼働を優先する。
- 認証は既存方針に合わせ、デバイス向けは `X-API-Key: <DEVICE_API_KEY>`、管理者向けは `Authorization: Bearer <ADMIN_TOKEN>` を使う。
- デバイス向けAPIでは、`device_id` と `X-API-Key` の対応関係をサーバー側で検証し、別デバイスの desired config 取得・ack を拒否する。
- OTA待受を有効化する場合は、API認証とは別に ArduinoOTA / PlatformIO OTA upload 側でも非空の `OTA_PASSWORD` を必須にする。パスワードなしOTAはMVPの遠隔更新フローでは禁止する。
- 設定値はデバイスが安全に扱えるホワイトリスト制にする。自由形式のコマンド文字列は受け付けない。
- desired config の既定は撮影間隔300秒・解像度SVGA。既存のWebSocketファームウェアは5秒・QVGAで送信するため、その条件を維持して更新するときはOTA前に管理者APIで明示的に `capture_interval_seconds=5` と `frame_size=QVGA` を保存する。デバイスは起動時と接続中の周期処理で設定を取得する。
- HTTP `POST /upload` には既定で10回/分のレート制限がある。5秒間隔の連続撮影にはWebSocket uploadを使うか、管理者がHTTP側の制限を別途調整する。

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
    "restart_requested": false,
    "ota_update_requested": false,
    "ota_window_seconds": 120
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
  "expected_version": 2,
  "config": {
    "capture_interval_seconds": 300,
    "frame_size": "SVGA",
    "jpeg_quality": 12,
    "vflip": false,
    "hmirror": false,
    "restart_requested": false,
    "ota_update_requested": false,
    "ota_window_seconds": 120
  }
}
```

初期実装では直接configオブジェクトをPUTしてもよいが、競合更新を避けるため、管理UI/APIでは `expected_version` を添えた条件付き更新を推奨する。成功時は `200 OK` で更新後のconfigを返す。入力不正時は `400 Bad Request`、認証失敗時は `401 Unauthorized`、存在しない/不正な `camera_id` は `404 Not Found` または `400 Bad Request`、`expected_version` 不一致時は `409 Conflict` とする。

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
    "restart_requested": false,
    "ota_update_requested": true,
    "ota_window_seconds": 120
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
5. `ota_update_requested` が `true` の場合、`ota_window_seconds` の間だけ Wi-Fi 接続と OTA サービスを維持し、ArduinoOTA などのOTA更新を受け付ける。
6. OTA待受に入る直前に `POST /api/devices/{device_id}/desired-config/ack` で `message: "ota_ready"` を送る。OTA成功時は再起動後の通常フローで新ファームウェア情報をackし、OTA認証失敗・転送失敗・timeout時は `failed` / `timeout` をackする。
7. OTA待受を使わない通常設定変更では、範囲チェックと反映結果をackする。
8. 反映後の設定で撮影・アップロードする。
9. `capture_interval_seconds` に従ってdeep sleepへ戻る。

### 7.1 OTA更新の扱い

deep sleep中のESP32-CAMはWi-FiもCPUも停止しているため、PlatformIO/ArduinoOTAから直接接続してOTAを書き込むことはできない。OTA更新は、デバイスが起床してWi-Fi接続し、`ArduinoOTA.handle()` などを処理している時間帯だけ成立する。

MVPでは「任意URLからファームウェアを取得して自己更新する」方式ではなく、既存のArduinoOTA/PlatformIO OTA uploadを前提に、desired configで **次回起床時にOTA待受ウィンドウを開く** 方式とする。

想定フロー:

1. 管理者が `ota_update_requested=true` と `ota_window_seconds` を設定する。
2. ESP32-CAMが次回起床時に desired config を取得する。
3. デバイスは `OTA ready` 状態をシリアルログまたはackで通知し、指定秒数だけ `ArduinoOTA.handle()` を処理する。
4. 運用者は同じLAN内から、デバイスに設定済みの非空 `OTA_PASSWORD` を使って `pio run -e esp32cam_http_upload_ota -t upload` を実行する。
5. OTA成功後にデバイスは再起動し、新ファームウェアで通常フローへ戻る。
6. OTA認証失敗・転送失敗時は `failed`、待受がtimeoutした場合は `timeout` をackに記録し、通常撮影・アップロード・deep sleepへ戻る。

`ota_update_requested` は一回限りの操作として扱い、OTA成功・OTA失敗・OTA待受timeout後にサーバー側で `false` へ戻す。二重更新や意図しない長時間起床を避けるため、`ota_window_seconds` には上限を設ける。

## 8. セキュリティ・安全性

- 管理者APIとデバイスAPIの認証を分離する。
- デバイスAPIは `device_id` とAPIキーの対応を検証し、APIキーを知る別デバイスによるなりすまし取得・ackを拒否する。
- 設定値はサーバー側とファームウェア側の両方で範囲チェックする。
- `restart_requested` はack後にサーバー側で自動的に `false` に戻すか、`restart_request_id` 方式へ拡張する。初期実装では二重再起動を避けるため、ack成功時のクリアを推奨する。
- OTA更新は起床中の明示的な待受ウィンドウに限定し、任意URL・未検証バイナリ・署名なし自動更新は扱わない。
- OTA待受を開くデバイスは非空の `OTA_PASSWORD` を必須にし、パスワードなしのArduinoOTA待受は失敗として扱う。管理者APIのBearer tokenだけではOTA経路の認証にならないため、OTA upload時にも別途パスワードを要求する。
- 任意コマンド実行・任意パス保存・任意URL取得は禁止する。
- ログにはAPIキー、Bearer token、Wi-Fiパスワード、OTAパスワードを出力しない。
- 将来モーター制御へ進む場合は、別Issueで可動範囲、速度制限、非常停止、物理的ストッパーを要件化する。

## 9. 実装ステップ案

1. OpenAPIに `desired-config` 系エンドポイントを追加する。
2. SQLite storeに desired config のCRUDとack更新を追加する。
3. Flask routesに管理者/デバイスAPIを追加する。
4. pytestで認証、入力検証、version更新、ack記録を確認する。
5. デバイスAPIで `device_id` とAPIキーの対応検証を実装・テストする。
6. ESP32-CAMサンプル側に polling とackの最小実装を追加する。
7. ESP32-CAMサンプル側に起床時OTA待受ウィンドウを追加し、非空 `OTA_PASSWORD` 必須の既存ArduinoOTA/PlatformIO OTA upload手順と接続する。
8. README/日本語docsに運用手順を追加する。

## 10. 受け入れ基準

- 管理者がカメラ単位で desired config を取得・更新できる。
- デバイスが起床時に desired config を取得できる。
- デバイスAPIは `device_id` とAPIキーの対応が一致しない要求を拒否できる。
- デバイスが適用結果をackでき、管理者APIで最終ack状態を確認できる。
- 不正な設定値は `400 Bad Request` で拒否される。
- `ota_update_requested=true` の場合、デバイスが次回起床時に指定秒数だけOTA更新を受け付け、timeout後は通常運用へ戻る。
- OTA待受は非空 `OTA_PASSWORD` が設定されている場合のみ有効化され、パスワードなしOTAは失敗として扱われる。
- OTA認証失敗・転送失敗・timeout時は状態がackまたは管理者APIから確認でき、`ota_update_requested` が解除される。
- 認証なしでは管理者API・デバイスAPIとも利用できない。
- 既存の画像アップロード、heartbeat、ログ収集、WebSocket uploadのテストが退行しない。
