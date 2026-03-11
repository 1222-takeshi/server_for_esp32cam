# ESP32-CAM Snapshot Server

This project provides a **snapshot-first central server** for ESP32-CAM devices.
The current milestone is intentionally narrow:

* Receive JPEG snapshots over HTTP from a single ESP32-CAM.
* Store uploaded images on a central server.
* View stored images from a browser.
* Keep the server structure small but ready for future storage backends such as NAS or Google Drive.

The primary operating model is **low-power ESP32-CAM firmware** that wakes up, captures a snapshot, uploads it, and returns to deep sleep.
Continuous video, multi-camera orchestration, ML processing, and remote I/O control are treated as later phases.

## Current Functionality

* **Snapshot Upload**
    * `POST /upload` accepts raw JPEG bytes plus `X-Device-Id` and `X-Captured-At` headers.
    * Uploaded files are stored by the configured storage backend.
* **Browser Access**
    * `GET /` shows the latest uploaded image and quick links.
    * `GET /files` lists stored images in reverse chronological order.
    * `GET /files/<filename>` serves an individual uploaded image.
* **Operational Readiness**
    * `GET /healthz` provides a lightweight liveness endpoint.
    * The app uses an application factory and a storage abstraction so local disk can be replaced later.

## Planned Next Steps

* Add alternative storage backends behind the same server API:
    * NAS
    * Google Drive
* Expand camera-aware browsing and storage organization once the single-camera path is stable.
* Evaluate short clip upload as a later phase.
* Revisit ML pipelines only after the snapshot and storage path are stable.

For detailed information on setup, usage, troubleshooting, and future development, please refer to the `docs` directory.

*   [Requirements](docs/requirements.md)
*   [Setup and Installation](docs/setup.md)
*   [Running the Project](docs/running.md)
*   [Usage](docs/usage.md)
*   [Troubleshooting](docs/troubleshooting.md)
*   [Contributing](docs/contributing.md)
*   [Future Development / Roadmap](docs/roadmap.md)
*   [License](docs/license.md)
*   [Acknowledgments](docs/acknowledgments.md)
