# Usage Guide

This guide explains how to run the ESP32 Multi-Camera System server, configure clients, and use its current functionalities. Please note that advanced features like multi-camera management, ML-based object recognition, and remote I/O control are planned enhancements as detailed in the [Project Roadmap](ROADMAP.md).

## Running the Project

Currently, this project provides a server to receive and display images primarily from a single ESP32-CAM like device.

### 1. Run the Server:

*   Navigate to the directory containing the `script` folder in your terminal.
*   If you created a virtual environment (as recommended in the [Setup Guide](SETUP.md)), make sure it's activated:
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
    *   Send these images as HTTP POST requests to the server's `/upload` endpoint. The URL will be `http://<server_ip_address>:8080/upload`, where `<server_ip_address>` is the IP address of the machine running `server.py`. (Refer to the ESP32-CAM setup details in the [Setup Guide](SETUP.md)).
*   Power on and run your ESP32-CAM or other client device.

### 3. Accessing Uploaded Images:

*   Once images are uploaded from the client, you can view them by navigating to the following URL in your web browser:
    `http://<server_ip_address>:8080/files`
*   This page will list all successfully uploaded image files. Clicking on a filename will display the image.
*   Uploaded images are stored in the `uploads` directory in the project's root folder on the server.

### Future Enhancements (Multi-camera, ML, I/O Control):

As features outlined in the [Project Roadmap](ROADMAP.md) (like multi-camera feeds, ML processing, I/O control) are implemented:
*   The server (`server.py`) will be significantly enhanced, or new components/scripts might be introduced.
*   Client-side (ESP32) firmware will require corresponding updates to support new functionalities (e.g., sending camera IDs, receiving control commands).
*   Detailed instructions for setting up and running these advanced features will be added to this document or new dedicated guides.

## Detailed Usage (Current Functionalities)

The web server provides the following functionalities:

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
