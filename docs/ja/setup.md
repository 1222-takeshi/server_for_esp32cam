# セットアップとインストール

### サーバーサイドのセットアップ:

1.  **リポジトリのクローン (任意):**
    まだの場合は、このリポジトリをローカルマシンにクローンします:
    ```bash
    git clone <repository_url>
    cd <repository_directory>
    ```

2.  **Pythonのインストール:**
    Python 3.xがインストールされていることを確認してください。[python.org](https://www.python.org/) からダウンロードできます。

3.  **仮想環境の作成 (推奨):**
    Pythonプロジェクトには仮想環境を作成することをお勧めします。
    ```bash
    python -m venv venv
    # Windowsの場合
    venv\Scripts\activate
    # macOS/Linuxの場合
    source venv/bin/activate
    ```

4.  **依存関係のインストール:**
    `pyproject.toml` を使って、editable mode と development dependency をまとめてインストールします:
    ```bash
    pip install -e ".[dev]"
    ```

5.  **ディレクトリ構造:**
    サーバーはデフォルトで `uploads/` に JPEG ファイルを保存します。このディレクトリは存在しない場合、起動時に自動作成されます。

6.  **オプション設定:**
    環境変数でランタイム挙動を変更できます:
    ```bash
    export PORT=8080
    export ESP32CAM_UPLOAD_DIR=/absolute/path/to/uploads
    export ESP32CAM_STORAGE_BACKEND=local
    export ESP32CAM_MAX_CONTENT_LENGTH=1048576
    ```

### クライアントサイドのセットアップ (例: Arduino IDEを使用したESP32-CAM):

これは一般的なガイドです。ESP32-CAMボードのバージョンや使用する正確なコードによって詳細は異なる場合があります。

1.  **Arduino IDEのインストール:**
    [arduino.cc](https://www.arduino.cc/) から最新のArduino IDEをダウンロードしてインストールします。

2.  **ESP32ボードサポートの設定:**
    *   Arduino IDEを開きます。
    *   `ファイル > 環境設定` に進みます。
    *   「追加のボードマネージャURL」フィールドに次のURLを追加します:
        ```
        https://raw.githubusercontent.com/espressif/arduino-esp32/gh-pages/package_esp32_index.json
        ```
    *   「OK」をクリックします。
    *   `ツール > ボード > ボードマネージャ...` に進みます。
    *   「esp32」を検索し、Espressif Systemsによるパッケージをインストールします。

3.  **ESP32-CAMボードの選択:**
    *   `ツール > ボード` に進み、特定のESP32-CAMモデル (例: "AI Thinker ESP32-CAM") を選択します。
    *   ボードの要件に応じて `ツール` の下の他の設定 (例: ポート、書き込み装置) を調整します。

4.  **必要なライブラリのインストール:**
    ESP32-CAMスケッチによっては、以下のライブラリをインストールする必要がある場合があります:
    *   Wi-Fi (`WiFi.h`)
    *   HTTPクライアント (`HTTPClient.h`)
    *   カメラ (`esp_camera.h`)
    ライブラリは `スケッチ > ライブラリをインクルード > ライブラリを管理...` からインストールできます。

5.  **公式ESP32カメラドキュメントの参照:**
    カメラの初期化、設定、および高度な使用方法に関する詳細情報については、公式の `esp32-camera` ドライバリポジトリが非常に貴重なリソースです:
    *   [GitHub上のespressif/esp32-camera](https://github.com/espressif/esp32-camera)
    このリポジトリには、カメラドライバのソースコード、サンプル、および堅牢なクライアントサイドファームウェアの開発に非常に役立つさらなるドキュメントが含まれています。

6.  **ESP32-CAMスケッチのロード:**
    *   ESP32-CAMには、以下の機能を持つArduinoスケッチが必要です:
        1.  カメラを初期化する。
        2.  Wi-Fiネットワークに接続する。
        3.  画像をキャプチャする。
        4.  画像データを JPEG として中央サーバーの `/upload` エンドポイント (例: `http://<your_server_ip>:8080/upload`) に HTTP POST リクエストで送信する。
        5.  低電力運用が必要な場合は、送信後に deep sleep へ戻る。
    *   ESP32-CAMからサーバーに画像を送信するための多くのサンプルスケッチがオンラインで入手可能です。サーバーのIPアドレスとポートを指すように適合させる必要があります。

    **サンプルスニペット (概念的 - 画像データ送信用):**
    ```c++
    // ... (WiFi接続とカメラ設定) ...

    #include <HTTPClient.h>

    void sendPhotoToServer(uint8_t *imageData, size_t imageLen) {
      HTTPClient http;
      // <your_server_ip> を server.py を実行しているマシンの実際のIPに置き換えてください
      http.begin("http://<your_server_ip>:8080/upload");
      http.addHeader("Content-Type", "image/jpeg");

      int httpResponseCode = http.POST(imageData, imageLen);

      if (httpResponseCode > 0) {
        String response = http.getString();
        Serial.println(httpResponseCode);
        Serial.println(response);
      } else {
        Serial.print("Error on sending POST: ");
        Serial.println(httpResponseCode);
      }
      http.end();
    }

    // メインループまたは関数内:
    // camera_fb_t * fb = esp_camera_fb_get();
    // if (fb) {
    //   sendPhotoToServer(fb->buf, fb->len);
    //   esp_camera_fb_return(fb);
    // }
    ```

7.  **ESP32-CAMへのスケッチのアップロード:**
    ESP32-CAMをコンピューターに接続し (通常はUSB-シリアルアダプター経由で、フラッシュモードになっていることを確認)、スケッチをアップロードします。

## ローカルでの動作確認

実機接続前に、`curl` で upload 経路を確認できます:

```bash
curl -X POST \
  -H "Content-Type: image/jpeg" \
  --data-binary @sample.jpg \
  http://127.0.0.1:8080/upload
```
