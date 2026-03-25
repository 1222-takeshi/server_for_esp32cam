# Running the Project

Currently, this project provides a server to receive and display JPEG snapshots from a single ESP32-CAM class device.
The current milestone intentionally focuses on **single-camera snapshot upload**. Multi-camera management, ML-based recognition, and remote I/O control remain future work.
The repository also includes a minimal ESP32-CAM example under `examples/` so you can verify the upload contract without writing a sketch from scratch.

### 1. Run the Server:

*   Navigate to the directory containing the `script` folder in your terminal.
*   If you created a virtual environment, make sure it's activated.
    ```bash
    # On Windows
    # venv\Scripts\activate
    # On macOS/Linux
    # source venv/bin/activate
    ```
*   Run the Flask server:
    ```bash
    python script/server.py
    ```
*   By default, the server will start on `http://0.0.0.0:8080/`.
*   You will see output in your terminal indicating the server is running, similar to:
    ```
     * Serving Flask app 'server'
     * Debug mode: off
     * Running on all addresses (0.0.0.0)
     * Running on http://127.0.0.1:8080
     * Running on http://<your_local_ip>:8080
    Press CTRL+C to quit
    ```
    Note down `<your_local_ip>` if you are accessing the server from another device on the same network (like an ESP32-CAM).

### 2. Configure and Run the Client (ESP32-CAM):

*   Start from `examples/esp32cam_snapshot_upload/esp32cam_snapshot_upload.ino`.
*   Ensure the ESP32-CAM is programmed to:
    *   Connect to the same Wi-Fi network as the server.
    *   Optionally expose an OTA window before capturing when you are iterating on the sketch.
    *   Synchronize time before taking the snapshot.
    *   Capture JPEG snapshots.
    *   Send each snapshot as an HTTP POST request to `http://<server_ip_address>:8080/upload`.
    *   Include `Content-Type: image/jpeg`, `X-Device-Id`, and timezone-aware `X-Captured-At` headers on every upload.
    *   Optionally return to deep sleep after the upload cycle.
*   If you use the in-repo example, change only the Wi-Fi credentials, server IP, and device identifier first. Keep the header names and raw JPEG POST body unchanged.
*   The in-repo example keeps deep sleep disabled by default so you can inspect the serial logs first. OTA is also disabled by default and should be turned on only when you want network uploads after the first serial flash.
*   The example initializes Wi-Fi and NTP before the camera, and keeps camera defaults conservative (`VGA` with PSRAM, `QVGA` without PSRAM, `fb_count = 1`) because that proved more stable on real hardware.
*   Power on and run the ESP32-CAM.

### 3. Accessing Uploaded Images:

*   Once images are uploaded, you can view them by navigating to:
    `http://<server_ip_address>:8080/files`
*   This page will list all successfully uploaded image files. Clicking on a filename will display the image.
*   Uploaded images are stored in the configured upload directory (`uploads/` by default).
*   The latest upload metadata should also appear on `/`.

### 4. Health Check:

*   To confirm the server process is alive:
    `http://<server_ip_address>:8080/healthz`

### WSL2 NAT Note

If the server runs inside WSL2 in NAT mode, the ESP32-CAM usually cannot reach the WSL IP directly from your LAN.
Expose the server through the Windows host IP instead, and forward the port to the current WSL IP:

```powershell
netsh interface portproxy add v4tov4 listenaddress=0.0.0.0 listenport=8080 connectaddress=<wsl_ip> connectport=8080
netsh advfirewall firewall add rule name="ESP32 WSL 8080" dir=in action=allow protocol=TCP localport=8080
```

Find `<wsl_ip>` with `hostname -I` or `ip addr show` inside WSL.
Then point the ESP32-CAM at `http://<windows_host_ip>:8080/upload` and verify `http://<windows_host_ip>:8080/healthz` from Windows before testing the board again.
If WSL is restarted, re-check the IP and update the rule.

### OTA Update Flow

After the first serial flash, the in-repo example can accept OTA updates:

*   Watch for `OTA ready on ...` in the serial monitor.
*   Set `ENABLE_OTA = true` and choose an `OTA_PASSWORD` before relying on OTA.
*   Upload from Arduino IDE using the network port, or use `espota.py`:
    ```bash
    python3 ~/.arduino15/packages/esp32/hardware/esp32/3.3.7/tools/espota.py \
      -i <device_ip> \
      -p 3232 \
      -f /path/to/esp32cam_snapshot_upload.ino.bin \
      -a <ota_password_if_set>
    ```
*   If you want a stable mDNS name, set `OTA_HOSTNAME`.
*   If the board should sleep immediately in production, disable OTA or shorten `OTA_WAIT_WINDOW_MS`.

### Future Enhancements:

Later phases may add:
*   camera-aware browsing and storage organization
*   storage backend switching (NAS / Google Drive)
*   short video clips
*   ML processing

### Local Verification Checklist:

Before testing on hardware, confirm the upload contract with:

```bash
curl -X POST \
  -H "Content-Type: image/jpeg" \
  -H "X-Device-Id: esp32cam-local-test" \
  -H "X-Captured-At: 2026-03-11T12:34:56Z" \
  --data-binary @sample.jpg \
  http://127.0.0.1:8080/upload
```

Then check `GET /` and `GET /files` to confirm that the new image and metadata are visible.
