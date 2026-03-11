# Future Development / Roadmap

This project is currently in its first practical milestone:

* single ESP32-CAM
* JPEG snapshot upload
* central server storage
* browser-based browsing

Future work should extend this base without weakening the snapshot-first path.

### 1. Stabilize the Single-Camera Snapshot Path

*   Strengthen upload validation and storage behavior.
*   Keep the browser UI simple and reliable.
*   Improve low-power snapshot workflows for deep sleep ESP32-CAM firmware.

### 2. Add Storage Backend Flexibility

*   Keep ESP32-CAM uploads going to one central server API.
*   Add server-side backend choices such as:
    * local disk
    * NAS
    * Google Drive

### 3. Introduce Multi-Camera Identification

*   Add camera IDs only after the single-camera path is stable.
*   Organize saved assets per camera.
*   Expand the UI to distinguish image sources clearly.

### 4. Evaluate Short Video Clips

*   Avoid continuous video as the initial default.
*   Consider short clip upload only if snapshot workflows are insufficient.
*   Keep low-power operation as a design constraint.

### 5. Revisit Advanced Features Later

*   ML processing
*   alerts and notifications
*   remote I/O control
*   richer automation pipelines

These features should be added only after the snapshot and storage path is dependable.
