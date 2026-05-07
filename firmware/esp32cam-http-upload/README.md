# ESP32-CAM PlatformIO Firmware

AI Thinker ESP32-CAM（OV2640想定）をPlatformIOで扱うための最小ファームウェア構成です。

この最初のステップでは、Wi-Fi・カメラ・OTAはまだ使わず、USBシリアル経由で以下を確認します。

- PlatformIOでビルドできる
- USBシリアル変換アダプタ経由で書き込める
- 起動後にSerialログが読める
- PSRAMの有無など、ESP32-CAMの基本情報が見える

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

```fish
cd /Users/takeshimiura/workspace/server_for_esp32cam/firmware/esp32cam-http-upload
pio run
```

## USBシリアル書き込み手順

USBシリアル変換アダプタの場合、書き込み時はESP32-CAMをダウンロードモードに入れる必要があります。

1. ESP32-CAMの `GPIO0` を `GND` に接続する
2. `RST` を押す、または電源を入れ直す
3. 以下を実行する

```fish
pio run -t upload
```

書き込みが完了したら通常起動に戻します。

1. `GPIO0` と `GND` の接続を外す
2. `RST` を押す

## シリアルモニタ

```fish
pio device monitor
```

または明示的にポートを指定します。

```fish
pio device monitor --port /dev/cu.usbserial-1120 --baud 115200
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

終了は `Ctrl+]` です。

## よくある問題

### `pio: command not found`

fishの場合:

```fish
fish_add_path /Users/takeshimiura/Library/Python/3.11/bin
```

### `Failed to connect to ESP32: Timed out waiting for packet header`

ESP32-CAMが書き込みモードに入っていない可能性があります。

- `GPIO0` が `GND` に接続されているか確認
- 接続後に `RST` を押す
- TX/RXがクロス接続になっているか確認
- 5V/GNDが安定しているか確認

### 書き込み後に何もログが出ない

- `GPIO0` と `GND` の接続を外してから `RST` を押す
- `pio device monitor --port /dev/cu.usbserial-1120 --baud 115200` を使う
- TX/RX配線を確認する

## 次のステップ

このSerial smoke testが通ったら、次はWi-Fi接続のみを追加します。

順番:

1. Serial smoke test（このプロジェクト）
2. Wi-Fi接続確認
3. カメラ初期化確認
4. JPEG撮影確認
5. Flaskサーバー `/upload` へのHTTP POST
6. OTA対応
