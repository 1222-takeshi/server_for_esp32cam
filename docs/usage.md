# Usage

This project provides a web server with the following functionalities:

### Server Endpoints:

*   **`GET /`**
    *   **Description:** Displays the main HTML page with the latest uploaded image.
    *   **Usage:** Open `http://<server_ip>:8080/` in a web browser.
    *   **Response:** HTML page with latest image preview, image count, and quick links.

*   **`POST /upload`**
    *   **Description:** Endpoint for uploading JPEG images. The server expects raw image data in the request body and acts as the central ingress point even when storage backends change later.
    *   **Usage (Programmatic, e.g., from ESP32-CAM or a script):**
        Send an HTTP POST request with the image data in the body.
        *   **Method:** `POST`
        *   **URL:** `http://<server_ip>:8080/upload`
        *   **Headers:** `Content-Type: image/jpeg`, `X-Device-Id: <camera_name>`, `X-Captured-At: <ISO-8601-with-timezone>`
        *   **Body:** Raw JPEG binary data.
    *   **Response (Success):**
        *   **Status Code:** `200 OK`
        *   **Body (JSON):** `{"message": "File uploaded successfully", "filename": "<saved_name>", "size": <bytes>, "device_id": "<camera_name>", "captured_at": "<UTC timestamp>", "stored_at": "<UTC timestamp>"}`
    *   **Response (Failure):**
        *   **Status Code:** `400 Bad Request` for empty data, missing device metadata, invalid metadata, or invalid JPEG payload
        *   **Status Code:** `413 Payload Too Large` for uploads beyond the configured size limit
        *   **Status Code:** `415 Unsupported Media Type` for non-JPEG uploads
        *   **Status Code:** `500 Internal Server Error` for storage failures
    *   **Notes:** Uploaded files are saved with unique server-generated filenames. `X-Captured-At` must be timezone-aware ISO 8601, for example `2026-03-11T12:34:56Z`. The server normalizes `captured_at` to UTC in its JSON response, and `stored_at` is the server-side persistence time in UTC.

*   **`GET /files`**
    *   **Description:** Displays an HTML page (`templates/files.html`) listing stored files in reverse chronological order together with stored device metadata when available.
    *   **Usage:** Open `http://<server_ip>:8080/files` in a web browser.
    *   **Response:** HTML page with clickable filenames plus `device_id`, `captured_at`, and `stored_at` details.

*   **`GET /files/<filename>`**
    *   **Description:** Serves a specific file from the `uploads/` directory.
    *   **Usage:**
        *   Typically accessed by clicking a link on the `/files` page.
        *   Or directly via `http://<server_ip>:8080/files/<filename_to_view>` in a web browser.
    *   **Response:** The requested image file.

*   **`GET /healthz`**
    *   **Description:** Lightweight health endpoint.
    *   **Usage:** Open `http://<server_ip>:8080/healthz` or use it from monitoring scripts.
    *   **Response:** `{"status": "ok"}`

### Web Interface:

1.  **Main Page (`/`):**
    *   Shows the latest uploaded image if one exists.
    *   Shows the latest upload's `device_id`, `captured_at`, and `stored_at`.
    *   Displays the current number of stored images.
    *   Provides links to the files list and health check.

2.  **Uploaded Files Page (`/files`):**
    *   Lists all images that have been successfully uploaded to the server.
    *   Shows stored `device_id`, `captured_at`, and `stored_at` metadata for each image when available.
    *   Each filename is a link. Clicking it will display the corresponding image.

### Example Workflow (ESP32-CAM to Server):

1.  **ESP32-CAM:** Captures an image.
2.  **ESP32-CAM:** Connects to Wi-Fi.
3.  **ESP32-CAM:** Sends the JPEG bytes via an HTTP POST request to `http://<server_ip>:8080/upload` with `X-Device-Id` and `X-Captured-At` headers.
4.  **Server:** Receives the data, stores it using the configured storage backend, and responds with a success message plus metadata.
5.  **User:** Opens `http://<server_ip>:8080/files` in a browser to see the list of uploaded images.
6.  **User:** Clicks on an image filename to view it.
