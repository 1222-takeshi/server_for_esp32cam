# プロジェクトの実行

現在、このプロジェクトは複数の ESP32-CAM 系デバイスから JPEG スナップショットを受信・表示するサーバーを提供します。現行マイルストーンは **複数台 snapshot upload とサーバー側の人検知** です。リモート I/O 制御と動画系は後続フェーズです。
この repo には `examples/` 配下に最小 ESP32-CAM 例も含まれているため、スケッチをゼロから書かずに upload 契約を確認できます。

### 1. サーバーの実行:

*   ターミナルで `script` フォルダを含むディレクトリに移動します。
*   仮想環境を作成した場合は、それがアクティブになっていることを確認してください。
    ```bash
    # Windowsの場合
    # venv\Scripts\activate
    # macOS/Linuxの場合
    # source venv/bin/activate
    ```
*   Flaskサーバーを実行します:
    ```bash
    python script/server.py
    ```
*   デフォルトでは、サーバーは `http://0.0.0.0:8080/` で起動します。
*   新しい upload は `uploads/<camera_id>/images/` に保存され、SQLite metadata にも登録されます。
*   detector は既定では無効です。人検知を使う場合は `ml` extra と TFLite 関連の環境変数を設定してください。
*   ターミナルにサーバーが実行中であることを示す出力が表示されます (類似の形式):
    ```
     * Serving Flask app 'server'
     * Debug mode: off
     * Running on all addresses (0.0.0.0)
     * Running on http://127.0.0.1:8080
     * Running on http://<your_local_ip>:8080
    Press CTRL+C to quit
    ```
    同じネットワーク上の別のデバイス (ESP32-CAMなど) からサーバーにアクセスする場合は、`<your_local_ip>` をメモしておきます。

### 2. クライアントの設定と実行 (ESP32-CAM):

*   `examples/esp32cam_snapshot_upload/esp32cam_snapshot_upload.ino` を起点にします。
*   ESP32-CAM が以下のようにプログラムされていることを確認してください:
    *   サーバーと同じWi-Fiネットワークに接続する。
    *   開発中は撮影前に OTA の待受け時間を設けられるようにする。
    *   撮影前に時刻同期する。
    *   JPEG スナップショットを撮影する。
    *   各スナップショットを HTTP POST リクエストとして `http://<server_ip_address>:8080/upload` に送信する。
    *   各 upload に `Content-Type: image/jpeg`、`X-Device-Id`、timezone 付き `X-Captured-At` を付ける。
    *   必要に応じて送信後に deep sleep に戻る。
*   repo 内の例を使う場合は、まず Wi-Fi 認証情報、サーバー IP、device ID だけを変更し、header 名と raw JPEG body はそのままにしてください。
*   repo 内の例は、最初にシリアル確認しやすいよう deep sleep を既定で無効にしています。OTA も既定では無効で、初回シリアル書き込み後に必要なときだけ有効化します。
*   実機検証では、`Wi-Fi -> NTP -> camera -> upload` の順と、保守的な camera 既定値（PSRAM 時 `VGA`, 非 PSRAM 時 `QVGA`, `fb_count = 1`）の方が安定しました。
*   ESP32-CAM に電源を投入して実行します。

### 3. アップロードされた画像へのアクセス:

*   画像がアップロードされたら、まず multi-camera dashboard を開きます:
    `http://<server_ip_address>:8080/`
*   全画像一覧は次です:
    `http://<server_ip_address>:8080/files`
*   特定カメラの詳細は次です:
    `http://<server_ip_address>:8080/cameras/<camera_id>`
*   このページには、正常にアップロードされたすべての画像ファイルが一覧表示されます。ファイル名をクリックすると画像が表示されます。
*   アップロード画像は設定された upload ディレクトリ（デフォルトは `uploads/`）に保存されます。
*   dashboard では camera ごとの `healthy / stale / offline`、最新 snapshot、人検知状態を確認できます。

### 4. Health Check:

*   プロセスの生存確認には以下を利用します:
    `http://<server_ip_address>:8080/healthz`

### 5. WSL2 NAT の注意:

サーバーを WSL2 の NAT モードで動かす場合、ESP32-CAM から WSL の IP へ直接は届かないことがあります。
その場合は Windows ホスト IP を使い、転送先は `127.0.0.1` ではなく現在の WSL IP にします:

```powershell
netsh interface portproxy add v4tov4 listenaddress=0.0.0.0 listenport=8080 connectaddress=<wsl_ip> connectport=8080
netsh advfirewall firewall add rule name="ESP32 WSL 8080" dir=in action=allow protocol=TCP localport=8080
```

`<wsl_ip>` は WSL 内で `hostname -I` か `ip addr show` で確認できます。
その後、ESP32-CAM の送信先を `http://<windows_host_ip>:8080/upload` にし、Windows から `http://<windows_host_ip>:8080/healthz` が通ることを先に確認してください。
WSL を再起動すると IP が変わることがあるため、その場合は rule を更新してください。

### 6. OTA 更新フロー:

最初の 1 回をシリアルで書き込んだ後は、repo 内の例で OTA 更新できます。

*   シリアルに `OTA ready on ...` が出ることを確認する
*   `ENABLE_OTA = true` にし、共有 LAN では `OTA_PASSWORD` も設定する
*   Arduino IDE の network port、または `espota.py` で更新する
    ```bash
    python3 ~/.arduino15/packages/esp32/hardware/esp32/3.3.7/tools/espota.py \
      -i <device_ip> \
      -p 3232 \
      -f /path/to/esp32cam_snapshot_upload.ino.bin \
      -a <ota_password_if_set>
    ```
*   安定した mDNS 名が欲しければ `OTA_HOSTNAME` を設定する
*   本番運用で即 sleep させたい場合は `ENABLE_OTA = false` のままにするか `OTA_WAIT_WINDOW_MS` を短くする

### 7. JSON API:

必要なら次の API も使えます:

*   `GET /api/cameras`
*   `GET /api/cameras/<camera_id>/snapshots`
*   `GET /api/detections`

### 7. すばやい確認フロー:

*   実機前に、まず `curl` で upload 契約をローカル確認します:
    ```bash
    curl -X POST \
      -H "Content-Type: image/jpeg" \
      -H "X-Device-Id: esp32cam-local-test" \
      -H "X-Captured-At: 2026-03-11T12:34:56Z" \
      --data-binary @sample.jpg \
      http://127.0.0.1:8080/upload
    ```
*   その後で ESP32-CAM 例を実行し、繰り返し upload が `/` と `/files` に反映されることを確認します。

### 将来の機能強化:

後続フェーズで次を追加する可能性があります:
*   保存先の切替（NAS / Google Drive）
*   短い動画クリップ
*   通知 / アラート
