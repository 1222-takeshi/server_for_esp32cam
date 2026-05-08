# WebSocketリアルタイム配信設計

Issue: [#19 WebSocket導入によるリアルタイム映像配信の実装](https://github.com/1222-takeshi/server_for_esp32cam/issues/19)

## 1. 目的と前提

現在のサーバーは、ESP32-CAMが `POST /upload` に raw JPEG をアップロードし、サーバーが保存・メタデータ登録・推論キュー投入を行う **snapshot-first** 構成である。

WebSocket対応の目的は、ESP32-CAMから受け取った最新JPEGをブラウザへ低遅延で通知・配信し、ページリロードやポーリングなしで映像に近い表示を実現することである。

前提:

- ESP32-CAM側の既存アップロード契約は維持する。
  - `POST /upload`
  - `Content-Type: image/jpeg`
  - `X-Device-Id`
  - `X-Captured-At`
- WebSocketは **まずブラウザ向けの下り配信** として導入する。
- ESP32-CAMからサーバーへの送信は、初期段階ではHTTPアップロードのままとする。
- 実機がなくても、FlaskテストクライアントやダミーJPEGで配信ロジックを検証できる構成にする。

## 2. 推奨アーキテクチャ

初期実装では、HTTPアップロードを入力、WebSocketをブラウザ配信用の出力として扱う。

```text
ESP32-CAM
  |
  | POST /upload
  | raw JPEG + X-Device-Id + X-Captured-At
  v
Flask server
  |
  | 1. JPEG validation
  | 2. local filesystem save
  | 3. SQLite metadata record
  | 4. inference enqueue
  | 5. latest snapshot event publish
  v
WebSocket hub
  |
  | snapshot event JSON
  v
Browser clients
  |
  | GET /files/<filename> or /api/... による画像取得
  v
<img> refresh
```

この方式では、WebSocketでJPEG本体を毎回送らず、まずは「新しい画像が来た」というイベントを送る。
ブラウザはイベント内のURLを使ってJPEGを取得し、`<img>` の `src` を更新する。

## 3. JPEG本体をWebSocketで送らない理由

初期実装では、WebSocketメッセージはJSONイベントに限定する。

理由:

- Flaskの既存HTTP配信 (`/files/<filename>`) を再利用できる。
- WebSocket接続ごとに大きなJPEGバイナリを複製して送る必要がなく、メモリ使用量を抑えやすい。
- ブラウザ側の実装が単純になる。
- 既存の保存・履歴・推論処理と整合しやすい。
- 実機なしのテストでは、イベントJSONの内容を検証するだけで主要な契約を確認できる。

将来的に低遅延が不足する場合のみ、binary WebSocket frameでJPEGを直接送る案を再評価する。

## 4. WebSocketエンドポイント案

### 4.1 全カメラ購読

```text
GET /ws/snapshots
```

役割:

- 全カメラの新着snapshotイベントを購読する。
- ダッシュボードトップページ向け。

### 4.2 カメラ単位購読

```text
GET /ws/cameras/<camera_id>/snapshots
```

役割:

- 指定カメラの新着snapshotイベントのみ購読する。
- `/cameras/<camera_id>` の詳細ページ向け。

### 4.3 接続時の初期状態

WebSocket接続直後は、必要に応じて最新状態を1件送る。

```json
{
  "type": "snapshot.initial",
  "camera_id": "esp32cam-front-door",
  "snapshot": {
    "id": 123,
    "filename": "image_20260508T010203000000_abcd.jpg",
    "relative_path": "esp32cam-front-door/images/image_20260508T010203000000_abcd.jpg",
    "url": "/files/image_20260508T010203000000_abcd.jpg",
    "size": 10240,
    "captured_at": "2026-05-08T01:02:03+00:00",
    "stored_at": "2026-05-08T01:02:04+00:00",
    "inference_status": "queued"
  }
}
```

最新画像がない場合:

```json
{
  "type": "snapshot.initial",
  "camera_id": "esp32cam-front-door",
  "snapshot": null
}
```

## 5. イベント仕様

### 5.1 新着snapshotイベント

`POST /upload` 成功後、保存とメタデータ登録が完了したタイミングで配信する。

```json
{
  "type": "snapshot.created",
  "camera_id": "esp32cam-front-door",
  "snapshot": {
    "id": 123,
    "filename": "image_20260508T010203000000_abcd.jpg",
    "relative_path": "esp32cam-front-door/images/image_20260508T010203000000_abcd.jpg",
    "url": "/files/image_20260508T010203000000_abcd.jpg",
    "size": 10240,
    "captured_at": "2026-05-08T01:02:03+00:00",
    "stored_at": "2026-05-08T01:02:04+00:00",
    "inference_status": "queued"
  }
}
```

### 5.2 推論完了イベント（後続段階）

推論パイプラインの安定後に追加する。

```json
{
  "type": "inference.completed",
  "camera_id": "esp32cam-front-door",
  "snapshot_id": 123,
  "inference_status": "completed",
  "detections": [
    {
      "label": "person",
      "score": 0.91,
      "bbox": [10, 20, 120, 220]
    }
  ]
}
```

## 6. サーバー内部設計

### 6.1 コンポーネント

| コンポーネント | 役割 |
|---|---|
| `routes.upload_file` | 既存のJPEG受信・保存・メタデータ登録入口 |
| `SnapshotEventPublisher` | 新着snapshotイベントをアプリ内部にpublishする小さな抽象 |
| `WebSocketHub` | 接続中クライアントの管理、camera_idフィルタ、broadcast |
| `templates/*.html` | WebSocket接続と `<img>` 更新処理 |

### 6.2 実装方針

Flask標準だけではWebSocketを直接扱えないため、初期実装では以下のどちらかを採用する。

| 選択肢 | メリット | デメリット | 推奨 |
|---|---|---|---|
| Flask-Sock | FlaskアプリにWebSocket routeを小さく追加できる | 追加依存が必要 | 初期実装に推奨 |
| Flask-SocketIO | 再接続・room・イベント名などが充実 | Socket.IOプロトコルになり素のWebSocketより重い | 多機能化する場合に検討 |
| ASGI化 | 将来の非同期処理と相性がよい | Flask前提の構成変更が大きい | 今は見送り |

初期実装では `Flask-Sock` を使い、依存を `pyproject.toml` に追加する案が最小変更である。

## 7. クライアント側設計

### 7.1 トップページ

- `/ws/snapshots` に接続する。
- `snapshot.created` を受信したら、該当 `camera_id` のカードを更新する。
- `<img>` は `snapshot.url + '?t=' + Date.now()` のようにキャッシュバスター付きで更新する。
- 接続状態を `connected` / `reconnecting` / `disconnected` として表示する。

### 7.2 カメラ詳細ページ

- `/ws/cameras/<camera_id>/snapshots` に接続する。
- 新着snapshotを受信したら、最新画像と履歴リスト先頭を更新する。
- WebSocket未対応ブラウザや切断時は、既存の静的表示を維持する。

### 7.3 再接続

ブラウザ側は指数バックオフではなく、初期段階では固定間隔の再接続で十分とする。

```text
接続失敗 / close
  -> 3秒後に再接続
  -> 成功したら状態表示をconnectedへ戻す
```

## 8. エラー処理・制限

- WebSocket接続数上限を設定可能にする。
  - 例: `ESP32CAM_WS_MAX_CLIENTS=20`
- クライアントが送るメッセージは初期段階では受け付けない。
  - 受信した場合は無視またはエラーJSONを返す。
- broadcast中に送信失敗した接続はhubから除外する。
- JPEG本体はHTTP側の `MAX_CONTENT_LENGTH` で制限する。
- `camera_id` は既存の `X-Device-Id` と同じ値を使う。
- 認証は初期段階ではLAN内利用前提で見送り、外部公開する場合は別Issueで扱う。

## 9. テスト設計

実機なしで検証できるテストを優先する。

| 観点 | テスト内容 |
|---|---|
| 正常系 | ダミーJPEGを `POST /upload` すると `snapshot.created` 相当のイベントが生成される |
| フィルタ | `camera_id` 指定購読では対象カメラのイベントだけ受け取る |
| 複数接続 | 複数クライアントに同じsnapshotイベントが配信される |
| 切断 | 送信失敗したクライアントがhubから取り除かれる |
| 後方互換 | WebSocketが無効でも `POST /upload` と既存画面/APIは通る |
| UI | WebSocket未接続時でも既存HTMLが表示できる |

CIでは少なくとも以下を維持する。

```bash
python -m pytest -q
```

WebSocket実装を追加するPRでは、可能ならWebSocketテストクライアントを使った単体テストを追加する。
難しい場合でも、hubとpublisherを純Pythonクラスとして切り出し、Flask非依存の単体テストを先に通す。

## 10. 段階的実装計画

### Phase 1: 設計ドキュメント

- 本ドキュメントを追加する。
- 既存HTTPアップロード契約を維持する方針を明文化する。

### Phase 2: サーバー内部イベント層

- `SnapshotEventPublisher` または同等の小さな抽象を追加する。
- `POST /upload` 成功時にイベントを生成する。
- 実WebSocketなしで単体テストする。

### Phase 3: WebSocketHubとエンドポイント

- `Flask-Sock` を導入する。
- `/ws/snapshots` と `/ws/cameras/<camera_id>/snapshots` を追加する。
- 接続管理、camera_idフィルタ、送信失敗時の除外を実装する。

### Phase 4: ブラウザUI更新

- トップページとカメラ詳細ページにWebSocket接続処理を追加する。
- 新着snapshot受信時に画像とメタデータを更新する。
- 切断時の状態表示と再接続を追加する。

### Phase 5: 実機検証

- ESP32-CAMから既存 `POST /upload` で連続JPEGを送る。
- ブラウザで更新遅延、メモリ使用量、複数クライアント接続を確認する。
- 問題があればbinary frame配信やMJPEGとの比較を別Issueで検討する。

## 11. 採用しない案

### ESP32-CAMからサーバーへWebSocketで直接JPEG送信

初期段階では採用しない。

理由:

- ESP32-CAM側のファームウェア複雑度が上がる。
- HTTP upload smoke testの既存段階的検証を活かしにくい。
- 切断・再接続・backpressureをESP32側で扱う必要がある。

既存HTTPアップロードが安定した後、低遅延要件が明確になった場合に再検討する。

### 画像保存を省略してWebSocketのみで配信

初期段階では採用しない。

理由:

- 履歴、推論、デバッグ、再表示が難しくなる。
- 現在のsnapshot-first設計と衝突する。
- 実機なしテストでの再現性が落ちる。

## 12. 完了条件

Issue #19 の初期実装完了条件は以下とする。

- `POST /upload` 成功時に、接続中ブラウザへ新着snapshotイベントが届く。
- トップページまたはカメラ詳細ページの最新画像がWebSocketイベントで更新される。
- WebSocketが切断されても、既存HTTP画面とJSON APIは壊れない。
- ダミーJPEGを使った自動テストがCIで通る。
- 実機検証手順がREADMEまたは関連ドキュメントに記載されている。
