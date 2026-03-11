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

5.  **Directory Structure:**
    The server stores uploaded JPEG files in `uploads/` by default. The directory is created automatically if it does not exist.

6.  **Optional Configuration:**
    You can change runtime behavior with environment variables:
    ```bash
    export PORT=8080
    export ESP32CAM_UPLOAD_DIR=/absolute/path/to/uploads
    export ESP32CAM_STORAGE_BACKEND=local
    export ESP32CAM_MAX_CONTENT_LENGTH=1048576
    ```

### Client-Side Setup (Example: ESP32-CAM with Arduino IDE):

This is a general guide. Specifics might vary based on your ESP32-CAM board version and the exact code you use.

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

4.  **Install Necessary Libraries:**
    Depending on your ESP32-CAM sketch, you might need to install libraries for:
    *   Wi-Fi (`WiFi.h`)
    *   HTTP Client (`HTTPClient.h`)
    *   Camera (`esp_camera.h`)
    You can install libraries via `Sketch > Include Library > Manage Libraries...`.

5.  **Refer to Official ESP32 Camera Documentation:**
    For detailed information on camera initialization, configuration, and advanced usage, the official `esp32-camera` driver repository is an invaluable resource:
    *   [espressif/esp32-camera on GitHub](https://github.com/espressif/esp32-camera)
    This repository contains the camera driver source code, examples, and further documentation that can be very helpful for developing robust client-side firmware.

6.  **Load ESP32-CAM Sketch:**
    *   You'll need an Arduino sketch for your ESP32-CAM that:
        1.  Initializes the camera.
        2.  Connects to your Wi-Fi network.
        3.  Captures an image.
        4.  Sends the image data as a JPEG via an HTTP POST request to the server's central `/upload` endpoint (e.g., `http://<your_server_ip>:8080/upload`).
        5.  Returns to deep sleep if low-power operation is desired.
    *   There are many example sketches available online for sending images from ESP32-CAM to a server. You'll need to adapt one to point to your server's IP address and port.

    **Example Snippet (Conceptual - for sending image data):**
    ```c++
    // ... (WiFi connection and camera setup) ...

    #include <HTTPClient.h>

    void sendPhotoToServer(uint8_t *imageData, size_t imageLen) {
      HTTPClient http;
      // Replace <your_server_ip> with the actual IP of the machine running server.py
      http.begin("http://<your_server_ip>:8080/upload");
      http.addHeader("Content-Type", "image/jpeg");

      int httpResponseCode = http.POST(imageData, imageLen);

      if (httpResponseCode > 0) {
        String response = http.getString();
        Serial.println(httpResponseCode);
        Serial.println(response);
      } else {
        Serial.print("Error on sending POST: ");
        Serial.println(httpResponseCode);
      }
      http.end();
    }

    // In your main loop or a function:
    // camera_fb_t * fb = esp_camera_fb_get();
    // if (fb) {
    //   sendPhotoToServer(fb->buf, fb->len);
    //   esp_camera_fb_return(fb);
    // }
    ```

7.  **Upload Sketch to ESP32-CAM:**
    Connect your ESP32-CAM to your computer (usually via a USB-to-serial adapter, ensuring it's in flashing mode) and upload the sketch.

## Verify the Server Locally

Before connecting real hardware, you can test the upload path with `curl`:

```bash
curl -X POST \
  -H "Content-Type: image/jpeg" \
  --data-binary @sample.jpg \
  http://127.0.0.1:8080/upload
```
