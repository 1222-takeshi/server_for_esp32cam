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