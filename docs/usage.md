# Usage

This project provides a web server with the following functionalities:

### Server Endpoints:

*   **`GET /`**
    *   **Description:** Displays the multi-camera dashboard page with latest snapshots, camera health, and detection status.
    *   **Usage:** Open `http://<server_ip>:8080/` in a web browser.
    *   **Response:** HTML page with camera cards, image count, and quick links.

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
    *   **Notes:** The server also registers camera/snapshot metadata and enqueues asynchronous inference. Upload success does not wait for inference completion.

*   **`GET /cameras`**
    *   **Description:** Displays the HTML camera list page.
    *   **Usage:** Open `http://<server_ip>:8080/cameras`.
    *   **Response:** HTML page with camera names, health status, and links to detail pages.

*   **`GET /cameras/<camera_id>`**
    *   **Description:** Displays one camera's detail page with recent snapshots and person detections.
    *   **Usage:** Open `http://<server_ip>:8080/cameras/<camera_id>`.
    *   **Response:** HTML page with latest image, overlay boxes, recent snapshots, and inference status.

*   **`GET /api/cameras`**
    *   **Description:** Returns camera summaries as JSON.
    *   **Response:** `{"cameras": [...]}`

*   **`GET /api/cameras/<camera_id>/snapshots`**
    *   **Description:** Returns recent snapshots for one camera as JSON.
    *   **Pagination:** Use the `next_before` value from a response as `before` on the next request to avoid deep `offset` scans.
    *   **Response:** `{"camera_id": "<camera_id>", "snapshots": [...]}`

*   **`GET /api/detections`**
    *   **Description:** Returns recent detections as JSON. Supports `camera_id` and `since` query parameters.
    *   **Pagination:** Use the opaque `next_before` value from a response as `before` on the next request; do not combine it with `offset`.
    *   **Response:** `{"detections": [...]}`. Each detection includes nullable `track_id`; when tracking is enabled, the same camera-local ID identifies a matched object across snapshots.

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
    *   Shows a dashboard for known cameras.
    *   Displays each camera's latest snapshot, last-seen timestamp, health state, and inference result.
    *   Provides links to camera detail pages, files, and health check.

2.  **Uploaded Files Page (`/files`):**
    *   Lists all images that have been successfully uploaded to the server.
    *   Shows stored `device_id`, `captured_at`, `stored_at`, and `inference_status` metadata for each image when available.
    *   Each filename is a link. Clicking it will display the corresponding image.

3.  **Camera Detail Page (`/cameras/<camera_id>`):**
    *   Shows the latest snapshot for one camera.
    *   Overlays person-detection boxes on the latest image when detections are available.
    *   Lists recent snapshots together with inference status and detection counts.

### Example Workflow (ESP32-CAM to Server):

1.  **ESP32-CAM:** Captures an image.
2.  **ESP32-CAM:** Connects to Wi-Fi.
3.  **ESP32-CAM:** Sends the JPEG bytes via an HTTP POST request to `http://<server_ip>:8080/upload` with `X-Device-Id` and `X-Captured-At` headers.
4.  **Server:** Receives the data, stores it, registers camera/snapshot metadata, and responds with a success message plus metadata.
5.  **Server:** Runs person detection asynchronously for the stored snapshot.
6.  **User:** Opens `http://<server_ip>:8080/` or `http://<server_ip>:8080/cameras/<camera_id>` in a browser to inspect camera state and detections.
