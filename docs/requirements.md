# Requirements

## Hardware Requirements

This server application runs on a standard computer or server environment (Windows, Linux, macOS).

To use the current milestone as intended, you will typically need:

*   **An image capturing device:**
    *   **ESP32-CAM:** The primary target. It should capture a JPEG image and send it to this server's `/upload` endpoint via HTTP POST.
    *   **Other devices that can send JPEG over HTTP:** Useful for testing, but not the primary design target.
*   **Network Connectivity:** The ESP32-CAM and the server must be able to communicate over the same network, or the server must be reachable from the device.
*   **Storage Capacity:** Enough local disk space to store uploaded JPEG snapshots.

## Software Requirements

### Server-Side:

*   **Python:** Python 3.10 or later.
*   **Flask:** The web framework used by the server.
*   **pytest:** Used for automated tests.
*   **Operating System:** Linux, macOS, or Windows.

### Client-Side (e.g., ESP32-CAM):

*   **For ESP32-CAM:**
    *   **Arduino IDE** or **PlatformIO IDE**
    *   **ESP32 Board Support Package:** To be installed in the Arduino IDE or PlatformIO.
    *   **Relevant Libraries:** Camera support, Wi-Fi, and HTTP client support for JPEG upload.

### Optional (Future Phases):

*   **NAS access libraries or mount support:** For storing files on network storage.
*   **Google Drive API libraries:** If cloud-backed storage is added later.
*   **OpenCV / ML frameworks:** Only after snapshot upload and storage are stable.
