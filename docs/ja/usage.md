# 使用方法

このプロジェクトは、以下の機能を備えたWebサーバーを提供します:

### サーバーエンドポイント:

*   **`GET /`**
    *   **説明:** 最新画像を含むメインHTMLページを表示します。
    *   **使用方法:** Webブラウザで `http://<server_ip>:8080/` を開きます。
    *   **レスポンス:** 最新画像プレビュー、画像数、クイックリンクを含むHTMLページ。

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
        *   **ボディ (JSON):** `{"message": "File uploaded successfully", "filename": "<saved_name>", "size": <bytes>, "device_id": "<camera_name>", "captured_at": "<timestamp>", "stored_at": "<timestamp>" }`
    *   **レスポンス (失敗時):**
        *   **ステータスコード:** `400 Bad Request` (データなし、device metadata 欠落、metadata 不正、または JPEG として不正な payload)
        *   **ステータスコード:** `413 Payload Too Large` (サイズ上限超過)
        *   **ステータスコード:** `415 Unsupported Media Type` (JPEG以外)
        *   **ステータスコード:** `500 Internal Server Error` (保存失敗)
    *   **注意:** アップロードされたファイルはサーバー側で一意なファイル名に変換して保存されます。`X-Captured-At` は `2026-03-11T12:34:56Z` のような timezone 付き ISO 8601 を指定してください。

*   **`GET /files`**
    *   **説明:** 保存済みファイルを新しい順に一覧表示するHTMLページ (`templates/files.html`) を表示します。
    *   **使用方法:** Webブラウザで `http://<server_ip>:8080/files` を開きます。
    *   **レスポンス:** クリック可能なファイル名のリストがあるHTMLページ。

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
    *   最新アップロード画像を表示します。
    *   現在の保存画像数を表示します。
    *   files 一覧と health check へのリンクを提供します。

2.  **アップロードされたファイルページ (`/files`):**
    *   サーバーに正常にアップロードされたすべての画像を一覧表示します。
    *   各ファイル名はリンクになっており、クリックすると対応する画像が表示されます。

### サンプルワークフロー (ESP32-CAMからサーバーへ):

1.  **ESP32-CAM:** 画像をキャプチャします。
2.  **ESP32-CAM:** Wi-Fiに接続します。
3.  **ESP32-CAM:** JPEG バイト列を `http://<server_ip>:8080/upload` へ `X-Device-Id` と `X-Captured-At` 付きの HTTP POST リクエストとして送信します。
4.  **サーバー:** データを受信し、設定された storage backend に保存して、成功メッセージとメタデータを返します。
5.  **ユーザー:** ブラウザで `http://<server_ip>:8080/files` を開き、アップロードされた画像のリストを表示します。
6.  **ユーザー:** 画像ファイル名をクリックして表示します。
