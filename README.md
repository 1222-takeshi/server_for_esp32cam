# ESP32 Multi-Camera System with ML and I/O Control Potential

This project aims to build a versatile system capable of displaying images from multiple ESP32 cameras on a central web server. Beyond image display, it is envisioned to integrate Machine Learning-based object recognition and allow for controlling motors or other I/O devices connected to the ESP32 cameras.

Currently, the server supports receiving and displaying images from a single source. Future development will focus on expanding this to handle multiple camera feeds, implement ML processing pipelines, and establish communication channels for I/O control.

**Core Functionality (Current & Planned):**

*   **Image Aggregation:**
    *   Receive and store images uploaded from ESP32-CAM devices (currently single, planned for multiple).
    *   Provide a web interface to list and view uploaded images.
*   **Multiple Camera Support (Planned):**
    *   Mechanisms to differentiate and manage feeds from various ESP32 cameras.
    *   User interface enhancements for displaying multiple camera views.
*   **Machine Learning Integration (Planned):**
    *   Server-side processing of images/streams using OpenCV or other ML frameworks for tasks like object detection.
    *   Displaying recognition results alongside camera feeds.
*   **Remote I/O Control (Planned):**
    *   Interface to send control signals from the server to ESP32 devices for actuating motors or other peripherals.

## Hardware Requirements

This server application itself runs on a standard computer or server environment (Windows, Linux, macOS).

To utilize the image upload functionality as intended (e.g., for a remote camera setup), you will typically need:

*   **An image capturing device:**
    *   **ESP32-CAM:** This is a common choice, as suggested by the project's context. The device should be programmed to capture images and send them to this server's `/upload` endpoint via HTTP POST request.
    *   **Other Microcontrollers with Camera and Wi-Fi:** Any microcontroller capable of capturing images and making HTTP requests (e.g., Raspberry Pi with a camera module, other ESP32 variants).
    *   **Smartphones or Computers:** Can also be used to send images for testing purposes.
*   **Network Connectivity:** The image capturing device and the server running this application must be on the same network or the server must be accessible from the device (e.g., via the internet with proper port forwarding).
    *   **For multiple cameras:** Ensure your network has sufficient bandwidth to handle multiple simultaneous image streams. The server's processing power and storage capacity should also be considered for a multi-camera setup.

## Software Requirements

### Server-Side:

*   **Python:** The server script (`server.py`) is written in Python. Python 3.x is recommended.
*   **Flask:** A micro web framework for Python, used to build the web application. You can install it using pip:
    ```bash
    pip install Flask
    ```
*   **Operating System:** A standard operating system that can run Python (e.g., Linux, macOS, Windows).

### Client-Side (e.g., ESP32-CAM):

*   **For ESP32-CAM:**
    *   **Arduino IDE** or **PlatformIO IDE:** For programming the ESP32-CAM.
    *   **ESP32 Board Support Package:** To be installed in the Arduino IDE or PlatformIO.
    *   **Relevant Libraries:** Depending on the specific code used on the ESP32-CAM, libraries for camera operation (e.g., `esp_camera.h`), Wi-Fi connectivity (e.g., `WiFi.h`), and HTTP client functionality (e.g., `HTTPClient.h` or a custom implementation) will be needed.
*   **For Other Devices:**
    *   Appropriate development environment and libraries for capturing images and making HTTP POST requests.

### Optional (For Future Enhancements like ML and I/O Control):

*   **OpenCV:** If image processing and object detection features are to be implemented, OpenCV (Python version `opencv-python`) would be required on the server-side.
    ```bash
    pip install opencv-python
    ```
*   **TensorFlow/PyTorch (or other ML frameworks):** For more advanced object detection models.
*   **MQTT Client Library (e.g., Paho-MQTT):** If using MQTT for bi-directional communication with ESP32 devices for I/O control or sensor data.
    ```bash
    pip install paho-mqtt
    ```
*   **Other relevant Python libraries:** Depending on the chosen methods for I/O control, sensor data handling, or specific ML tasks.

## Setup and Installation

### Server-Side Setup:

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
    Install Flask (and any other Python dependencies if added later):
    ```bash
    pip install Flask
    ```
    If a `requirements.txt` file is provided in the future, you can install all dependencies with:
    ```bash
    pip install -r requirements.txt
    ```

5.  **Directory Structure:**
    The server script (`script/server.py`) expects a directory named `uploads` in the project's base directory (where `script/` and `templates/` are located) to store uploaded images. It will be created automatically if it doesn't exist when the server starts.

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
        4.  Sends the image data (e.g., as a JPEG) via an HTTP POST request to the server's `/upload` endpoint (e.g., `http://<your_server_ip>:8080/upload`).
    *   There are many example sketches available online for sending images from ESP32-CAM to a server. You'll need to adapt one to point to your server's IP address and port.

    **Example Snippet (Conceptual - for sending image data):**
    ```c++
    // ... (WiFi connection and camera setup) ...

    #include <HTTPClient.h>

    void sendPhotoToServer(uint8_t *imageData, size_t imageLen) {
      HTTPClient http;
      // Replace <your_server_ip> with the actual IP of the machine running server.py
      http.begin("http://<your_server_ip>:8080/upload");
      http.addHeader("Content-Type", "image/jpeg"); // Or appropriate content type

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

## Running the Project

Currently, this project provides a server to receive and display images primarily from a single ESP32-CAM like device. The functionalities for multiple camera management, ML-based object recognition, and remote I/O control described in the "Future Development / Roadmap" section are planned enhancements.

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
*   By default, the server will start on `http://0.0.0.0:8080/`. This means it will be accessible from any IP address on your network on port 8080.
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

### 2. Configure and Run the Client (e.g., ESP32-CAM):

*   Ensure your client device (e.g., ESP32-CAM) is programmed to:
    *   Connect to the same Wi-Fi network as the server.
    *   Capture images.
    *   Send these images as HTTP POST requests to the server's `/upload` endpoint. The URL will be `http://<server_ip_address>:8080/upload`, where `<server_ip_address>` is the IP address of the machine running `server.py`.
*   Power on and run your ESP32-CAM or other client device.

### 3. Accessing Uploaded Images:

*   Once images are uploaded from the client, you can view them by navigating to the following URL in your web browser:
    `http://<server_ip_address>:8080/files`
*   This page will list all successfully uploaded image files. Clicking on a filename will display the image.
*   Uploaded images are stored in the `uploads` directory in the project's root folder on the server.

### Future Enhancements (Multi-camera, ML, I/O Control):

As features outlined in the "Future Development / Roadmap" (like multi-camera feeds, ML processing, I/O control) are implemented:
*   The server (`server.py`) will be significantly enhanced, or new components/scripts might be introduced.
*   Client-side (ESP32) firmware will require corresponding updates to support new functionalities (e.g., sending camera IDs, receiving control commands).
*   Detailed instructions for setting up and running these advanced features will be added to this README in their respective sections or new dedicated guides.

## Usage

This project provides a web server with the following functionalities:

### Server Endpoints:

*   **`GET /`**
    *   **Description:** Displays the main HTML page (`templates/index.html`).
    *   **Usage:** Open `http://<server_ip>:8080/` in a web browser.
    *   **Response:** HTML page with a link to view uploaded files.

*   **`POST /upload`**
    *   **Description:** Endpoint for uploading image files. The server expects raw image data in the request body.
    *   **Usage (Programmatic, e.g., from ESP32-CAM or a script):**
        Send an HTTP POST request with the image data in the body.
        *   **Method:** `POST`
        *   **URL:** `http://<server_ip>:8080/upload`
        *   **Headers (Example):** `Content-Type: image/jpeg` (or `image/png`, etc., depending on the image format)
        *   **Body:** Raw binary data of the image.
    *   **Response (Success):**
        *   **Status Code:** `200 OK`
        *   **Body (JSON):** `{"message": "File uploaded successfully"}`
    *   **Response (Failure):**
        *   **Status Code:** `400 Bad Request` (e.g., no data) or `500 Internal Server Error`.
        *   **Body (JSON):** `{"message": "File upload failed", "error": "<error_details>"}`
    *   **Notes:** Uploaded files are saved in the `uploads/` directory on the server with a filename like `image_<timestamp>.jpg`.

*   **`GET /files`**
    *   **Description:** Displays an HTML page (`templates/files.html`) listing all files currently in the `uploads/` directory.
    *   **Usage:** Open `http://<server_ip>:8080/files` in a web browser.
    *   **Response:** HTML page with a list of clickable filenames.

*   **`GET /files/<filename>`**
    *   **Description:** Serves a specific file from the `uploads/` directory.
    *   **Usage:**
        *   Typically accessed by clicking a link on the `/files` page.
        *   Or directly via `http://<server_ip>:8080/files/<filename_to_view>` in a web browser.
    *   **Response:** The requested image file.

### Web Interface:

1.  **Main Page (`/`):**
    *   Provides a starting point with a link: "View Uploaded Files".

2.  **Uploaded Files Page (`/files`):**
    *   Lists all images that have been successfully uploaded to the server.
    *   Each filename is a link. Clicking it will display the corresponding image.

### Example Workflow (ESP32-CAM to Server):

1.  **ESP32-CAM:** Captures an image.
2.  **ESP32-CAM:** Connects to Wi-Fi.
3.  **ESP32-CAM:** Sends the image data via an HTTP POST request to `http://<server_ip>:8080/upload`.
4.  **Server:** Receives the data, saves it as `uploads/image_<timestamp>.jpg`, and responds with a success message.
5.  **User:** Opens `http://<server_ip>:8080/files` in a browser to see the list of uploaded images.
6.  **User:** Clicks on an image filename to view it.

## Troubleshooting

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
        *   **Content-Type Header:** While `server.py` currently doesn't strictly enforce `Content-Type` for saving, ensure the client is sending an appropriate header (e.g., `image/jpeg`) as good practice. The server saves the raw body, so the client must send valid image data.
        *   **File Naming/Saving:** The server saves files as `image_<timestamp>.jpg`. If the client sends non-JPEG data, the file extension will still be `.jpg`, but the content might be incorrect.

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
        *   **HTTP POST Request Format:** Ensure the sketch is forming the HTTP POST request correctly (correct endpoint `/upload`, correct headers, and the image data in the body).
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

## Contributing

Contributions to this project are welcome! If you'd like to contribute, please follow these general guidelines:

1.  **Fork the Repository:**
    Start by forking the main repository to your own GitHub account.

2.  **Create a Branch:**
    Create a new branch in your forked repository for your feature or bug fix. Use a descriptive branch name, e.g., `feat/add-image-filtering` or `fix/upload-error-handling`.
    ```bash
    git checkout -b feat/your-new-feature
    ```

3.  **Make Your Changes:**
    Implement your feature or bug fix.
    *   Ensure your code follows the existing style and structure.
    *   If you add new features, consider adding relevant documentation or examples.
    *   If you add new Python dependencies, update `requirements.txt` (if one exists, or consider creating one).

4.  **Test Your Changes:**
    Thoroughly test your changes to ensure they work as expected and do not introduce new issues.
    *   If applicable, write unit tests for new functionality.

5.  **Commit Your Changes:**
    Commit your changes with clear and concise commit messages.
    ```bash
    git add .
    git commit -m "feat: Add feature X"
    # or "fix: Resolve bug Y"
    ```

6.  **Push to Your Fork:**
    Push your changes to your forked repository.
    ```bash
    git push origin feat/your-new-feature
    ```

7.  **Open a Pull Request:**
    Go to the original repository and open a Pull Request (PR) from your forked branch to the `main` branch of the original repository.
    *   Provide a clear description of the changes in your PR.
    *   Reference any relevant issues (e.g., "Closes #123").

8.  **Code Review:**
    Your PR will be reviewed, and feedback may be provided. Be prepared to make further changes if requested.

By contributing, you agree that your contributions will be licensed under the project's license.

## Future Development / Roadmap

This project is currently in its initial phase, focusing on basic image upload from a single client. The long-term vision includes evolving this into a comprehensive multi-camera management system with advanced capabilities. Key areas planned for future development include:

### 1. Enhanced Multi-Camera Support:

*   **Camera Identification & Management:**
    *   Implement a system for uniquely identifying each ESP32-CAM client (e.g., by MAC address, configurable ID).
    *   Server-side logic to handle and differentiate streams/images from multiple identified cameras.
*   **Dynamic Web Interface:**
    *   Develop a web UI that can display feeds from multiple cameras simultaneously (e.g., grid view, selectable feeds).
    *   Allow users to label or name individual camera feeds.
*   **Scalable Storage:**
    *   Organize stored images in a way that's easily attributable to specific cameras (e.g., subdirectories per camera ID).

### 2. Machine Learning Integration:

*   **Server-Side Object Detection/Recognition:**
    *   Integrate OpenCV for basic image processing tasks (e.g., motion detection, face detection).
    *   Explore using more advanced ML frameworks (TensorFlow, PyTorch) with pre-trained or custom models for object recognition (e.g., identifying common objects, people, specific items).
    *   Develop a pipeline for processing incoming images/streams and storing/displaying results.
*   **Real-time Alerts & Notifications:**
    *   Based on ML detection results, implement a system for sending alerts (e.g., email, web notifications) for specific events.
*   **Data Annotation & Training (Long-term):**
    *   Consider features for collecting and annotating images to train custom ML models.

### 3. Remote I/O Control and Sensor Integration:

*   **Bi-directional Communication:**
    *   Establish a robust communication channel (e.g., MQTT, WebSockets, or dedicated HTTP endpoints) for sending commands from the server to ESP32 clients.
*   **Motor Control Interface:**
    *   Develop server-side UI elements and API endpoints to trigger motor movements on connected ESP32 devices (e.g., for pan-tilt camera mounts, robotic components).
    *   ESP32 firmware to interpret commands and actuate motors (e.g., via PWM, motor drivers).
*   **Sensor Data Acquisition:**
    *   Allow ESP32 clients to send sensor data (e.g., temperature, humidity, motion sensors) back to the server.
    *   Display sensor data alongside camera feeds or on a dashboard.

### 4. System Improvements:

*   **Security:** Implement user authentication, secure communication (HTTPS), and authorization for accessing feeds and control functions.
*   **Configuration:** Allow more settings to be configured via UI or configuration files (e.g., camera settings, storage paths, ML model selection).
*   **Performance & Scalability:** Optimize server performance for handling multiple concurrent streams and large volumes of data.
*   **User Interface/User Experience:** Continuously improve the web interface for better usability and presentation of information.
*   **Comprehensive API:** Develop a well-documented API for programmatic interaction with the server.

Contributions in these areas are highly welcome. Please refer to the "Contributing" section for guidelines.

## License

This project is licensed under the MIT License. See the [LICENSE](LICENSE) file for more details.

## Acknowledgments

*   This project uses [Flask](https://flask.palletsprojects.com/), a lightweight WSGI web application framework.
*   Inspiration for working with ESP32-CAM and image servers often comes from the vibrant community of electronics and IoT enthusiasts. (If you have specific tutorials, articles, or individuals you'd like to credit, please add them here!)
*   The [ESP32 Camera Driver by Espressif](https://github.com/espressif/esp32-camera) is a fundamental component for any ESP32-CAM based project.
