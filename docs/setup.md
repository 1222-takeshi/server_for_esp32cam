# Setup and Installation

## Server-Side Setup:

1.  **Clone the Repository (Optional):**
    If you haven't already, clone this repository to your local machine:
    ```bash
    git clone <repository_url>
    cd <repository_directory>
    ```

2.  **Install Python:**
    Ensure you have Python 3.x installed. You can download it from [python.org](https://www.python.org/).

3.  **Create a Virtual Environment (Recommended):**
    It's good practice to create a virtual environment for Python projects.
    ```bash
    python -m venv venv
    # On Windows
    venv\Scripts\activate
    # On macOS/Linux
    source venv/bin/activate
    ```

4.  **Install Dependencies:**
    Install the project in editable mode with development dependencies:
    ```bash
    pip install -e ".[dev]"
    ```
    If you want to run server-side person detection on your local PC or Raspberry Pi later, install the optional ML dependencies too:
    ```bash
    pip install -e ".[dev,ml]"
    ```

5.  **Directory Structure:**
    The server stores uploaded JPEG files in `uploads/` by default. The directory is created automatically if it does not exist.

6.  **Optional Configuration:**
    You can change runtime behavior with environment variables:
    ```bash
    export PORT=8080
    export ESP32CAM_UPLOAD_DIR=/absolute/path/to/uploads
    export ESP32CAM_DB_PATH=/absolute/path/to/esp32cam.sqlite3
    export ESP32CAM_STORAGE_BACKEND=local
    export ESP32CAM_MAX_CONTENT_LENGTH=1048576
    # Set either value to 0 or leave it unset to disable that retention rule.
    export ESP32CAM_STORAGE_RETENTION_MAX_AGE_SECONDS=604800
    export ESP32CAM_STORAGE_RETENTION_MAX_BYTES=1073741824
    export ESP32CAM_DETECTOR=disabled
    export ESP32CAM_MODEL_PATH=/absolute/path/to/detect.tflite
    export ESP32CAM_LABELS_PATH=/absolute/path/to/labels.txt
    export ESP32CAM_DETECTION_THRESHOLD=0.5
    ```
    The intended deployment path is local PC first, then Raspberry Pi 4 / CPU-only with the same server-side detector.

### Client-Side Setup (Example: ESP32-CAM with Arduino IDE):

This repository includes a minimal `AI Thinker ESP32-CAM` sketch at `examples/esp32cam_snapshot_upload/esp32cam_snapshot_upload.ino`.
Use it as the starting point instead of an arbitrary online example so the request format matches this server immediately.

1.  **Install Arduino IDE:**
    Download and install the latest Arduino IDE from [arduino.cc](https://www.arduino.cc/).

2.  **Configure ESP32 Board Support:**
    *   Open Arduino IDE.
    *   Go to `File > Preferences`.
    *   In the "Additional Boards Manager URLs" field, add the following URL:
        ```
        https://raw.githubusercontent.com/espressif/arduino-esp32/gh-pages/package_esp32_index.json
        ```
    *   Click "OK".
    *   Go to `Tools > Board > Boards Manager...`.
    *   Search for "esp32" and install the package by Espressif Systems.

3.  **Select ESP32-CAM Board:**
    *   Go to `Tools > Board` and select your specific ESP32-CAM model (e.g., "AI Thinker ESP32-CAM").
    *   Adjust other settings under `Tools` as per your board's requirements (e.g., Port, Programmer).
    *   If you plan to use OTA later, change `Tools > Partition Scheme` from the common default `Huge APP (3MB No OTA)` to an OTA-capable layout such as `Regular 4MB with spiffs` or `Minimal SPIFFS`.

4.  **Install Necessary Libraries:**
    The in-repo example uses:
    *   Wi-Fi (`WiFi.h`)
    *   HTTP Client (`HTTPClient.h`)
    *   Camera (`esp_camera.h`)
    *   Time sync (`time.h`)
    *   OTA (`ArduinoOTA.h`, `ESPmDNS.h`, `NetworkUdp.h`)
    You can install libraries via `Sketch > Include Library > Manage Libraries...`.

5.  **Refer to the In-Repo ESP32-CAM Example:**
    This repository now keeps a minimal ESP32-CAM client example under `examples/`. Use it as the starting point for your board-specific sketch and adapt only the Wi-Fi credentials, server URL, and device identifier first.
    *   The example captures one JPEG, sends the raw bytes to `/upload`, and can optionally enter deep sleep after the upload cycle.
    *   During development, the same sketch can also wait briefly for OTA updates and stay OTA-reachable after a snapshot when deep sleep is disabled.
    *   It also shows the required headers:
        *   `Content-Type: image/jpeg`
        *   `X-Device-Id`
        *   `X-Captured-At`
    *   The example is pinned to the `AI Thinker ESP32-CAM` pin map. If you use another board variant, update the camera pin definitions before debugging server behavior.
    *   The default camera settings are intentionally conservative (`VGA` with PSRAM, `QVGA` without PSRAM, `fb_count = 1`) because real hardware verification showed that larger defaults can trigger brownout resets on modest power rails.

6.  **Keep the Timestamp Timezone-Aware:**
    `X-Captured-At` must be an ISO 8601 timestamp with timezone information. Use either `Z` for UTC or an explicit offset such as `+09:00`.
    *   Valid example: `2026-03-11T12:34:56Z`
    *   Valid example: `2026-03-11T21:34:56+09:00`
    *   Invalid example: `2026-03-11T12:34:56`

7.  **Load ESP32-CAM Sketch:**
    *   The sample sketch or your board-specific variant should:
        1.  Connect to your Wi-Fi network.
        2.  Optionally wait for an OTA window during development.
        3.  Synchronize time with NTP.
        4.  Initialize the camera.
        5.  Capture one image.
        6.  POST the raw JPEG bytes to `http://<your_server_ip>:8080/upload`.
        7.  Log the HTTP status and response body to the serial monitor.
        8.  Enter deep sleep after the upload cycle when low-power operation is desired.
    *   The upload target must point to the actual server IP address and port that are reachable from the ESP32-CAM.
    *   The in-repo example keeps `ENABLE_DEEP_SLEEP = false` by default so you can debug over serial first. Set it to `true` for low-power operation.
    *   The in-repo example keeps `ENABLE_OTA = false` by default. After the first serial flash, set it to `true` if you want the board to stay OTA-reachable during development.
    *   If OTA is enabled, set `OTA_HOSTNAME` to a DNS-safe name and set `OTA_PASSWORD` before using OTA on a shared LAN.
    *   It also checks the captured JPEG size against the server's default `1 MiB` upload limit. If you see a size warning or `413 Payload Too Large`, lower the frame size or increase the JPEG quality value.

8.  **Upload Sketch to ESP32-CAM:**
    Connect your ESP32-CAM to your computer (usually via a USB-to-serial adapter, ensuring it's in flashing mode) and upload the sketch.

9.  **Optional: Enable OTA for Later Uploads:**
    *   Flash the sketch once over serial.
    *   Make sure the board is using an OTA-capable partition scheme. `Huge APP (3MB No OTA)` will boot and upload snapshots, but OTA will fail with `OTA error[1]`.
    *   Keep deep sleep disabled while you are iterating on the sketch, or leave a boot OTA window if you need deep sleep.
    *   Set `ENABLE_OTA = true`.
    *   After boot, wait for the serial message `OTA ready on ...`.
    *   Build the sketch and upload later binaries with the ESP32 OTA helper:
    ```bash
    python3 ~/.arduino15/packages/esp32/hardware/esp32/3.3.7/tools/espota.py \
      -i <device_ip> \
      -p 3232 \
      -f /path/to/esp32cam_snapshot_upload.ino.bin \
      -a <ota_password_if_set>
    ```

10. **WSL2 NAT Note (Windows Hosts):**
    If the server runs inside WSL2 in NAT mode, the ESP32-CAM cannot usually reach the WSL IP directly from your LAN.
    In that case, point `SERVER_URL` at the Windows host IP and add a Windows port forward to the current WSL IP:
    ```powershell
    netsh interface portproxy add v4tov4 listenaddress=0.0.0.0 listenport=8080 connectaddress=<wsl_ip> connectport=8080
    netsh advfirewall firewall add rule name="ESP32 WSL 8080" dir=in action=allow protocol=TCP localport=8080
    ```
    Find `<wsl_ip>` with `hostname -I` or `ip addr show` inside WSL.
    If WSL is restarted later, re-check the IP and update the rule.
    Then verify from Windows that `http://<windows_lan_ip>:8080/healthz` returns `{"status":"ok"}` before blaming the ESP32-CAM sketch.

## Server-Side Person Detection

This project keeps inference on the server, not on the ESP32-CAM.
The first implementation targets `person` detection only and is designed so that the same code path can run on a local PC now and on Raspberry Pi 4 / CPU-only later.

1.  Install the optional ML extra:
    ```bash
    pip install -e ".[dev,ml]"
    ```
2.  Download or place a TFLite object detection model and labels file on disk.
3.  Enable the detector:
    ```bash
    export ESP32CAM_DETECTOR=tflite_cpu
    export ESP32CAM_MODEL_PATH=/absolute/path/to/detect.tflite
    export ESP32CAM_LABELS_PATH=/absolute/path/to/labels.txt
    export ESP32CAM_PERSON_LABEL=person
    export ESP32CAM_PERSON_CLASS_ID=0
    ```
4.  Start the server and verify that new uploads transition from `pending` to `succeeded` on the dashboard or camera detail page.

If `ESP32CAM_DETECTOR=disabled`, uploads still succeed and inference is recorded as `skipped`. If a detector is requested but misconfigured, such as a missing TFLite model path, inference is recorded as `failed` so configuration errors remain visible.

## Verify the Server Locally

Before connecting real hardware, you can test the upload path with `curl`:

```bash
curl -X POST \
  -H "Content-Type: image/jpeg" \
  -H "X-Device-Id: esp32cam-local-test" \
  -H "X-Captured-At: 2026-03-11T12:34:56Z" \
  --data-binary @sample.jpg \
  http://127.0.0.1:8080/upload
```

When this succeeds, verify that:

* the response JSON includes `device_id`, `captured_at`, and `stored_at`
* the `captured_at` value is normalized to UTC in the response
* the uploaded image appears in `GET /files`

Then flash the ESP32-CAM example and watch the serial monitor for:

* Wi-Fi connection status
* NTP sync status
* captured JPEG size
* HTTP status and response body
