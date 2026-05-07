# セットアップとインストール

### サーバーサイドのセットアップ:

#### macOSで開発する場合

1.  **リポジトリのクローン:**
    ```bash
    git clone https://github.com/1222-takeshi/server_for_esp32cam.git
    cd server_for_esp32cam
    ```

2.  **Pythonの確認:**
    macOS標準のPythonではなく、Homebrew、pyenv、またはpython.org配布版などで管理しているPython 3系の利用を推奨します。まずバージョンを確認してください。
    ```bash
    python3 --version
    ```

3.  **仮想環境の作成:**
    このプロジェクトでは、ローカル開発用の仮想環境名を `.venv` に統一します。
    ```bash
    python3 -m venv .venv
    source .venv/bin/activate
    python -m pip install --upgrade pip
    ```

4.  **依存関係のインストール:**
    Flaskやpytestなどの依存関係は `requirements.txt` からインストールします。
    ```bash
    pip install -r requirements.txt
    ```

5.  **セットアップ確認:**
    ```bash
    python -m py_compile script/server.py
    pytest
    ```
    これらが失敗する場合は、コードまたはテスト環境側の問題を先に解消してください。

6.  **ディレクトリ構造:**
    サーバースクリプト (`script/server.py`) は、アップロードされた画像を保存するために、プロジェクトのベースディレクトリに `uploads` という名前のディレクトリを使用します。サーバー起動時に存在しない場合は自動的に作成されます。`uploads/` は実行時生成物のためGit管理対象外です。

#### Windows / WSL2 / Linuxで開発する場合

1.  **リポジトリのクローン:**
    ```bash
    git clone https://github.com/1222-takeshi/server_for_esp32cam.git
    cd server_for_esp32cam
    ```

2.  **仮想環境の作成:**
    ```bash
    python -m venv .venv
    # Windowsの場合
    .venv\Scripts\activate
    # WSL2/Linuxの場合
    source .venv/bin/activate
    ```

3.  **依存関係のインストール:**
    ```bash
    python -m pip install --upgrade pip
    pip install -r requirements.txt
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
        4.  画像データ (例: JPEG) をHTTP POSTリクエストを介してサーバーの `/upload` エンドポイント (例: `http://<your_server_ip>:8080/upload`) に送信する。
    *   ESP32-CAMからサーバーに画像を送信するための多くのサンプルスケッチがオンラインで入手可能です。サーバーのIPアドレスとポートを指すように適合させる必要があります。

    **サンプルスニペット (概念的 - 画像データ送信用):**
    ```c++
    // ... (WiFi接続とカメラ設定) ...

    #include <HTTPClient.h>

    void sendPhotoToServer(uint8_t *imageData, size_t imageLen) {
      HTTPClient http;
      // <your_server_ip> を server.py を実行しているマシンの実際のIPに置き換えてください
      http.begin("http://<your_server_ip>:8080/upload");
      http.addHeader("Content-Type", "image/jpeg"); // または適切なコンテントタイプ

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
