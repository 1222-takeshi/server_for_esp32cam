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

## Documentation

Detailed documentation is available in the `/docs` directory:

*   **[Setup Guide](docs/SETUP.md):** Hardware requirements, software prerequisites, and step-by-step installation instructions.
*   **[Usage Guide](docs/USAGE.md):** How to run the project, use its current features, and example workflows.
*   **[Troubleshooting Guide](docs/TROUBLESHOOTING.md):** Solutions to common issues and debugging tips.
*   **[Project Roadmap](docs/ROADMAP.md):** Planned features and future direction of the project.
*   **[Contributing Guide](docs/CONTRIBUTING.md):** How to contribute to this project.

A Japanese version of the documentation is also available:
*   **[日本語README (README_ja.md)](README_ja.md)**
*   **[セットアップガイド (日本語)](docs/ja/SETUP_JA.md)**
*   **[使用方法ガイド (日本語)](docs/ja/USAGE_JA.md)**
*   **[トラブルシューティングガイド (日本語)](docs/ja/TROUBLESHOOTING_JA.md)**
*   **[プロジェクトロードマップ (日本語)](docs/ja/ROADMAP_JA.md)**
*   **[コントリビューションガイド (日本語)](docs/ja/CONTRIBUTING_JA.md)**


## License

This project is licensed under the MIT License. See the [LICENSE](LICENSE) file for more details.

## Acknowledgments

*   This project uses [Flask](https://flask.palletsprojects.com/), a lightweight WSGI web application framework.
*   Inspiration for working with ESP32-CAM and image servers often comes from the vibrant community of electronics and IoT enthusiasts. (If you have specific tutorials, articles, or individuals you'd like to credit, please add them here!)
*   The [ESP32 Camera Driver by Espressif](https://github.com/espressif/esp32-camera) is a fundamental component for any ESP32-CAM based project.
