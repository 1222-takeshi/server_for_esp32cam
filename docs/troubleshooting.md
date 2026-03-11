# Troubleshooting

Here are some common issues you might encounter and how to resolve them:

### Server-Side Issues:

*   **Problem:** Server doesn't start, or `ImportError: No module named flask`.
    *   **Solution:** Ensure Flask is installed in your Python environment. If using a virtual environment, make sure it's activated. Run `pip install Flask`.

*   **Problem:** `OSError: [Errno 98] Address already in use` (or similar).
    *   **Solution:** Port 8080 is already being used by another application.
        *   Stop the other application using the port.
        *   Or, modify `server.py` to use a different port: change `app.run(host='0.0.0.0', port=8080)` to `app.run(host='0.0.0.0', port=<new_port_number>)` and update the client-side accordingly.

*   **Problem:** Server runs, but client (e.g., ESP32-CAM) cannot connect.
    *   **Check:**
        *   **Firewall:** Ensure your server machine's firewall is not blocking incoming connections on port 8080 (or the port you are using).
        *   **IP Address:** Verify you are using the correct IP address of the server machine in your client's configuration. Use the server's local network IP (e.g., `192.168.1.x`), not `127.0.0.1` or `localhost` if the client is a separate device.
        *   **Network:** Ensure the server and client are on the same Wi-Fi network.
        *   **Server Logs:** Check the `server.py` console output for any error messages when the client attempts to connect or upload.

*   **Problem:** Files are uploaded, but images are corrupted or not what was expected.
    *   **Check:**
        *   **Client-Side Code:** Ensure the client is sending the image data correctly (e.g., correct image format, complete data).
        *   **Required Headers:** `POST /upload` requires `Content-Type: image/jpeg`, `X-Device-Id`, and `X-Captured-At` on every request. Missing metadata headers are rejected with `400 Missing device metadata`.
        *   **Timestamp Format:** `X-Captured-At` must be an ISO 8601 timestamp with an explicit timezone, for example `2026-03-11T12:34:56Z` or `2026-03-11T21:34:56+09:00`. Timezone-naive values are rejected with `400 Invalid captured_at metadata`, and non-UTC offsets are normalized to UTC in the JSON response.
        *   **JPEG Validation:** The server rejects non-`image/jpeg` uploads with `415 Unsupported content type` and rejects payloads that do not look like JPEG bytes with `400 Invalid JPEG payload`.
        *   **File Naming/Saving:** The server stores accepted uploads with generated names such as `image_<timestamp>_<uuid>.jpg`, so use the returned `filename` instead of assuming a client-side name.

*   **Problem:** Uploads fail with `Missing device metadata` or `Invalid captured_at metadata`.
    *   **Check:**
        *   **Header Names:** Verify the request includes the exact header names `X-Device-Id` and `X-Captured-At`.
        *   **Device ID Value:** Ensure `X-Device-Id` is not blank after trimming whitespace.
        *   **Timezone-Aware Timestamp:** Ensure `X-Captured-At` includes `Z` or an offset such as `+09:00`. A value like `2026-03-11T12:34:56` is invalid because it omits the timezone.
        *   **Quick Reproduction:** Test the server with `curl` first so you can confirm the headers independently of the ESP32-CAM firmware.

*   **Problem:** `uploads` directory not found or permission issues.
    *   **Solution:** The `server.py` script attempts to create the `uploads` directory if it doesn't exist. If it fails due to permissions, create the `uploads` directory manually in the project's root folder and ensure the user running the script has write permissions to it.

### Client-Side Issues (ESP32-CAM Example):

*   **Problem:** ESP32-CAM cannot connect to Wi-Fi.
    *   **Check:**
        *   **Credentials:** Double-check Wi-Fi SSID and password in your ESP32-CAM sketch.
        *   **Signal Strength:** Ensure the ESP32-CAM has a good Wi-Fi signal.
        *   **Router Configuration:** Some routers might have MAC filtering or other security settings that prevent new devices from connecting.

*   **Problem:** ESP32-CAM connects to Wi-Fi but fails to send image to server.
    *   **Check:**
        *   **Server IP and Port:** Verify the server IP address and port in the ESP32-CAM sketch are correct and match the running server.
        *   **HTTP POST Request Format:** Ensure the sketch targets `/upload`, sends raw JPEG bytes in the body, and includes `Content-Type: image/jpeg`, `X-Device-Id`, and timezone-aware `X-Captured-At` headers.
        *   **Serial Monitor:** Use the Arduino IDE's Serial Monitor to check for error messages or debug output from your ESP32-CAM sketch. This is crucial for diagnosing client-side issues.
        *   **Server Logs:** Simultaneously check the `server.py` console output for any messages when the ESP32-CAM attempts to upload.

*   **Problem:** Image capture fails on ESP32-CAM.
    *   **Check:**
        *   **Camera Model:** Ensure you've selected the correct camera model in your ESP32-CAM sketch (e.g., `CAMERA_MODEL_AI_THINKER`).
        *   **Camera Connections:** Verify the camera module is properly connected to the ESP32 board.
        *   **Power Supply:** ESP32-CAM can be power-hungry, especially with Wi-Fi active. Ensure it has a stable and sufficient power supply (e.g., 5V, at least 2A recommended for some setups). Insufficient power can lead to crashes or brownouts.
        *   **GPIO Pins:** If you're using a board that requires specific GPIOs for camera or SD card, ensure they are correctly configured and not conflicting.

*   **Consult Official Resources:** For issues related to the ESP32 camera driver, camera initialization, or specific error codes from the camera component, check the [official esp32-camera repository issues](https://github.com/espressif/esp32-camera/issues) and documentation.

### General Tips:

*   **Start Simple:** If you're having trouble, try to isolate the problem. Test server functionality with a tool like `curl` or Postman first. Test client Wi-Fi connectivity separately.
*   **Check Logs:** Always check the console output of `server.py` and the serial monitor output of your client device (if applicable) for error messages.
*   **Incrementally Develop:** When writing client code (like for ESP32-CAM), add features step-by-step (connect to Wi-Fi, then try a simple GET request, then try POSTing data).
