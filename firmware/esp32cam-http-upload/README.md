# ESP32-CAM PlatformIO Firmware

AI Thinker ESP32-CAM（OV2640想定）をPlatformIOで扱うための段階的な最小ファームウェア構成です。

現在含まれる確認ステップ:

1. `esp32cam_serial_smoke`: USBシリアル書き込み・通常起動・Serialログ・PSRAM確認
2. `esp32cam_wifi_smoke`: Wi-Fi接続・IPアドレス・RSSI確認
3. `esp32cam_camera_init_smoke`: カメラ初期化・OV2640センサーID確認
4. `esp32cam_jpeg_capture_smoke`: JPEG framebufferを1枚撮影し、形式・サイズ・解像度を確認
5. `esp32cam_http_upload_smoke`: JPEGを1枚撮影し、Mac上のFlaskサーバー `/upload` へHTTP POST送信
6. `esp32cam_websocket_upload`: WebSocketでJPEG binary frameを連続送信
7. `esp32cam_*_ota*`: ArduinoOTA対応ファームウェアのUSB初回書き込み・OTA更新

HTTP POSTを既存の安定経路として残しつつ、WebSocket連続アップロードを追加しています。

## 前提

- ボード: AI Thinker ESP32-CAM想定
- カメラ: OV2640想定
- 書き込み: USBシリアル変換アダプタ
- macOSで検出済みのポート: `/dev/cu.usbserial-1120`
- ボーレート: `115200`

PlatformIO CLIがfishのPATHに入っていない場合:

```fish
fish_add_path /Users/takeshimiura/Library/Python/3.11/bin
```

確認:

```fish
pio --version
pio device list
```

## ビルド

全envをビルドします。

```fish
cd /Users/takeshimiura/workspace/server_for_esp32cam/firmware/esp32cam-http-upload
pio run
```

個別にビルドする場合:

```fish
pio run -e esp32cam_serial_smoke
pio run -e esp32cam_wifi_smoke
pio run -e esp32cam_camera_init_smoke
pio run -e esp32cam_jpeg_capture_smoke
pio run -e esp32cam_http_upload_smoke
```

## Step 1: Serial smoke test

USBシリアル変換アダプタの場合、書き込み時はESP32-CAMをダウンロードモードに入れる必要があります。

1. ESP32-CAMの `GPIO0` を `GND` に接続する
2. `RST` を押す、または電源を入れ直す
3. 以下を実行する

```fish
pio run -e esp32cam_serial_smoke -t upload
```

書き込みが完了したら通常起動に戻します。

1. `GPIO0` と `GND` の接続を外す
2. `RST` を押す

シリアルモニタ:

```fish
pio device monitor -e esp32cam_serial_smoke
```

期待ログ例:

```text
ESP32-CAM PlatformIO serial smoke test
Chip revision: ...
CPU frequency: 240 MHz
Flash size: ... bytes
Free heap: ... bytes
PSRAM: found
Serial smoke test is running.
alive: millis=... free_heap=...
```

## Step 2: Wi-Fi smoke test

Wi-Fi認証情報はコミットしません。ローカルに `include/esp32cam_config.h` を作って設定します。

```fish
cd /Users/takeshimiura/workspace/server_for_esp32cam/firmware/esp32cam-http-upload
cp include/esp32cam_config.h.example include/esp32cam_config.h
```

`include/esp32cam_config.h` を編集します。

```cpp
#define WIFI_SSID "YOUR_WIFI_SSID"
#define WIFI_PASSWORD "YOUR_WIFI_PASSWORD"
#define WIFI_CONNECT_TIMEOUT_MS 20000
#define UPLOAD_URL "http://YOUR_MAC_LAN_IP:8080/upload"
#define DEVICE_ID "esp32cam-01"
#define HTTP_UPLOAD_TIMEOUT_MS 10000
#define DEVICE_API_BASE_URL "http://YOUR_MAC_LAN_IP:8080"
#define DEVICE_API_KEY ""
#define DESIRED_CONFIG_POLL_TIMEOUT_MS 10000
#define DESIRED_CONFIG_FIRMWARE_VERSION "0.3.0"
#define NTP_SERVER "pool.ntp.org"
#define NTP_SYNC_TIMEOUT_MS 10000
```

注意:

- ESP32-CAMは通常 2.4GHz Wi-Fi に接続します。5GHz専用SSIDには接続できません。
- `include/esp32cam_config.h` は `.gitignore` 対象です。SSID/パスワードはコミットしないでください。

ビルド:

```fish
pio run -e esp32cam_wifi_smoke
```

書き込み:

```fish
pio run -e esp32cam_wifi_smoke -t upload
```

通常起動後のモニタ:

```fish
pio device monitor -e esp32cam_wifi_smoke
```

期待ログ例:

```text
ESP32-CAM PlatformIO Wi-Fi smoke test
Local config: include/esp32cam_config.h found
Connecting to Wi-Fi SSID: ...
WiFi connected
IP address: 192.168.1.xxx
Gateway: 192.168.1.1
Subnet: 255.255.255.0
DNS: ...
RSSI: -50 dBm
Next step after this passes: camera init test.
alive: millis=... wifi_status=3 ip=192.168.1.xxx rssi=-50 free_heap=...
```

`wifi_status=3` は `WL_CONNECTED` です。

## Step 3: Camera init smoke test

このステップではJPEG撮影やHTTP送信はまだ行いません。AI Thinker ESP32-CAMのピンマップで `esp_camera_init()` が成功することだけを確認します。

ビルド:

```fish
pio run -e esp32cam_camera_init_smoke
```

書き込み:

1. `GPIO0` を `GND` に接続
2. `RST` を押す
3. 以下を実行

```fish
pio run -e esp32cam_camera_init_smoke -t upload
```

通常起動:

1. `GPIO0` と `GND` の接続を外す
2. `RST` を押す
3. シリアルモニタを開く

```fish
pio device monitor -e esp32cam_camera_init_smoke
```

期待ログ例:

```text
ESP32-CAM PlatformIO camera init smoke test
PSRAM: found
Camera model: AI Thinker ESP32-CAM pin map
Expected sensor: OV2640
Frame size: QVGA
Framebuffer location: PSRAM
Camera init succeeded
Camera sensor PID: 0x26
Camera sensor VER: ...
Camera sensor detected: OV2640
Free heap after camera init: ... bytes
Free PSRAM after camera init: ... bytes
Next step after this passes: JPEG capture test.
alive: millis=... camera_init=ok free_heap=... free_psram=...
```

## Step 4: JPEG capture smoke test

このステップではHTTP送信やファイル保存はまだ行わず、`esp_camera_fb_get()` でJPEG framebufferを1枚取得できることだけを確認します。取得後は必ず `esp_camera_fb_return(fb)` で返却します。

ビルド:

```fish
pio run -e esp32cam_jpeg_capture_smoke
```

書き込み:

1. `GPIO0` を `GND` に接続
2. `RST` を押す
3. 以下を実行

```fish
pio run -e esp32cam_jpeg_capture_smoke -t upload
```

通常起動:

1. `GPIO0` と `GND` の接続を外す
2. `RST` を押す
3. シリアルモニタを開く

```fish
pio device monitor -e esp32cam_jpeg_capture_smoke
```

期待ログ例:

```text
ESP32-CAM PlatformIO JPEG capture smoke test
PSRAM: found
Camera model: AI Thinker ESP32-CAM pin map
Expected sensor: OV2640
Frame size: QVGA
Framebuffer location: PSRAM
Camera init succeeded
Camera sensor PID: 0x26
Camera sensor detected: OV2640
Capturing one frame...
Free heap before capture: ... bytes
Free PSRAM before capture: ... bytes
Frame captured
Format: JPEG
Width: ... px
Height: ... px
Size: ... bytes
Free heap after capture: ... bytes
Free PSRAM after capture: ... bytes
JPEG capture succeeded
Framebuffer returned
Free heap after framebuffer return: ... bytes
Free PSRAM after framebuffer return: ... bytes
Next step after this passes: HTTP POST upload test.
alive: millis=... camera_init=ok jpeg_capture=ok free_heap=... free_psram=...
```

もし `Frame capture failed: esp_camera_fb_get returned null` が出る場合は、5V給電の安定性、カメラフレキの挿し込み、PSRAM検出状態、ボードのピン配置を確認してください。

## Step 5: HTTP upload smoke test

このステップではJPEGを1枚撮影し、既存Flaskサーバーの `POST /upload` に送信します。サーバー側の `/upload` は以下を要求します。

- `Content-Type: image/jpeg`
- `X-Device-Id: <device id>`
- `X-Captured-At: <timezone付きISO 8601>`
- JPEG SOI (`0xff 0xd8`) で始まるnon-empty body

### Mac上でFlaskサーバーを起動

ESP32-CAMから到達できるように `0.0.0.0:8080` で起動します。

```fish
cd /Users/takeshimiura/workspace/server_for_esp32cam
python -m script.server
```

MacのLAN IPを確認します。

```fish
ipconfig getifaddr en0
```

有線LANなど `en0` 以外を使っている場合は、利用中インターフェースのIPv4アドレスを使ってください。`localhost` / `127.0.0.1` はESP32-CAM自身を指すため使えません。

### ローカル設定

`include/esp32cam_config.h` の `UPLOAD_URL` をMacのLAN IPに合わせます。

```cpp
#define WIFI_SSID "YOUR_WIFI_SSID"
#define WIFI_PASSWORD "YOUR_WIFI_PASSWORD"
#define WIFI_CONNECT_TIMEOUT_MS 20000
#define UPLOAD_URL "http://192.168.1.23:8080/upload"
#define DEVICE_ID "esp32cam-01"
#define HTTP_UPLOAD_TIMEOUT_MS 10000
#define DEVICE_API_BASE_URL "http://YOUR_MAC_LAN_IP:8080"
#define DEVICE_API_KEY ""
#define DESIRED_CONFIG_POLL_TIMEOUT_MS 10000
#define DESIRED_CONFIG_FIRMWARE_VERSION "0.3.0"
#define NTP_SERVER "pool.ntp.org"
#define NTP_SYNC_TIMEOUT_MS 10000
```

### ビルド・書き込み・確認

ビルド:

```fish
pio run -e esp32cam_http_upload_smoke
```

書き込み:

1. `GPIO0` を `GND` に接続
2. `RST` を押す
3. 以下を実行

```fish
pio run -e esp32cam_http_upload_smoke -t upload
```

通常起動:

1. `GPIO0` と `GND` の接続を外す
2. `RST` を押す
3. シリアルモニタを開く

```fish
pio device monitor -e esp32cam_http_upload_smoke
```

期待ログ例:

```text
ESP32-CAM PlatformIO HTTP upload smoke test
Upload URL: http://192.168.1.23:8080/upload
Device ID: esp32cam-01
WiFi connected
Syncing time with NTP server: pool.ntp.org
Captured-At header: 2026-...
Camera init succeeded
Frame captured
Format: JPEG
HTTP POST upload starting...
HTTP status code: 200
HTTP response body: {"captured_at":...,"device_id":"esp32cam-01",...}
HTTP upload result: ok
Framebuffer returned
alive: millis=... wifi=ok camera_init=ok jpeg_capture=ok http_upload=ok http_status=200 free_heap=... free_psram=...
```

サーバー側では `uploads/<DEVICE_ID>/images/` にJPEGが保存され、`/files` や `/cameras/<DEVICE_ID>` から確認できます。

## Desired config polling / ack

`esp32cam_http_upload_smoke` と OTA 対応HTTP upload envでは、Wi-Fi接続後・撮影前にサーバーの device API から desired config を取得できます。

ローカルの `include/esp32cam_config.h` で次を設定します。

```cpp
#define DEVICE_API_BASE_URL "http://192.168.1.23:8080"
#define DEVICE_API_KEY "per-device-api-key"  // ESP32CAM_DEVICE_API_KEYS 未使用なら空文字でも可
#define DESIRED_CONFIG_POLL_TIMEOUT_MS 10000
#define DESIRED_CONFIG_FIRMWARE_VERSION "0.3.0"
```

起動時の処理:

1. `GET /api/devices/<DEVICE_ID>/desired-config?current_version=<n>` を送信します。
2. `capture_interval_seconds`, `frame_size`, `jpeg_quality`, `vflip`, `hmirror` をローカル設定へ反映します。
3. `POST /api/devices/<DEVICE_ID>/desired-config/ack` で `version`, `applied`, `message`, `firmware_version`, `free_heap` を送信します。
4. `ota_update_requested=true` かつ `ENABLE_ARDUINO_OTA` と非空 `OTA_PASSWORD` が設定されている場合、`ota_window_seconds` の間だけ OTA window を開きます。
5. `restart_requested=true` の場合、ack後に `ESP.restart()` します。

`DEVICE_API_KEY` は `/upload` の `X-API-Key` にも付与されます。サーバー側で `ESP32CAM_DEVICE_API_KEYS` を設定した場合は、`DEVICE_ID` に対応するキーを必ず設定してください。

## よくある問題

### `pio: command not found`

fishの場合:

```fish
fish_add_path /Users/takeshimiura/Library/Python/3.11/bin
```

### `Failed to connect to ESP32: No serial data received` / `Timed out waiting for packet header`

ESP32-CAMが書き込みモードに入っていない、またはUSBシリアル配線でESP32-CAMからの応答を読めていない可能性があります。

まずMac側は以下を確認します。

```fish
pio device list
lsof /dev/cu.usbserial-1120
```

`lsof` に `screen` や `pio device monitor` が出る場合は、そのプロセスを終了してから再実行してください。

そのうえで、手動書き込みは次の順番で試します。

1. `GPIO0` を `GND` に接続する
2. USBシリアルの `GND` とESP32-CAMの `GND` が確実に共通になっていることを確認する
3. `pio run -e esp32cam_jpeg_capture_smoke -t upload` を実行する
4. `Connecting...` の `.` が出始めたら、`GPIO0-GND` は接続したまま `RST` を1回押す
5. それでも失敗する場合は、`GPIO0-GND` 接続後に先に `RST` を押してから upload を実行する

配線も確認してください。

| USBシリアル | ESP32-CAM |
|---|---|
| 5V | 5V |
| GND | GND |
| TX | U0R / RX |
| RX | U0T / TX |

よくある原因:

- `GPIO0` と `GND` をつないだ後に `RST` していない
- TX/RXが逆、または同名同士に接続していてクロスになっていない
- 3.3V給電で電流不足になっている（ESP32-CAMは5V給電推奨）
- USBシリアル変換アダプタ側の電圧設定が合っていない
- カメラ初期化時の突入で電源が落ちている

### シリアルモニタに `DOWNLOAD_BOOT` / `waiting for download` が出る

`GPIO0` と `GND` がまだ接続されています。

1. モニタを終了する
2. `GPIO0-GND` を外す
3. `RST` を押す
4. 再度 `pio device monitor -e esp32cam_jpeg_capture_smoke`

### `Camera init failed: esp_camera_init error=0x...` が出る

主な確認点:

- ESP32-CAMへ5Vで安定給電しているか
- USBシリアル変換アダプタの5V出力が不足していないか
- GNDが共通になっているか
- カメラのフレキケーブルが奥まで刺さっていて、ラッチが閉じているか
- ボードがAI Thinker互換ピン配置か
- カメラモジュールがOV2640か
- PSRAMが `found` になっているか

カメラ初期化はWi-Fi接続より電源変動の影響を受けやすいです。失敗や再起動が出る場合は、短いUSBケーブル、安定した5V電源、配線の接触を優先して確認してください。

## 次のステップ

HTTP upload smoke testが通ったら、次は連続アップロード、OTA対応、またはWebSocket方式への移行検討に進みます。

順番:

1. Serial smoke test: 完了
2. Wi-Fi接続確認: 完了
3. カメラ初期化確認: 完了
4. JPEG撮影確認: 完了
5. Flaskサーバー `/upload` へのHTTP POST: このプロジェクト
6. 連続アップロード / OTA対応 / WebSocket方式への移行検討


## ArduinoOTA / PlatformIO OTA upload

OTA updates are intended for the next step after the USB-based HTTP upload smoke
test has succeeded. Flash the OTA-capable firmware over USB first so the ESP32-CAM has a
known-good recovery path and an OTA-capable partition table.

The USB bootstrap environment keeps the existing `POST /upload` HTTP upload smoke
test and also enables ArduinoOTA:

```ini
[env:esp32cam_http_upload_ota_usb]
upload_protocol = esptool
```

The network OTA environment reuses the same firmware build flags, but uploads with
`espota`:

```ini
[env:esp32cam_http_upload_ota]
extends = env:esp32cam_http_upload_ota_usb
upload_protocol = espota
```

1. Copy `include/esp32cam_config.h.example` to `include/esp32cam_config.h` and
   set Wi-Fi, `UPLOAD_URL`, and local OTA settings. Do not commit the local file.
2. Build and upload the OTA-capable firmware once over USB:

   ```bash
   pio run -e esp32cam_http_upload_ota_usb -t upload
   pio device monitor -e esp32cam_http_upload_ota_usb
   ```

3. Confirm the serial log shows `OTA ready` and that the HTTP upload smoke test
   still succeeds with `HTTP status code: 200` against `POST /upload`.
4. Note the ESP32-CAM address from the serial log. IP address is the most
   reliable upload destination, especially when mDNS/hostname resolution does
   not work on the LAN:

   ```text
   WiFi connected
   IP address: 192.168.1.50
   OTA ready: hostname=esp32cam-01 port=3232 ip=192.168.1.50
   ```

   `esp32cam_config.h` configures the OTA service running on the ESP32-CAM
   (`OTA_HOSTNAME`, `OTA_PORT`, `OTA_PASSWORD`). PlatformIO still needs the
   host-side upload destination through environment variables or
   `--upload-port`.
5. Upload a later build over Wi-Fi from the same LAN:

   ```bash
   export ESP32CAM_OTA_HOST=192.168.1.50
   export ESP32CAM_OTA_PORT=3232
   export ESP32CAM_OTA_PASSWORD=<local OTA password, if configured>
   pio run -e esp32cam_http_upload_ota -t upload
   ```

   If mDNS works on your LAN, the hostname from `OTA_HOSTNAME` may also work:

   ```bash
   export ESP32CAM_OTA_HOST=esp32cam-01.local
   ```

   To avoid retyping these values, keep a local ignored file such as
   `.env.local` and load it before OTA upload:

   ```bash
   source .env.local
   pio run -e esp32cam_http_upload_ota -t upload
   ```

   Example `.env.local`:

   ```bash
   export ESP32CAM_OTA_HOST=192.168.1.50
   export ESP32CAM_OTA_PORT=3232
   export ESP32CAM_OTA_PASSWORD=
   ```

   For stable OTA operation, consider assigning a fixed DHCP lease for the
   ESP32-CAM in the router so the IP address does not change.

If OTA upload fails, reconnect USB serial, put the module into download mode, and
flash the same environment again. After an OTA update, re-run the HTTP upload
smoke test and verify the server still receives `POST /upload`.

## WebSocket continuous upload

`esp32cam_websocket_upload` sends captured JPEG frames as binary WebSocket
messages to the Flask server endpoint:

```text
/ws/cameras/<DEVICE_ID>/upload
```

The server stores each received binary JPEG frame through the same snapshot
storage, metadata, inference queue, and browser WebSocket notification path used
by HTTP `POST /upload`. This keeps the browser-side real-time update behavior
unchanged while allowing the ESP32-CAM to stream frames over a persistent socket.

1. Start the Flask server on the LAN machine.
2. Copy `include/esp32cam_config.h.example` to `include/esp32cam_config.h` and
   set at least:

   ```cpp
   #define WIFI_SSID "..."
   #define WIFI_PASSWORD "..."
   #define DEVICE_ID "esp32cam-01"
   #define WS_UPLOAD_HOST "192.168.1.23"  // Flask server LAN IP
   #define WS_UPLOAD_PORT 8080
   #define WS_UPLOAD_PATH "/ws/cameras/" DEVICE_ID "/upload"
   ```

3. Configure continuous capture:

   ```cpp
   #define CAPTURE_INTERVAL_MS 5000  // capture one frame every 5 seconds
   #define CAPTURE_FRAME_LIMIT 0     // 0 = unlimited
   ```

   For a video-like burst, set `CAPTURE_FRAME_LIMIT` to a positive number. For
   example, `CAPTURE_INTERVAL_MS=200` and `CAPTURE_FRAME_LIMIT=50` captures up
   to 50 frames at roughly 5 fps, depending on Wi-Fi/camera throughput.

4. Build and upload:

   ```bash
   pio run -e esp32cam_websocket_upload -t upload
   pio device monitor -e esp32cam_websocket_upload
   ```

   OTA-capable WebSocket firmware can also be installed first over USB and then
   updated over Wi-Fi:

   ```bash
   pio run -e esp32cam_websocket_upload_ota_usb -t upload
   source .env.local
   pio run -e esp32cam_websocket_upload_ota -t upload
   ```

The loop calls both `webSocket.loop()` and `ArduinoOTA.handle()` frequently, so
OTA remains responsive while continuous capture is running. If the frame limit is
reached, capture stops but the main loop keeps running for OTA/alive logging.

### Roadmap: recognition, tracking, servo control

The current implementation intentionally stops at reliable frame ingestion:

- ESP32-CAM captures frames and sends JPEG binary frames over WebSocket.
- The server stores each frame and enqueues it for the existing inference path.
- Browser clients continue receiving snapshot notifications over WebSocket.

Next layers can be added on top of this path without changing the ingestion
contract:

1. run image recognition on stored frame sequences server-side,
2. derive target positions from image-based tracking,
3. expose a command channel back to the ESP32-CAM,
4. drive pan/tilt servos from tracking results.
