# Running the Project

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
