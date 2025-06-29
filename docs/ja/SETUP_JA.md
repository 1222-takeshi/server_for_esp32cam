# セットアップガイド

このガイドでは、ESP32マルチカメラシステムのセットアップに必要なハードウェア要件、ソフトウェア要件、およびインストール手順について詳しく説明します。

## ハードウェア要件

このサーバーアプリケーション自体は、標準的なコンピューターまたはサーバー環境 (Windows, Linux, macOS) で動作します。

意図した通りに画像アップロード機能を利用する (例: リモートカメラ設定) ためには、通常以下のものが必要です:

*   **画像キャプチャデバイス:**
    *   **ESP32-CAM:** プロジェクトの文脈から推奨される一般的な選択肢です。このデバイスは、画像をキャプチャし、HTTP POSTリクエストを介してこのサーバーの `/upload` エンドポイントに送信するようにプログラムする必要があります。
    *   **その他のカメラ付きマイクロコントローラー (Wi-Fi対応):** 画像をキャプチャし、HTTPリクエストを作成できる任意のマイクロコントローラー (例: カメラモジュール付きRaspberry Pi、他のESP32バリアント)。
    *   **スマートフォンまたはコンピューター:** テスト目的で画像を送信するためにも使用できます。
*   **ネットワーク接続:** 画像キャプチャデバイスとこのアプリケーションを実行しているサーバーは、同じネットワーク上にあるか、サーバーがデバイスからアクセス可能である必要があります (例: 適切なポートフォワーディングを設定したインターネット経由)。
    *   **複数カメラの場合:** ネットワークが複数の同時画像ストリームを処理するのに十分な帯域幅を持っていることを確認してください。マルチカメラ設定では、サーバーの処理能力とストレージ容量も考慮する必要があります。

## ソフトウェア要件

### サーバーサイド:

*   **Python:** サーバースクリプト (`server.py`) はPythonで書かれています。Python 3.xを推奨します。
*   **Flask:** Webアプリケーションの構築に使用されるPython用のマイクロWebフレームワークです。pipを使用してインストールできます:
    ```bash
    pip install Flask
    ```
*   **オペレーティングシステム:** Pythonを実行できる標準的なオペレーティングシステム (例: Linux, macOS, Windows)。

### クライアントサイド (例: ESP32-CAM):

*   **ESP32-CAMの場合:**
    *   **Arduino IDE** または **PlatformIO IDE:** ESP32-CAMのプログラミング用。
    *   **ESP32 ボードサポートパッケージ:** Arduino IDEまたはPlatformIOにインストールする必要があります。
    *   **関連ライブラリ:** ESP32-CAMで使用される特定のコードに応じて、カメラ操作 (例: `esp_camera.h`)、Wi-Fi接続 (例: `WiFi.h`)、およびHTTPクライアント機能 (例: `HTTPClient.h` またはカスタム実装) のためのライブラリが必要になります。
*   **その他のデバイスの場合:**
    *   画像をキャプチャし、HTTP POSTリクエストを作成するための適切な開発環境とライブラリ。

### オプション (将来の機能強化: ML、I/O制御など):

*   **OpenCV:** 画像処理や物体検出機能を実装する場合、サーバーサイドでOpenCV (Python版 `opencv-python`) が必要になります。
    ```bash
    pip install opencv-python
    ```
*   **TensorFlow/PyTorch (または他のMLフレームワーク):** より高度な物体検出モデル用。
*   **MQTTクライアントライブラリ (例: Paho-MQTT):** I/O制御やセンサーデータのためにESP32デバイスとの双方向通信にMQTTを使用する場合。
    ```bash
    pip install paho-mqtt
    ```
*   **その他の関連Pythonライブラリ:** I/O制御、センサーデータ処理、または特定のMLタスクに選択された方法に応じて。

## セットアップとインストールの手順

### サーバーサイドのセットアップ:

1.  **リポジトリのクローン (任意):**
    まだの場合は、このリポジトリをローカルマシンにクローンします:
    ```bash
    git clone <repository_url>
    cd <repository_directory>
    ```
    （`<repository_url>` と `<repository_directory>` は実際のURLとローカルパスに置き換えてください。）

2.  **Pythonのインストール:**
    Python 3.xがインストールされていることを確認してください。[python.org](https://www.python.org/) からダウンロードできます。

3.  **仮想環境の作成 (推奨):**
    Pythonプロジェクトには仮想環境を作成することをお勧めします。
    ```bash
    python -m venv venv
    ```
    仮想環境をアクティベートします:
    *   Windowsの場合: `venv\Scripts\activate`
    *   macOS/Linuxの場合: `source venv/bin/activate`

4.  **依存関係のインストール:**
    Flask (および後で追加される可能性のある他のPython依存関係) をインストールします:
    ```bash
    pip install Flask
    ```
    将来的に `requirements.txt` ファイルが提供される場合は、すべての依存関係を次のようにインストールできます:
    ```bash
    pip install -r requirements.txt
    ```

5.  **ディレクトリ構造:**
    サーバースクリプト (`script/server.py`) は、アップロードされた画像を保存するために、プロジェクトのベースディレクトリ ( `script/` および `templates/` がある場所) に `uploads` という名前のディレクトリを期待します。サーバー起動時に存在しない場合は自動的に作成されます。

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
    *   ESP32-CAMからサーバーに画像を送信するための多くのサンプルスケッチがオンラインで入手可能です。サーバーのIPアドレスとポートを指すように適合させる必要があります。( `<your_server_ip>` をサーバーの実際のIPアドレスに置き換えてください)。

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
