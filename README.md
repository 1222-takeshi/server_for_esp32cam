# ESP32-CAM Snapshot Server

This project provides a **snapshot-first central server** for ESP32-CAM devices.
The current milestone now covers:

* Receive JPEG snapshots over HTTP from multiple ESP32-CAM devices.
* Store uploaded images on a central server.
* Track per-camera status and view stored images from a browser.
* Run server-side person detection asynchronously after each upload.
* Keep the server structure small but ready for future storage backends such as NAS or Google Drive.
* Include a minimal ESP32-CAM client example under `examples/` that demonstrates the upload contract end to end.

The primary operating model is **low-power ESP32-CAM firmware** that wakes up, captures a snapshot, uploads it, and returns to deep sleep.
Continuous video and remote I/O control remain later phases.

## Current Functionality

* **Snapshot Upload**
    * `POST /upload` accepts raw JPEG bytes plus `X-Device-Id` and `X-Captured-At` headers.
    * Uploaded files are stored under camera-aware paths and indexed in SQLite metadata.
* **Multi-Camera Monitoring**
    * `GET /` shows a dashboard of known cameras, latest snapshots, and health state.
    * `GET /cameras/<camera_id>` shows recent snapshots and person detections for one camera.
    * JSON APIs under `/api/` expose camera summaries, snapshots, and detections.
* **Person Detection**
    * The server enqueues each snapshot for asynchronous person-only detection.
    * The default design targets local PC development and later Raspberry Pi 4 / CPU-only deployment.
    * Detection is enabled with the optional `ml` extra and a configured TFLite model.
* **Client Example**
    * The `examples/` directory contains a minimal ESP32-CAM sketch that captures one JPEG, sends it to `/upload`, can return to deep sleep afterward, and can stay OTA-reachable during development.
    * The example is the quickest way to verify the expected headers, payload shape, and server URL before wiring real hardware.
* **Browser Access**
    * `GET /files` lists stored images in reverse chronological order together with stored metadata when available.
    * `GET /files/<filename>` serves an individual uploaded image.
* **Operational Readiness**
    * `GET /healthz` provides a lightweight liveness endpoint.
    * The app uses an application factory and a storage abstraction so local disk can be replaced later.

## Planned Next Steps

* Add alternative storage backends behind the same server API:
    * NAS
    * Google Drive
* Tune the person detection pipeline for Raspberry Pi 4 CPU-only operation.
* Add alerting and notifications after dashboard and detection history are stable.
* Evaluate short clip upload as a later phase.

## Quick Start (Docker)

```bash
cp .env.example .env          # configure environment variables
docker compose build
docker compose up
```

The dashboard is then available at `http://localhost:8080`.

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
