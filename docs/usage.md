# Usage

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
