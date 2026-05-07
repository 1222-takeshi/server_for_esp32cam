# ESP32-CAM PlatformIO Firmware

AI Thinker ESP32-CAM（OV2640想定）をPlatformIOで扱うための段階的な最小ファームウェア構成です。

現在含まれる確認ステップ:

1. `esp32cam_serial_smoke`: USBシリアル書き込み・通常起動・Serialログ・PSRAM確認
2. `esp32cam_wifi_smoke`: Wi-Fi接続・IPアドレス・RSSI確認
3. `esp32cam_camera_init_smoke`: カメラ初期化・OV2640センサーID確認

HTTP upload、OTAはまだこのステップでは扱いません。

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
cd /Users/takeshimiura/workspace/server_for_esp32cam/.hermes/worktrees/issue56-camera-init-smoke/firmware/esp32cam-http-upload
pio run
```

個別にビルドする場合:

```fish
pio run -e esp32cam_serial_smoke
pio run -e esp32cam_wifi_smoke
pio run -e esp32cam_camera_init_smoke
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
cd /Users/takeshimiura/workspace/server_for_esp32cam/.hermes/worktrees/issue56-camera-init-smoke/firmware/esp32cam-http-upload
cp include/esp32cam_config.h.example include/esp32cam_config.h
```

`include/esp32cam_config.h` を編集します。

```cpp
#define WIFI_SSID "YOUR_WIFI_SSID"
#define WIFI_PASSWORD "YOUR_WIFI_PASSWORD"
#define WIFI_CONNECT_TIMEOUT_MS 20000
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
Frame size for init smoke: QVGA
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
3. `pio run -e esp32cam_camera_init_smoke -t upload` を実行する
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
4. 再度 `pio device monitor -e esp32cam_camera_init_smoke`

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

Camera init smoke testが通ったら、次はJPEG撮影確認のみを追加します。

順番:

1. Serial smoke test: 完了
2. Wi-Fi接続確認: 完了
3. カメラ初期化確認: このプロジェクト
4. JPEG撮影確認
5. Flaskサーバー `/upload` へのHTTP POST
6. OTA対応
