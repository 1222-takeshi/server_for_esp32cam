# 使用方法

このプロジェクトは、以下の機能を備えたWebサーバーを提供します:

### サーバーエンドポイント:

*   **`GET /`**
    *   **説明:** 複数カメラの dashboard ページを表示します。
    *   **使用方法:** Webブラウザで `http://<server_ip>:8080/` を開きます。
    *   **レスポンス:** camera ごとの最新 snapshot、状態、検知結果、クイックリンクを含むHTMLページ。

*   **`POST /upload`**
    *   **説明:** JPEG画像をアップロードするためのエンドポイント。サーバーはリクエストボディに raw JPEG データを期待し、将来 backend が変わっても中央の受け口として振る舞います。
    *   **使用方法 (プログラム的、例: ESP32-CAMまたはスクリプトから):**
        画像データをボディに入れてHTTP POSTリクエストを送信します。
        *   **メソッド:** `POST`
        *   **URL:** `http://<server_ip>:8080/upload`
        *   **ヘッダー:** `Content-Type: image/jpeg`, `X-Device-Id: <camera_name>`, `X-Captured-At: <timezone付きISO-8601>`
        *   **ボディ:** JPEG のバイナリデータ。
    *   **レスポンス (成功時):**
        *   **ステータスコード:** `200 OK`
        *   **ボディ (JSON):** `{"message": "File uploaded successfully", "filename": "<saved_name>", "size": <bytes>, "device_id": "<camera_name>", "captured_at": "<UTC timestamp>", "stored_at": "<UTC timestamp>" }`
    *   **レスポンス (失敗時):**
        *   **ステータスコード:** `400 Bad Request` (データなし、device metadata 欠落、metadata 不正、または JPEG として不正な payload)
        *   **ステータスコード:** `413 Payload Too Large` (サイズ上限超過)
        *   **ステータスコード:** `415 Unsupported Media Type` (JPEG以外)
        *   **ステータスコード:** `500 Internal Server Error` (保存失敗)
    *   **注意:** アップロードされたファイルはサーバー側で一意なファイル名に変換して保存されます。`X-Captured-At` は `2026-03-11T12:34:56Z` のような timezone 付き ISO 8601 を指定してください。JSON レスポンスの `captured_at` は UTC に正規化され、`stored_at` はサーバー側の保存時刻を UTC で表します。upload 成功後は非同期で人検知が走りますが、レスポンス自体は推論完了を待ちません。

*   **`GET /cameras`**
    *   **説明:** camera 一覧ページを表示します。
    *   **使用方法:** `http://<server_ip>:8080/cameras`
    *   **レスポンス:** camera 名、状態、detail へのリンクを含むHTMLページ。

*   **`GET /cameras/<camera_id>`**
    *   **説明:** 1 台分の camera detail ページを表示します。
    *   **使用方法:** `http://<server_ip>:8080/cameras/<camera_id>`
    *   **レスポンス:** 最新画像、bbox overlay、最近の snapshots、推論状態を含むHTMLページ。

*   **`GET /api/cameras`**
    *   **説明:** camera summary を JSON で返します。`limit`（1–200、既定50）と `offset`（既定0）でページングできます。レスポンスには全件数の `total`、指定値の `limit`/`offset`、ページ内の `cameras` が含まれます。
    *   **例:** `GET /api/cameras?limit=50&offset=0`

*   **`GET /api/cameras/<camera_id>/snapshots`**
    *   **説明:** 指定 camera の最近の snapshots を JSON で返します。

*   **`GET /api/detections`**
    *   **説明:** 最近の detections を JSON で返します。`camera_id` と `since` で絞り込みできます。

*   **`GET /files`**
    *   **説明:** 保存済みファイルを新しい順に一覧表示するHTMLページ (`templates/files.html`) を表示し、利用可能なら保存済み metadata も併記します。
    *   **使用方法:** Webブラウザで `http://<server_ip>:8080/files` を開きます。
    *   **レスポンス:** クリック可能なファイル名に加えて `device_id`、`captured_at`、`stored_at` の詳細を含むHTMLページ。

*   **`GET /files/<filename>`**
    *   **説明:** `uploads/` ディレクトリから特定のファイルを配信します。
    *   **使用方法:**
        *   通常は `/files` ページ上のリンクをクリックしてアクセスします。
        *   または、Webブラウザで直接 `http://<server_ip>:8080/files/<filename_to_view>` を介してアクセスします。
    *   **レスポンス:** 要求された画像ファイル。

*   **`GET /healthz`**
    *   **説明:** 軽量な health endpoint です。
    *   **使用方法:** `http://<server_ip>:8080/healthz` を開くか監視スクリプトから使用します。
    *   **レスポンス:** `{"status": "ok"}`

### Webインターフェース:

1.  **メインページ (`/`):**
    *   複数 camera の dashboard を表示します。
    *   各 camera の最新 snapshot、最終受信時刻、状態、推論結果を表示します。
    *   camera detail、files 一覧、health check へのリンクを提供します。

2.  **アップロードされたファイルページ (`/files`):**
    *   サーバーに正常にアップロードされたすべての画像を一覧表示します。
    *   利用可能な場合は各画像の `device_id`、`captured_at`、`stored_at`、`inference_status` を表示します。
    *   各ファイル名はリンクになっており、クリックすると対応する画像が表示されます。

3.  **camera detail ページ (`/cameras/<camera_id>`):**
    *   1 台分の最新 snapshot を表示します。
    *   検知がある場合は人検知 bbox を overlay 表示します。
    *   最近の snapshots と推論状態を表示します。

### サンプルワークフロー (ESP32-CAMからサーバーへ):

1.  **ESP32-CAM:** 画像をキャプチャします。
2.  **ESP32-CAM:** Wi-Fiに接続します。
3.  **ESP32-CAM:** JPEG バイト列を `http://<server_ip>:8080/upload` へ `X-Device-Id` と `X-Captured-At` 付きの HTTP POST リクエストとして送信します。
4.  **サーバー:** データを受信し、保存し、camera/snapshot metadata を登録して成功レスポンスを返します。
5.  **サーバー:** 保存済み snapshot に対して非同期で person 検知を実行します。
6.  **ユーザー:** ブラウザで `http://<server_ip>:8080/` または `http://<server_ip>:8080/cameras/<camera_id>` を開き、camera 状態と検知結果を確認します。
