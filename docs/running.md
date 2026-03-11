# Running the Project

Currently, this project provides a server to receive and display JPEG snapshots from a single ESP32-CAM class device.
The current milestone intentionally focuses on **single-camera snapshot upload**. Multi-camera management, ML-based recognition, and remote I/O control remain future work.

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

*   Ensure the ESP32-CAM is programmed to:
    *   Connect to the same Wi-Fi network as the server.
    *   Capture JPEG snapshots.
    *   Send each snapshot as an HTTP POST request to `http://<server_ip_address>:8080/upload`.
    *   Optionally return to deep sleep between captures.
*   Power on and run the ESP32-CAM.

### 3. Accessing Uploaded Images:

*   Once images are uploaded, you can view them by navigating to:
    `http://<server_ip_address>:8080/files`
*   This page will list all successfully uploaded image files. Clicking on a filename will display the image.
*   Uploaded images are stored in the configured upload directory (`uploads/` by default).

### 4. Health Check:

*   To confirm the server process is alive:
    `http://<server_ip_address>:8080/healthz`

### Future Enhancements:

Later phases may add:
*   multiple camera identification
*   storage backend switching (NAS / Google Drive)
*   short video clips
*   ML processing
