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
    サーバー側の人検知も使う場合は、ML 依存も追加します:
    ```bash
    pip install -e ".[dev,ml]"
    ```

5.  **ディレクトリ構造:**
    サーバーはデフォルトで `uploads/` に JPEG ファイルを保存します。このディレクトリは存在しない場合、起動時に自動作成されます。

6.  **オプション設定:**
    環境変数でランタイム挙動を変更できます:
    ```bash
    export PORT=8080
    export ESP32CAM_UPLOAD_DIR=/absolute/path/to/uploads
    export ESP32CAM_DB_PATH=/absolute/path/to/esp32cam.sqlite3
    export ESP32CAM_STORAGE_BACKEND=local
    export ESP32CAM_MAX_CONTENT_LENGTH=1048576
    export ESP32CAM_DETECTOR=disabled
    export ESP32CAM_MODEL_PATH=/absolute/path/to/detect.tflite
    export ESP32CAM_LABELS_PATH=/absolute/path/to/labels.txt
    export ESP32CAM_DETECTION_THRESHOLD=0.5
    ```
    運用想定は「まず local PC で開発し、将来は Raspberry Pi 4 / CPU-only へ同じコードを移す」形です。

### クライアントサイドのセットアップ (例: Arduino IDEを使用したESP32-CAM):

この repo には `AI Thinker ESP32-CAM` 向けの最小スケッチ `examples/esp32cam_snapshot_upload/esp32cam_snapshot_upload.ino` が含まれています。
任意のオンライン例を探す前に、まずこのスケッチを起点にすると現行サーバー契約にすぐ合わせられます。

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
    *   後で OTA を使うなら、`ツール > Partition Scheme` を既定の `Huge APP (3MB No OTA)` から、`Regular 4MB with spiffs` か `Minimal SPIFFS` のような OTA 対応レイアウトへ変更してください。

4.  **必要なライブラリのインストール:**
    repo 内の例では次のライブラリを使います:
    *   Wi-Fi (`WiFi.h`)
    *   HTTPクライアント (`HTTPClient.h`)
    *   カメラ (`esp_camera.h`)
    *   時刻同期 (`time.h`)
    *   OTA (`ArduinoOTA.h`, `ESPmDNS.h`, `NetworkUdp.h`)
    ライブラリは `スケッチ > ライブラリをインクルード > ライブラリを管理...` からインストールできます。

5.  **リポジトリ内の ESP32-CAM 例を参照:**
    このリポジトリには `examples/` 配下に最小 ESP32-CAM クライアント例があります。ボード固有のスケッチをゼロから書く代わりに、この例を出発点として使い、Wi-Fi 認証情報、サーバー URL、device ID だけをまず調整してください。
    *   この例は 1 枚だけ撮影し、`/upload` に raw JPEG bytes を送信します。
    *   開発中は、撮影前に短い OTA 待受け時間を取り、deep sleep 無効時はそのまま OTA 更新を受け付けられます。
    *   必要であれば、upload サイクル後に deep sleep へ戻ります。
    *   必須ヘッダは次の 3 つです:
        *   `Content-Type: image/jpeg`
        *   `X-Device-Id`
        *   `X-Captured-At`
    *   例は `AI Thinker ESP32-CAM` の pin map 前提です。別バリアントなら、まず camera pin 定義を合わせてください。
    *   既定の camera 設定は保守的です。実機検証では `SVGA + fb_count=2` よりも `VGA/QVGA + fb_count=1` の方が brownout を避けやすく、`1 MiB` 上限にも収まりやすいことを確認しました。

6.  **X-Captured-At を timezone-aware にする:**
    `X-Captured-At` は timezone 情報を含む ISO 8601 形式で送ってください。`Z` 付きの UTC か、`+09:00` のような明示的な offset を使います。
    *   有効な例: `2026-03-11T12:34:56Z`
    *   有効な例: `2026-03-11T21:34:56+09:00`
    *   無効な例: `2026-03-11T12:34:56`

7.  **ESP32-CAMスケッチのロード:**
    *   サンプルまたはボード固有のスケッチは以下を満たしてください:
        1.  Wi-Fiネットワークに接続する。
        2.  開発中は必要に応じて OTA 待受け時間を設ける。
        3.  NTP で時刻同期する。
        4.  カメラを初期化する。
        5.  画像を 1 枚キャプチャする。
        6.  raw JPEG bytes を `http://<your_server_ip>:8080/upload` に POST する。
        7.  HTTP ステータスとレスポンス本文をシリアルモニターに出力する。
        8.  低電力運用が必要なら、upload サイクル後に deep sleep へ戻る。
    *   upload 先は、ESP32-CAM から到達可能な実際のサーバー IP アドレスとポートにしてください。
    *   repo 内の例は、まずシリアルで切り分けしやすいよう `ENABLE_DEEP_SLEEP = false` を既定にしています。低電力運用では `true` に変更してください。
    *   repo 内の例は `ENABLE_OTA = false` を既定にしています。初回シリアル書き込み後に OTA で反復開発したいときだけ `true` に変更してください。
    *   OTA を使う場合は、`OTA_HOSTNAME` を DNS-safe な名前にし、共有 LAN では `OTA_PASSWORD` も設定してください。
    *   また、撮影 JPEG サイズをサーバー既定の `1 MiB` 上限と比較します。サイズ警告や `413 Payload Too Large` が出る場合は、解像度を下げるか JPEG quality の数値を大きくしてください。

8.  **ESP32-CAMへのスケッチのアップロード:**
    ESP32-CAMをコンピューターに接続し (通常はUSB-シリアルアダプター経由で、フラッシュモードになっていることを確認)、スケッチをアップロードします。

9.  **OTA の準備:**
    *   最初の 1 回はシリアルで書き込みます。
    *   board の partition scheme が OTA 対応であることを確認します。`Huge APP (3MB No OTA)` のままだと snapshot upload は動いても OTA は `OTA error[1]` で失敗します。
    *   反復開発中は deep sleep を無効のままにするか、短い OTA 待受け時間を残します。
    *   `ENABLE_OTA = true` に変更します。
    *   起動後にシリアルへ `OTA ready on ...` が出たら、後続の更新は `espota.py` で送れます。
    ```bash
    python3 ~/.arduino15/packages/esp32/hardware/esp32/3.3.7/tools/espota.py \
      -i <device_ip> \
      -p 3232 \
      -f /path/to/esp32cam_snapshot_upload.ino.bin \
      -a <ota_password_if_set>
    ```

10. **WSL2 NAT の注意 (Windows ホスト):**
    サーバーを WSL2 の NAT モードで動かす場合、ESP32-CAM から WSL の IP へ直接届かないことがあります。
    その場合は Windows ホストの IP を `SERVER_URL` に使い、Windows 側で `:8080` を現在の WSL IP へ転送してください:
    ```powershell
    netsh interface portproxy add v4tov4 listenaddress=0.0.0.0 listenport=8080 connectaddress=<wsl_ip> connectport=8080
    netsh advfirewall firewall add rule name="ESP32 WSL 8080" dir=in action=allow protocol=TCP localport=8080
    ```
    `<wsl_ip>` は WSL 内で `hostname -I` か `ip addr show` で確認できます。
    WSL を再起動すると IP が変わることがあるため、その場合は rule を更新してください。
    その後、Windows から `http://<windows_lan_ip>:8080/healthz` が `{"status":"ok"}` を返すことを確認してから実機を疑ってください。

## サーバー側の人検知

このプロジェクトでは推論を ESP32-CAM 側ではなくサーバー側で行います。
最初の実装は `person` のみを対象とし、local PC でも Raspberry Pi 4 / CPU-only でも同じ経路で動かせるようにしています。

1.  ML extra を入れます:
    ```bash
    pip install -e ".[dev,ml]"
    ```
2.  TFLite モデルと labels ファイルを用意します。
3.  detector を有効にします:
    ```bash
    export ESP32CAM_DETECTOR=tflite_cpu
    export ESP32CAM_MODEL_PATH=/absolute/path/to/detect.tflite
    export ESP32CAM_LABELS_PATH=/absolute/path/to/labels.txt
    export ESP32CAM_PERSON_LABEL=person
    export ESP32CAM_PERSON_CLASS_ID=0
    ```
4.  サーバー起動後、dashboard や camera detail で `pending -> succeeded` へ遷移することを確認します。

detector が無効、または model が見つからない場合でも upload 自体は成功しますが、推論状態は failed として記録されます。

## ローカルでの動作確認

実機接続前に、`curl` で upload 経路を確認できます:

```bash
curl -X POST \
  -H "Content-Type: image/jpeg" \
  -H "X-Device-Id: esp32cam-local-test" \
  -H "X-Captured-At: 2026-03-11T12:34:56Z" \
  --data-binary @sample.jpg \
  http://127.0.0.1:8080/upload
```

成功したら、次を確認してください:

* レスポンス JSON に `device_id`、`captured_at`、`stored_at` が含まれること
* `captured_at` が UTC に正規化されていること
* `GET /files` に新しい画像が表示されること

その後に ESP32-CAM 例を書き込み、シリアルモニタで次を確認してください:

* Wi-Fi 接続状態
* NTP 同期状態
* 撮影 JPEG サイズ
* HTTP status と response body
