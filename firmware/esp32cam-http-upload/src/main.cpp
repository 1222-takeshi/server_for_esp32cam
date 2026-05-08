#include <Arduino.h>
#include <cstring>

#if defined(WIFI_SMOKE_TEST) || defined(HTTP_UPLOAD_SMOKE_TEST) || defined(ENABLE_ARDUINO_OTA)
#include <WiFi.h>
#endif

#ifdef ENABLE_ARDUINO_OTA
#include <ArduinoOTA.h>
#endif

#ifdef HTTP_UPLOAD_SMOKE_TEST
#include <HTTPClient.h>
#include <time.h>
#endif

#if defined(CAMERA_INIT_SMOKE_TEST) || defined(JPEG_CAPTURE_SMOKE_TEST) || defined(HTTP_UPLOAD_SMOKE_TEST)
#include "esp_camera.h"
#endif

#if __has_include("esp32cam_config.h")
#include "esp32cam_config.h"
#define ESP32CAM_HAS_LOCAL_CONFIG 1
#else
#define ESP32CAM_HAS_LOCAL_CONFIG 0
#define WIFI_SSID ""
#define WIFI_PASSWORD ""
#define WIFI_CONNECT_TIMEOUT_MS 20000
#define UPLOAD_URL ""
#define DEVICE_ID "esp32cam-01"
#define HTTP_UPLOAD_TIMEOUT_MS 10000
#define NTP_SERVER "pool.ntp.org"
#define NTP_SYNC_TIMEOUT_MS 10000
#define OTA_HOSTNAME "esp32cam-01"
#define OTA_PORT 3232
#define OTA_PASSWORD ""
#define OTA_WAIT_WINDOW_MS 15000
#endif

#ifndef UPLOAD_URL
#define UPLOAD_URL ""
#endif
#ifndef DEVICE_ID
#define DEVICE_ID "esp32cam-01"
#endif
#ifndef HTTP_UPLOAD_TIMEOUT_MS
#define HTTP_UPLOAD_TIMEOUT_MS 10000
#endif
#ifndef NTP_SERVER
#define NTP_SERVER "pool.ntp.org"
#endif
#ifndef NTP_SYNC_TIMEOUT_MS
#define NTP_SYNC_TIMEOUT_MS 10000
#endif
#ifndef OTA_HOSTNAME
#define OTA_HOSTNAME "esp32cam-01"
#endif
#ifndef OTA_PORT
#define OTA_PORT 3232
#endif
#ifndef OTA_PASSWORD
#define OTA_PASSWORD ""
#endif
#ifndef OTA_WAIT_WINDOW_MS
#define OTA_WAIT_WINDOW_MS 15000
#endif

namespace {
constexpr uint32_t kBaudRate = 115200;
constexpr uint32_t kLogIntervalMs = 5000;
uint32_t lastLogMs = 0;
uint32_t bootCount = 0;

#if defined(CAMERA_INIT_SMOKE_TEST) || defined(JPEG_CAPTURE_SMOKE_TEST) || defined(HTTP_UPLOAD_SMOKE_TEST)
bool cameraInitSucceeded = false;

// AI Thinker ESP32-CAM pin map for OV2640.
constexpr int kCameraPinPwdn = 32;
constexpr int kCameraPinReset = -1;
constexpr int kCameraPinXclk = 0;
constexpr int kCameraPinSiod = 26;
constexpr int kCameraPinSioc = 27;
constexpr int kCameraPinY9 = 35;
constexpr int kCameraPinY8 = 34;
constexpr int kCameraPinY7 = 39;
constexpr int kCameraPinY6 = 36;
constexpr int kCameraPinY5 = 21;
constexpr int kCameraPinY4 = 19;
constexpr int kCameraPinY3 = 18;
constexpr int kCameraPinY2 = 5;
constexpr int kCameraPinVsync = 25;
constexpr int kCameraPinHref = 23;
constexpr int kCameraPinPclk = 22;
#endif

#if defined(JPEG_CAPTURE_SMOKE_TEST) || defined(HTTP_UPLOAD_SMOKE_TEST)
bool jpegCaptureSucceeded = false;
#endif

#ifdef HTTP_UPLOAD_SMOKE_TEST
bool wifiConnected = false;
bool httpUploadSucceeded = false;
int lastHttpStatusCode = 0;
#endif

void printHardwareInfo() {
  ++bootCount;
  Serial.printf("Boot count: %lu\n", static_cast<unsigned long>(bootCount));
  Serial.printf("Chip revision: %u\n", ESP.getChipRevision());
  Serial.printf("CPU frequency: %u MHz\n", ESP.getCpuFreqMHz());
  Serial.printf("Flash size: %u bytes\n", ESP.getFlashChipSize());
  Serial.printf("Free heap: %u bytes\n", ESP.getFreeHeap());
  Serial.printf("PSRAM: %s\n", psramFound() ? "found" : "not found");
  if (psramFound()) {
    Serial.printf("PSRAM size: %u bytes\n", ESP.getPsramSize());
    Serial.printf("Free PSRAM: %u bytes\n", ESP.getFreePsram());
  }
}

void printSerialSmokeBootInfo() {
  Serial.println();
  Serial.println("========================================");
  Serial.println("ESP32-CAM PlatformIO serial smoke test");
  printHardwareInfo();
  Serial.println("Serial smoke test is running.");
  Serial.println("Next step after this passes: Wi-Fi connection test.");
  Serial.println("========================================");
}

#if defined(WIFI_SMOKE_TEST) || defined(HTTP_UPLOAD_SMOKE_TEST)
void printMacAddress() {
  uint8_t mac[6] = {0};
  WiFi.macAddress(mac);
  Serial.printf("Wi-Fi MAC: %02X:%02X:%02X:%02X:%02X:%02X\n",
                mac[0],
                mac[1],
                mac[2],
                mac[3],
                mac[4],
                mac[5]);
}

void connectWiFi() {
  Serial.println();
  Serial.println("========================================");
  Serial.println("ESP32-CAM PlatformIO Wi-Fi smoke test");
  printHardwareInfo();
  printMacAddress();

#if ESP32CAM_HAS_LOCAL_CONFIG
  Serial.println("Local config: include/esp32cam_config.h found");
#else
  Serial.println("Local config: include/esp32cam_config.h not found");
  Serial.println("Copy include/esp32cam_config.h.example to include/esp32cam_config.h and set WIFI_SSID/WIFI_PASSWORD.");
#endif

  if (strlen(WIFI_SSID) == 0 || strcmp(WIFI_SSID, "YOUR_WIFI_SSID") == 0) {
    Serial.println("Wi-Fi smoke test skipped: WIFI_SSID is not configured.");
    Serial.println("========================================");
    return;
  }

  WiFi.persistent(false);
  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);
  WiFi.disconnect(true, true);
  delay(500);

  Serial.printf("Connecting to Wi-Fi SSID: %s\n", WIFI_SSID);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);

  const uint32_t startedAt = millis();
  uint32_t lastDotAt = 0;
  while (WiFi.status() != WL_CONNECTED && millis() - startedAt < WIFI_CONNECT_TIMEOUT_MS) {
    const uint32_t now = millis();
    if (now - lastDotAt >= 1000) {
      lastDotAt = now;
      Serial.print('.');
    }
    delay(50);
  }
  Serial.println();

  if (WiFi.status() == WL_CONNECTED) {
    Serial.println("WiFi connected");
    Serial.printf("IP address: %s\n", WiFi.localIP().toString().c_str());
    Serial.printf("Gateway: %s\n", WiFi.gatewayIP().toString().c_str());
    Serial.printf("Subnet: %s\n", WiFi.subnetMask().toString().c_str());
    Serial.printf("DNS: %s\n", WiFi.dnsIP().toString().c_str());
    Serial.printf("RSSI: %d dBm\n", WiFi.RSSI());
    Serial.println("Next step after this passes: camera init test.");
  } else {
    Serial.printf("WiFi connection failed: status=%d timeout_ms=%lu\n",
                  static_cast<int>(WiFi.status()),
                  static_cast<unsigned long>(WIFI_CONNECT_TIMEOUT_MS));
    Serial.println("Check SSID/password, 2.4GHz network availability, MAC filtering, and AP client isolation.");
  }
  Serial.println("========================================");
}
#endif

#if defined(CAMERA_INIT_SMOKE_TEST) || defined(JPEG_CAPTURE_SMOKE_TEST) || defined(HTTP_UPLOAD_SMOKE_TEST)
camera_config_t buildAiThinkerCameraConfig() {
  camera_config_t config = {};
  config.ledc_channel = LEDC_CHANNEL_0;
  config.ledc_timer = LEDC_TIMER_0;
  config.pin_d0 = kCameraPinY2;
  config.pin_d1 = kCameraPinY3;
  config.pin_d2 = kCameraPinY4;
  config.pin_d3 = kCameraPinY5;
  config.pin_d4 = kCameraPinY6;
  config.pin_d5 = kCameraPinY7;
  config.pin_d6 = kCameraPinY8;
  config.pin_d7 = kCameraPinY9;
  config.pin_xclk = kCameraPinXclk;
  config.pin_pclk = kCameraPinPclk;
  config.pin_vsync = kCameraPinVsync;
  config.pin_href = kCameraPinHref;
  config.pin_sccb_sda = kCameraPinSiod;
  config.pin_sccb_scl = kCameraPinSioc;
  config.pin_pwdn = kCameraPinPwdn;
  config.pin_reset = kCameraPinReset;
  config.xclk_freq_hz = 20000000;
  config.pixel_format = PIXFORMAT_JPEG;
  config.frame_size = psramFound() ? FRAMESIZE_QVGA : FRAMESIZE_QQVGA;
  config.jpeg_quality = 12;
  config.fb_count = 1;
  config.fb_location = psramFound() ? CAMERA_FB_IN_PSRAM : CAMERA_FB_IN_DRAM;
  config.grab_mode = CAMERA_GRAB_WHEN_EMPTY;
  return config;
}

void printCameraSensorInfo() {
  sensor_t *sensor = esp_camera_sensor_get();
  if (sensor == nullptr) {
    Serial.println("Camera sensor: unavailable after init");
    return;
  }

  Serial.printf("Camera sensor PID: 0x%02X\n", sensor->id.PID);
  Serial.printf("Camera sensor VER: 0x%02X\n", sensor->id.VER);
  Serial.printf("Camera sensor MIDH: 0x%02X\n", sensor->id.MIDH);
  Serial.printf("Camera sensor MIDL: 0x%02X\n", sensor->id.MIDL);
  if (sensor->id.PID == OV2640_PID) {
    Serial.println("Camera sensor detected: OV2640");
  } else {
    Serial.println("Camera sensor detected: unexpected PID; check module type if capture fails later.");
  }
}

bool initCamera(const char *testName, const char *frameSizeLabel, const char *nextStepMessage) {
  Serial.println();
  Serial.println("========================================");
  Serial.println(testName);
  printHardwareInfo();
  Serial.println("Camera model: AI Thinker ESP32-CAM pin map");
  Serial.println("Expected sensor: OV2640");
  Serial.printf("Frame size: %s\n", frameSizeLabel);
  Serial.printf("Framebuffer location: %s\n", psramFound() ? "PSRAM" : "DRAM");

  const camera_config_t config = buildAiThinkerCameraConfig();
  const esp_err_t err = esp_camera_init(&config);
  if (err != ESP_OK) {
    cameraInitSucceeded = false;
    Serial.printf("Camera init failed: esp_camera_init error=0x%x\n", err);
    Serial.println("Check 5V power stability, camera ribbon seating, board model/pin map, and PSRAM health.");
    Serial.println("========================================");
    return false;
  }

  cameraInitSucceeded = true;
  Serial.println("Camera init succeeded");
  printCameraSensorInfo();
  Serial.printf("Free heap after camera init: %u bytes\n", ESP.getFreeHeap());
  if (psramFound()) {
    Serial.printf("Free PSRAM after camera init: %u bytes\n", ESP.getFreePsram());
  }
  Serial.println(nextStepMessage);
  Serial.println("========================================");
  return true;
}

void initCameraSmokeTest() {
  initCamera("ESP32-CAM PlatformIO camera init smoke test",
             psramFound() ? "QVGA" : "QQVGA",
             "Next step after this passes: JPEG capture test.");
}
#endif

#if defined(JPEG_CAPTURE_SMOKE_TEST) || defined(HTTP_UPLOAD_SMOKE_TEST)
const char *pixelFormatName(const pixformat_t format) {
  switch (format) {
    case PIXFORMAT_JPEG:
      return "JPEG";
    case PIXFORMAT_RGB565:
      return "RGB565";
    case PIXFORMAT_GRAYSCALE:
      return "GRAYSCALE";
    default:
      return "other";
  }
}

void captureJpegSmokeTest() {
  const bool initOk = initCamera("ESP32-CAM PlatformIO JPEG capture smoke test",
                                 psramFound() ? "QVGA" : "QQVGA",
                                 "Capturing one frame...");
  if (!initOk) {
    jpegCaptureSucceeded = false;
    return;
  }

  Serial.printf("Free heap before capture: %u bytes\n", ESP.getFreeHeap());
  if (psramFound()) {
    Serial.printf("Free PSRAM before capture: %u bytes\n", ESP.getFreePsram());
  }

  camera_fb_t *fb = esp_camera_fb_get();
  if (fb == nullptr) {
    jpegCaptureSucceeded = false;
    Serial.println("Frame capture failed: esp_camera_fb_get returned null");
    Serial.println("Check power stability, camera ribbon seating, and PSRAM availability.");
    Serial.println("========================================");
    return;
  }

  Serial.println("Frame captured");
  Serial.printf("Format: %s\n", pixelFormatName(fb->format));
  Serial.printf("Width: %u px\n", fb->width);
  Serial.printf("Height: %u px\n", fb->height);
  Serial.printf("Size: %u bytes\n", fb->len);
  Serial.printf("Free heap after capture: %u bytes\n", ESP.getFreeHeap());
  if (psramFound()) {
    Serial.printf("Free PSRAM after capture: %u bytes\n", ESP.getFreePsram());
  }

  jpegCaptureSucceeded = (fb->format == PIXFORMAT_JPEG && fb->len > 0);
  if (jpegCaptureSucceeded) {
    Serial.println("JPEG capture succeeded");
  } else {
    Serial.println("JPEG capture failed: captured frame is not a non-empty JPEG");
  }

  esp_camera_fb_return(fb);
  Serial.println("Framebuffer returned");
  Serial.printf("Free heap after framebuffer return: %u bytes\n", ESP.getFreeHeap());
  if (psramFound()) {
    Serial.printf("Free PSRAM after framebuffer return: %u bytes\n", ESP.getFreePsram());
  }
  Serial.println("Next step after this passes: HTTP POST upload test.");
  Serial.println("========================================");
}

#ifdef HTTP_UPLOAD_SMOKE_TEST
bool connectWiFiForHttpUpload() {
  printMacAddress();

#if ESP32CAM_HAS_LOCAL_CONFIG
  Serial.println("Local config: include/esp32cam_config.h found");
#else
  Serial.println("Local config: include/esp32cam_config.h not found");
  Serial.println("Copy include/esp32cam_config.h.example to include/esp32cam_config.h and set WIFI_SSID/WIFI_PASSWORD/UPLOAD_URL.");
#endif

  if (strlen(WIFI_SSID) == 0 || strcmp(WIFI_SSID, "YOUR_WIFI_SSID") == 0) {
    Serial.println("HTTP upload smoke test skipped: WIFI_SSID is not configured.");
    return false;
  }
  if (strlen(UPLOAD_URL) == 0 || strcmp(UPLOAD_URL, "http://YOUR_MAC_LAN_IP:8080/upload") == 0) {
    Serial.println("HTTP upload smoke test skipped: UPLOAD_URL is not configured.");
    return false;
  }

  WiFi.persistent(false);
  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);
  WiFi.disconnect(true, true);
  delay(500);

  Serial.printf("Connecting to Wi-Fi SSID: %s\n", WIFI_SSID);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);

  const uint32_t startedAt = millis();
  uint32_t lastDotAt = 0;
  while (WiFi.status() != WL_CONNECTED && millis() - startedAt < WIFI_CONNECT_TIMEOUT_MS) {
    const uint32_t now = millis();
    if (now - lastDotAt >= 1000) {
      lastDotAt = now;
      Serial.print('.');
    }
    delay(50);
  }
  Serial.println();

  if (WiFi.status() != WL_CONNECTED) {
    Serial.printf("WiFi connection failed: status=%d timeout_ms=%lu\n",
                  static_cast<int>(WiFi.status()),
                  static_cast<unsigned long>(WIFI_CONNECT_TIMEOUT_MS));
    return false;
  }

  Serial.println("WiFi connected");
  Serial.printf("IP address: %s\n", WiFi.localIP().toString().c_str());
  Serial.printf("Gateway: %s\n", WiFi.gatewayIP().toString().c_str());
  Serial.printf("Subnet: %s\n", WiFi.subnetMask().toString().c_str());
  Serial.printf("DNS: %s\n", WiFi.dnsIP().toString().c_str());
  Serial.printf("RSSI: %d dBm\n", WiFi.RSSI());
  return true;
}

String capturedAtIso8601Utc() {
  configTime(0, 0, NTP_SERVER);
  const uint32_t startedAt = millis();
  uint32_t lastDotAt = 0;
  Serial.printf("Syncing time with NTP server: %s\n", NTP_SERVER);
  time_t now = time(nullptr);
  while (now < 1700000000 && millis() - startedAt < NTP_SYNC_TIMEOUT_MS) {
    const uint32_t currentMs = millis();
    if (currentMs - lastDotAt >= 1000) {
      lastDotAt = currentMs;
      Serial.print('.');
    }
    delay(50);
    now = time(nullptr);
  }
  Serial.println();

  if (now < 1700000000) {
    Serial.println("NTP sync timed out; using Unix epoch fallback captured_at.");
    return "1970-01-01T00:00:00Z";
  }

  struct tm timeinfo = {};
  gmtime_r(&now, &timeinfo);
  char buffer[25] = {0};
  strftime(buffer, sizeof(buffer), "%Y-%m-%dT%H:%M:%SZ", &timeinfo);
  return String(buffer);
}

void runHttpUploadSmokeTest() {
  Serial.println();
  Serial.println("========================================");
  Serial.println("ESP32-CAM PlatformIO HTTP upload smoke test");
  printHardwareInfo();
  Serial.printf("Upload URL: %s\n", strlen(UPLOAD_URL) > 0 ? UPLOAD_URL : "not configured");
  Serial.printf("Device ID: %s\n", DEVICE_ID);

  wifiConnected = connectWiFiForHttpUpload();
  if (!wifiConnected) {
    httpUploadSucceeded = false;
    Serial.println("HTTP upload skipped because Wi-Fi/configuration is not ready.");
    Serial.println("========================================");
    return;
  }

  const String capturedAt = capturedAtIso8601Utc();
  Serial.printf("Captured-At header: %s\n", capturedAt.c_str());

  const bool initOk = initCamera("ESP32-CAM PlatformIO HTTP upload camera step",
                                 psramFound() ? "QVGA" : "QQVGA",
                                 "Capturing one frame for HTTP upload...");
  if (!initOk) {
    httpUploadSucceeded = false;
    return;
  }

  camera_fb_t *fb = esp_camera_fb_get();
  if (fb == nullptr) {
    jpegCaptureSucceeded = false;
    httpUploadSucceeded = false;
    Serial.println("Frame capture failed: esp_camera_fb_get returned null");
    Serial.println("========================================");
    return;
  }

  Serial.println("Frame captured");
  Serial.printf("Format: %s\n", pixelFormatName(fb->format));
  Serial.printf("Width: %u px\n", fb->width);
  Serial.printf("Height: %u px\n", fb->height);
  Serial.printf("Size: %u bytes\n", fb->len);
  jpegCaptureSucceeded = (fb->format == PIXFORMAT_JPEG && fb->len > 0);
  if (!jpegCaptureSucceeded) {
    httpUploadSucceeded = false;
    Serial.println("HTTP upload skipped: captured frame is not a non-empty JPEG");
    esp_camera_fb_return(fb);
    Serial.println("Framebuffer returned");
    Serial.println("========================================");
    return;
  }

  Serial.println("HTTP POST upload starting...");
  HTTPClient http;
  http.setTimeout(HTTP_UPLOAD_TIMEOUT_MS);
  if (!http.begin(UPLOAD_URL)) {
    httpUploadSucceeded = false;
    Serial.println("HTTP client begin failed; check UPLOAD_URL.");
    esp_camera_fb_return(fb);
    Serial.println("Framebuffer returned");
    Serial.println("========================================");
    return;
  }

  http.addHeader("Content-Type", "image/jpeg");
  http.addHeader("X-Device-Id", DEVICE_ID);
  http.addHeader("X-Captured-At", capturedAt);
  lastHttpStatusCode = http.POST(fb->buf, fb->len);
  Serial.printf("HTTP status code: %d\n", lastHttpStatusCode);

  const String response = http.getString();
  Serial.printf("HTTP response length: %u bytes\n", response.length());
  if (response.length() > 0) {
    Serial.printf("HTTP response body: %.240s\n", response.c_str());
  }
  http.end();

  httpUploadSucceeded = (lastHttpStatusCode >= 200 && lastHttpStatusCode < 300);
  Serial.printf("HTTP upload result: %s\n", httpUploadSucceeded ? "ok" : "failed");

  esp_camera_fb_return(fb);
  Serial.println("Framebuffer returned");
  Serial.printf("Free heap after upload: %u bytes\n", ESP.getFreeHeap());
  if (psramFound()) {
    Serial.printf("Free PSRAM after upload: %u bytes\n", ESP.getFreePsram());
  }
  Serial.println("Next step after this passes: continuous upload / WebSocket / OTA planning.");
  Serial.println("========================================");
}
#endif
#endif

#ifdef ENABLE_ARDUINO_OTA
void setupArduinoOta() {
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("OTA setup skipped: Wi-Fi is not connected.");
    return;
  }

  ArduinoOTA.setHostname(OTA_HOSTNAME);
  ArduinoOTA.setPort(OTA_PORT);
  if (strlen(OTA_PASSWORD) > 0) {
    ArduinoOTA.setPassword(OTA_PASSWORD);
  }
  ArduinoOTA.onStart([]() {
    Serial.println("OTA update started");
  });
  ArduinoOTA.onEnd([]() {
    Serial.println("OTA update finished");
  });
  ArduinoOTA.onProgress([](unsigned int progress, unsigned int total) {
    const unsigned int percent = total == 0 ? 0 : (progress * 100U) / total;
    Serial.printf("OTA update progress: %u%%\n", percent);
  });
  ArduinoOTA.onError([](ota_error_t error) {
    Serial.printf("OTA update error: %u\n", static_cast<unsigned int>(error));
  });
  ArduinoOTA.begin();
  Serial.printf("OTA ready: hostname=%s port=%u ip=%s\n",
                OTA_HOSTNAME,
                static_cast<unsigned int>(OTA_PORT),
                WiFi.localIP().toString().c_str());
}
#endif

}  // namespace

void setup() {
  Serial.begin(kBaudRate);
  delay(1000);
#if defined(HTTP_UPLOAD_SMOKE_TEST)
  runHttpUploadSmokeTest();
#ifdef ENABLE_ARDUINO_OTA
  setupArduinoOta();
#endif
#elif defined(JPEG_CAPTURE_SMOKE_TEST)
  captureJpegSmokeTest();
#elif defined(CAMERA_INIT_SMOKE_TEST)
  initCameraSmokeTest();
#elif defined(WIFI_SMOKE_TEST)
  connectWiFi();
#else
  printSerialSmokeBootInfo();
#endif
}

void loop() {
#ifdef ENABLE_ARDUINO_OTA
  ArduinoOTA.handle();
#endif
  const uint32_t now = millis();
  if (now - lastLogMs >= kLogIntervalMs) {
    lastLogMs = now;
#if defined(HTTP_UPLOAD_SMOKE_TEST)
    Serial.printf("alive: millis=%lu wifi=%s camera_init=%s jpeg_capture=%s http_upload=%s http_status=%d free_heap=%u free_psram=%u\n",
                  static_cast<unsigned long>(now),
                  wifiConnected ? "ok" : "failed",
                  cameraInitSucceeded ? "ok" : "failed",
                  jpegCaptureSucceeded ? "ok" : "failed",
                  httpUploadSucceeded ? "ok" : "failed",
                  lastHttpStatusCode,
                  ESP.getFreeHeap(),
                  psramFound() ? ESP.getFreePsram() : 0);
#elif defined(JPEG_CAPTURE_SMOKE_TEST)
    Serial.printf("alive: millis=%lu camera_init=%s jpeg_capture=%s free_heap=%u free_psram=%u\n",
                  static_cast<unsigned long>(now),
                  cameraInitSucceeded ? "ok" : "failed",
                  jpegCaptureSucceeded ? "ok" : "failed",
                  ESP.getFreeHeap(),
                  psramFound() ? ESP.getFreePsram() : 0);
#elif defined(CAMERA_INIT_SMOKE_TEST)
    Serial.printf("alive: millis=%lu camera_init=%s free_heap=%u free_psram=%u\n",
                  static_cast<unsigned long>(now),
                  cameraInitSucceeded ? "ok" : "failed",
                  ESP.getFreeHeap(),
                  psramFound() ? ESP.getFreePsram() : 0);
#elif defined(WIFI_SMOKE_TEST)
    Serial.printf("alive: millis=%lu wifi_status=%d ip=%s rssi=%d free_heap=%u\n",
                  static_cast<unsigned long>(now),
                  static_cast<int>(WiFi.status()),
                  WiFi.localIP().toString().c_str(),
                  WiFi.status() == WL_CONNECTED ? WiFi.RSSI() : 0,
                  ESP.getFreeHeap());
#else
    Serial.printf("alive: millis=%lu free_heap=%u\n",
                  static_cast<unsigned long>(now),
                  ESP.getFreeHeap());
#endif
  }
  delay(10);
}
