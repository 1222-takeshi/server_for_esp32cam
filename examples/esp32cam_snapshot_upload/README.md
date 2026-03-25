# ESP32-CAM Snapshot Upload Example

This example shows the smallest practical client flow for this repository:

1. Boot the ESP32-CAM
2. Connect to Wi-Fi
3. Optionally expose Arduino OTA
4. Synchronize time with NTP
5. Initialize the camera with conservative defaults
6. Capture one JPEG snapshot
7. POST the raw JPEG bytes to `/upload`
8. Either return to deep sleep or remain OTA-reachable while awake

## Files

- `esp32cam_snapshot_upload.ino`

## What it sends

- `Content-Type: image/jpeg`
- `X-Device-Id: <your device id>`
- `X-Captured-At: <UTC ISO 8601 timestamp>`

The timestamp is formatted as UTC with a `Z` suffix, for example:

```text
2026-03-20T12:34:56Z
```

## Board Assumptions

The sketch is written for the AI Thinker ESP32-CAM pin map. If you use a different ESP32-CAM variant, update the camera pin definitions at the top of the sketch.

## Before Uploading

- Set `WIFI_SSID`
- Set `WIFI_PASSWORD`
- Set `SERVER_URL`
- Set `DEVICE_ID`
- Decide whether `ENABLE_OTA` should stay `false` or be changed to `true` for OTA-based development
- Set `OTA_HOSTNAME` if you want a stable mDNS name such as `front-door-cam.local`
- Set `OTA_PASSWORD` before using OTA on a shared LAN
- Decide whether `ENABLE_DEEP_SLEEP` should stay `false` for debugging or be changed to `true` for low-power runs
- If the server runs inside WSL2 NAT, point `SERVER_URL` at the Windows host IP and add a Windows `portproxy` rule to forward `:8080` to WSL

## Notes

- The sketch logs Wi-Fi status, NTP sync, JPEG size, HTTP response code, and response body to the serial monitor.
- The sketch connects Wi-Fi and synchronizes time before initializing the camera. This reduced brownout resets during real hardware verification.
- `X-Captured-At` is generated immediately before `esp_camera_fb_get()` so it reflects capture time instead of send time.
- The sketch checks the captured JPEG size against the server's default `1 MiB` limit before uploading.
- The default camera settings are intentionally conservative: `VGA` with PSRAM, `QVGA` without PSRAM, and `fb_count = 1`.
- Both success and failure end in a single `finishCycle()` path.
- If deep sleep is enabled, the board sleeps for `DEEP_SLEEP_SECONDS`; otherwise it stays on Wi-Fi and keeps OTA available for the next sketch upload.
- When both OTA and deep sleep are enabled, the board opens a short OTA window before the snapshot cycle.
- This example does not perform retries or backoff. It is intentionally minimal.

## OTA Workflow

1. Flash the sketch once over serial.
2. Set `ENABLE_OTA = true`.
3. Watch the serial monitor for `OTA ready on ...`.
4. Upload later builds over the network while the board is inside the OTA wait window or staying awake with deep sleep disabled.

For `AI Thinker ESP32-CAM`, also choose an OTA-capable partition scheme before the first serial flash.
The board default is typically `Huge APP (3MB No OTA)`, which makes OTA start fail with `OTA error[1]`.
Use `Regular 4MB with spiffs` or `Minimal SPIFFS` instead.

If you use the ESP32 core tools directly, a typical OTA upload command is:

```bash
python3 ~/.arduino15/packages/esp32/hardware/esp32/3.3.7/tools/espota.py \
  -i <device_ip> \
  -p 3232 \
  -f /path/to/esp32cam_snapshot_upload.ino.bin \
  -a <ota_password_if_set>
```

## Windows + WSL2 Note

If the Python server runs inside WSL2 in `nat` networking mode, the ESP32-CAM usually cannot reach the WSL IP directly from the LAN.
Expose the server on the Windows host first, then point `SERVER_URL` to the Windows LAN IP.
Forward `:8080` to the current WSL IP, not `127.0.0.1`:

```powershell
netsh interface portproxy add v4tov4 listenaddress=0.0.0.0 listenport=8080 connectaddress=<wsl_ip> connectport=8080
netsh advfirewall firewall add rule name="ESP32 WSL 8080" dir=in action=allow protocol=TCP localport=8080
```

You can get the current WSL IP with `hostname -I` or `ip addr show`.
Note that the WSL IP can change after a restart, so update the `portproxy` rule when that happens.
