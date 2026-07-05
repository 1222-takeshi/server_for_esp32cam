#include <Arduino.h>
#include <cstring>

#if defined(WIFI_SMOKE_TEST) || defined(HTTP_UPLOAD_SMOKE_TEST) || defined(WEBSOCKET_UPLOAD_SMOKE_TEST) || defined(ENABLE_ARDUINO_OTA)
#include <WiFi.h>
#endif

#ifdef ENABLE_ARDUINO_OTA
#include <ArduinoOTA.h>
#endif

#ifdef HTTP_UPLOAD_SMOKE_TEST
#include <ArduinoJson.h>
#include <HTTPClient.h>
#include <time.h>
#endif

#ifdef WEBSOCKET_UPLOAD_SMOKE_TEST
#include <WebSocketsClient.h>
#endif

#if defined(CAMERA_INIT_SMOKE_TEST) || defined(JPEG_CAPTURE_SMOKE_TEST) || defined(HTTP_UPLOAD_SMOKE_TEST) || defined(WEBSOCKET_UPLOAD_SMOKE_TEST)
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
#define DEVICE_API_BASE_URL ""
#define DEVICE_API_KEY ""
#define DESIRED_CONFIG_POLL_TIMEOUT_MS 10000
#define DESIRED_CONFIG_FIRMWARE_VERSION "0.3.0"
#define NTP_SERVER "pool.ntp.org"
#define NTP_SYNC_TIMEOUT_MS 10000
#define OTA_HOSTNAME "esp32cam-01"
#define OTA_PORT 3232
#define OTA_PASSWORD ""
#define OTA_WAIT_WINDOW_MS 15000
#define WS_UPLOAD_HOST ""
#define WS_UPLOAD_PORT 8080
#define WS_UPLOAD_PATH "/ws/cameras/" DEVICE_ID "/upload"
#define CAPTURE_INTERVAL_MS 5000
#define CAPTURE_FRAME_LIMIT 0
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
#ifndef DEVICE_API_BASE_URL
#define DEVICE_API_BASE_URL ""
#endif
#ifndef DEVICE_API_KEY
#define DEVICE_API_KEY ""
#endif
#ifndef DESIRED_CONFIG_POLL_TIMEOUT_MS
#define DESIRED_CONFIG_POLL_TIMEOUT_MS 10000
#endif
#ifndef DESIRED_CONFIG_FIRMWARE_VERSION
#define DESIRED_CONFIG_FIRMWARE_VERSION "0.3.0"
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
#ifndef WS_UPLOAD_HOST
#define WS_UPLOAD_HOST ""
#endif
#ifndef WS_UPLOAD_PORT
#define WS_UPLOAD_PORT 8080
#endif
#ifndef WS_UPLOAD_PATH
#define WS_UPLOAD_PATH "/ws/cameras/" DEVICE_ID "/upload"
#endif
#ifndef CAPTURE_INTERVAL_MS
#define CAPTURE_INTERVAL_MS 5000
#endif
#ifndef CAPTURE_FRAME_LIMIT
#define CAPTURE_FRAME_LIMIT 0
#endif
#ifndef FLASH_LED_ENABLED
#define FLASH_LED_ENABLED 0
#endif
#ifndef FLASH_LED_PIN
#define FLASH_LED_PIN 4
#endif
#ifndef FLASH_LED_BRIGHTNESS_PCT
#define FLASH_LED_BRIGHTNESS_PCT 50
#endif
#ifndef FLASH_LED_WARMUP_MS
#define FLASH_LED_WARMUP_MS 150
#endif
#ifndef FLASH_NIGHT_START_HOUR
#define FLASH_NIGHT_START_HOUR 18
#endif
#ifndef FLASH_NIGHT_END_HOUR
#define FLASH_NIGHT_END_HOUR 6
#endif
#ifndef FLASH_TZ_OFFSET_SECONDS
#define FLASH_TZ_OFFSET_SECONDS 32400
#endif

namespace {
constexpr uint32_t kBaudRate = 115200;
constexpr uint32_t kLogIntervalMs = 5000;
uint32_t lastLogMs = 0;
uint32_t bootCount = 0;

#ifdef HTTP_UPLOAD_SMOKE_TEST
struct DesiredConfig {
  uint32_t captureIntervalSeconds = 300;
  framesize_t frameSize = FRAMESIZE_QVGA;
  const char *frameSizeLabel = "QVGA";
  int jpegQuality = 12;
  bool vflip = false;
  bool hmirror = false;
  bool restartRequested = false;
  bool otaUpdateRequested = false;
  uint32_t otaWindowSeconds = 120;
};

RTC_DATA_ATTR uint32_t desiredConfigVersion = 0;
DesiredConfig desiredConfig;
bool desiredConfigPollSucceeded = false;
bool desiredConfigAckSucceeded = false;
String desiredConfigMessage = "not_polled";
#ifdef ENABLE_ARDUINO_OTA
void setupArduinoOta();
#endif
#endif

#if defined(CAMERA_INIT_SMOKE_TEST) || defined(JPEG_CAPTURE_SMOKE_TEST) || defined(HTTP_UPLOAD_SMOKE_TEST) || defined(WEBSOCKET_UPLOAD_SMOKE_TEST)
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

#if defined(JPEG_CAPTURE_SMOKE_TEST) || defined(HTTP_UPLOAD_SMOKE_TEST) || defined(WEBSOCKET_UPLOAD_SMOKE_TEST)
bool jpegCaptureSucceeded = false;
#endif

#if defined(HTTP_UPLOAD_SMOKE_TEST) || defined(WEBSOCKET_UPLOAD_SMOKE_TEST)
bool wifiConnected = false;
#endif

#ifdef HTTP_UPLOAD_SMOKE_TEST
bool httpUploadSucceeded = false;
int lastHttpStatusCode = 0;
#endif

#ifdef WEBSOCKET_UPLOAD_SMOKE_TEST
WebSocketsClient webSocket;
bool websocketConnected = false;
bool websocketUploadSucceeded = false;
uint32_t lastCaptureMs = 0;
uint32_t websocketFramesSent = 0;
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

#if defined(WIFI_SMOKE_TEST) || defined(HTTP_UPLOAD_SMOKE_TEST) || defined(WEBSOCKET_UPLOAD_SMOKE_TEST)
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

#if defined(CAMERA_INIT_SMOKE_TEST) || defined(JPEG_CAPTURE_SMOKE_TEST) || defined(HTTP_UPLOAD_SMOKE_TEST) || defined(WEBSOCKET_UPLOAD_SMOKE_TEST)
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
#ifdef HTTP_UPLOAD_SMOKE_TEST
  config.frame_size = desiredConfig.frameSize;
  config.jpeg_quality = desiredConfig.jpegQuality;
#else
  config.frame_size = psramFound() ? FRAMESIZE_QVGA : FRAMESIZE_QQVGA;
  config.jpeg_quality = 12;
#endif
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
#ifdef HTTP_UPLOAD_SMOKE_TEST
  sensor_t *sensor = esp_camera_sensor_get();
  if (sensor != nullptr) {
    sensor->set_vflip(sensor, desiredConfig.vflip ? 1 : 0);
    sensor->set_hmirror(sensor, desiredConfig.hmirror ? 1 : 0);
    Serial.printf("Desired config sensor orientation: vflip=%s hmirror=%s\n",
                  desiredConfig.vflip ? "true" : "false",
                  desiredConfig.hmirror ? "true" : "false");
  }
#endif
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

#if defined(JPEG_CAPTURE_SMOKE_TEST) || defined(HTTP_UPLOAD_SMOKE_TEST) || defined(WEBSOCKET_UPLOAD_SMOKE_TEST)
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

#if defined(HTTP_UPLOAD_SMOKE_TEST) || defined(WEBSOCKET_UPLOAD_SMOKE_TEST)
bool connectWiFiForUpload(const char *testName, const bool requireHttpUploadUrl) {
  printMacAddress();

#if ESP32CAM_HAS_LOCAL_CONFIG
  Serial.println("Local config: include/esp32cam_config.h found");
#else
  Serial.println("Local config: include/esp32cam_config.h not found");
  Serial.println("Copy include/esp32cam_config.h.example to include/esp32cam_config.h and set WIFI_SSID/WIFI_PASSWORD/upload settings.");
#endif

  if (strlen(WIFI_SSID) == 0 || strcmp(WIFI_SSID, "YOUR_WIFI_SSID") == 0) {
    Serial.printf("%s skipped: WIFI_SSID is not configured.\n", testName);
    return false;
  }
  if (requireHttpUploadUrl && (strlen(UPLOAD_URL) == 0 || strcmp(UPLOAD_URL, "http://YOUR_MAC_LAN_IP:8080/upload") == 0)) {
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
#endif

#ifdef HTTP_UPLOAD_SMOKE_TEST
framesize_t parseDesiredFrameSize(const char *value, const framesize_t fallback, const char **label) {
  if (value == nullptr) {
    return fallback;
  }
  if (strcmp(value, "QVGA") == 0) { *label = "QVGA"; return FRAMESIZE_QVGA; }
  if (strcmp(value, "VGA") == 0) { *label = "VGA"; return FRAMESIZE_VGA; }
  if (strcmp(value, "SVGA") == 0) { *label = "SVGA"; return FRAMESIZE_SVGA; }
  if (strcmp(value, "XGA") == 0) { *label = "XGA"; return FRAMESIZE_XGA; }
  if (strcmp(value, "SXGA") == 0) { *label = "SXGA"; return FRAMESIZE_SXGA; }
  if (strcmp(value, "UXGA") == 0) { *label = "UXGA"; return FRAMESIZE_UXGA; }
  return fallback;
}

String desiredConfigUrl(const char *suffix) {
  String base = DEVICE_API_BASE_URL;
  if (base.endsWith("/")) {
    base.remove(base.length() - 1);
  }
  return base + "/api/devices/" + DEVICE_ID + suffix;
}

bool addDeviceApiKeyHeader(HTTPClient &http) {
  if (strlen(DEVICE_API_KEY) == 0) {
    return false;
  }
  http.addHeader("X-API-Key", DEVICE_API_KEY);
  return true;
}

bool sendDesiredConfigAck(const uint32_t version, const bool applied, const String &message) {
  if (strlen(DEVICE_API_BASE_URL) == 0) {
    Serial.println("Desired config ack skipped: DEVICE_API_BASE_URL is not configured.");
    return false;
  }
  HTTPClient http;
  http.setTimeout(DESIRED_CONFIG_POLL_TIMEOUT_MS);
  const String url = desiredConfigUrl("/desired-config/ack");
  if (!http.begin(url)) {
    Serial.println("Desired config ack failed: HTTP client begin failed.");
    return false;
  }
  http.addHeader("Content-Type", "application/json");
  addDeviceApiKeyHeader(http);
  JsonDocument doc;
  doc["version"] = version;
  doc["applied"] = applied;
  doc["message"] = message;
  doc["firmware_version"] = DESIRED_CONFIG_FIRMWARE_VERSION;
  doc["free_heap"] = ESP.getFreeHeap();
  String body;
  serializeJson(doc, body);
  const int status = http.POST(body);
  const String response = http.getString();
  http.end();
  desiredConfigAckSucceeded = (status >= 200 && status < 300);
  Serial.printf("Desired config ack status: %d message=%s\n", status, message.c_str());
  if (response.length() > 0) {
    Serial.printf("Desired config ack response: %.240s\n", response.c_str());
  }
  return desiredConfigAckSucceeded;
}

bool applyDesiredConfig(JsonObject config, String &message) {
  DesiredConfig next = desiredConfig;
  if (config["capture_interval_seconds"].is<uint32_t>()) {
    const uint32_t value = config["capture_interval_seconds"].as<uint32_t>();
    if (value < 60 || value > 86400) { message = "invalid capture_interval_seconds"; return false; }
    next.captureIntervalSeconds = value;
  }
  if (config["frame_size"].is<const char *>()) {
    const char *label = next.frameSizeLabel;
    next.frameSize = parseDesiredFrameSize(config["frame_size"].as<const char *>(), next.frameSize, &label);
    if (strcmp(label, config["frame_size"].as<const char *>()) != 0) { message = "invalid frame_size"; return false; }
    next.frameSizeLabel = label;
  }
  if (config["jpeg_quality"].is<int>()) {
    const int value = config["jpeg_quality"].as<int>();
    if (value < 10 || value > 63) { message = "invalid jpeg_quality"; return false; }
    next.jpegQuality = value;
  }
  if (config["vflip"].is<bool>()) { next.vflip = config["vflip"].as<bool>(); }
  if (config["hmirror"].is<bool>()) { next.hmirror = config["hmirror"].as<bool>(); }
  if (config["restart_requested"].is<bool>()) { next.restartRequested = config["restart_requested"].as<bool>(); }
  if (config["ota_update_requested"].is<bool>()) { next.otaUpdateRequested = config["ota_update_requested"].as<bool>(); }
  if (config["ota_window_seconds"].is<uint32_t>()) {
    const uint32_t value = config["ota_window_seconds"].as<uint32_t>();
    if (value < 30 || value > 600) { message = "invalid ota_window_seconds"; return false; }
    next.otaWindowSeconds = value;
  }
  desiredConfig = next;
  message = "applied";
  return true;
}

bool pollDesiredConfig() {
  desiredConfigPollSucceeded = false;
  desiredConfigMessage = "not_changed";
  if (strlen(DEVICE_API_BASE_URL) == 0 || strcmp(DEVICE_API_BASE_URL, "http://YOUR_MAC_LAN_IP:8080") == 0) {
    Serial.println("Desired config polling skipped: DEVICE_API_BASE_URL is not configured.");
    return false;
  }
  HTTPClient http;
  http.setTimeout(DESIRED_CONFIG_POLL_TIMEOUT_MS);
  const String suffix = String("/desired-config?current_version=") + desiredConfigVersion;
  const String url = desiredConfigUrl(suffix.c_str());
  Serial.printf("Desired config GET: %s\n", url.c_str());
  if (!http.begin(url)) {
    desiredConfigMessage = "http_begin_failed";
    return false;
  }
  addDeviceApiKeyHeader(http);
  const int status = http.GET();
  const String response = http.getString();
  http.end();
  Serial.printf("Desired config GET status: %d\n", status);
  if (response.length() > 0) {
    Serial.printf("Desired config response: %.300s\n", response.c_str());
  }
  if (status < 200 || status >= 300) {
    desiredConfigMessage = String("poll_failed_status_") + status;
    return false;
  }
  JsonDocument doc;
  const DeserializationError error = deserializeJson(doc, response);
  if (error) {
    desiredConfigMessage = String("json_parse_failed: ") + error.c_str();
    return false;
  }
  const uint32_t version = doc["version"] | desiredConfigVersion;
  const bool changed = doc["changed"] | (version != desiredConfigVersion);
  if (!changed && version == desiredConfigVersion) {
    desiredConfigPollSucceeded = true;
    desiredConfigMessage = "unchanged";
    return true;
  }
  JsonObject config = doc["config"].as<JsonObject>();
  if (config.isNull()) {
    desiredConfigMessage = "missing_config";
    sendDesiredConfigAck(version, false, desiredConfigMessage);
    return false;
  }
  String message;
  const bool applied = applyDesiredConfig(config, message);
  desiredConfigMessage = message;
  sendDesiredConfigAck(version, applied, message);
  if (!applied) {
    return false;
  }
  desiredConfigVersion = version;
  desiredConfigPollSucceeded = true;
  Serial.printf("Desired config applied: version=%lu capture_interval_seconds=%lu frame_size=%s jpeg_quality=%d vflip=%s hmirror=%s restart_requested=%s ota_update_requested=%s ota_window_seconds=%lu\n",
                static_cast<unsigned long>(desiredConfigVersion),
                static_cast<unsigned long>(desiredConfig.captureIntervalSeconds),
                desiredConfig.frameSizeLabel,
                desiredConfig.jpegQuality,
                desiredConfig.vflip ? "true" : "false",
                desiredConfig.hmirror ? "true" : "false",
                desiredConfig.restartRequested ? "true" : "false",
                desiredConfig.otaUpdateRequested ? "true" : "false",
                static_cast<unsigned long>(desiredConfig.otaWindowSeconds));
  return true;
}

void runRequestedOtaWindow() {
  if (!desiredConfig.otaUpdateRequested) {
    return;
  }
#ifndef ENABLE_ARDUINO_OTA
  sendDesiredConfigAck(desiredConfigVersion, false, "ota_requested_but_firmware_ota_disabled");
  return;
#else
  if (strlen(OTA_PASSWORD) == 0) {
    sendDesiredConfigAck(desiredConfigVersion, false, "ota_requested_but_ota_password_empty");
    return;
  }
  setupArduinoOta();
  sendDesiredConfigAck(desiredConfigVersion, true, "ota_ready");
  const uint32_t windowMs = desiredConfig.otaWindowSeconds * 1000UL;
  const uint32_t startedAt = millis();
  Serial.printf("Desired config OTA window started: %lu seconds\n", static_cast<unsigned long>(desiredConfig.otaWindowSeconds));
  while (millis() - startedAt < windowMs) {
    ArduinoOTA.handle();
    delay(10);
  }
  sendDesiredConfigAck(desiredConfigVersion, false, "ota_timeout");
  Serial.println("Desired config OTA window timed out; continuing normal upload cycle.");
#endif
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

  wifiConnected = connectWiFiForUpload("HTTP upload smoke test", true);
  if (!wifiConnected) {
    httpUploadSucceeded = false;
    Serial.println("HTTP upload skipped because Wi-Fi/configuration is not ready.");
    Serial.println("========================================");
    return;
  }

  pollDesiredConfig();
  runRequestedOtaWindow();
  if (desiredConfig.restartRequested) {
    Serial.println("Desired config requested restart; restarting after ack.");
    delay(500);
    ESP.restart();
  }

  const String capturedAt = capturedAtIso8601Utc();
  Serial.printf("Captured-At header: %s\n", capturedAt.c_str());

  const bool initOk = initCamera("ESP32-CAM PlatformIO HTTP upload camera step",
                                 desiredConfig.frameSizeLabel,
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
  addDeviceApiKeyHeader(http);
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

#ifdef WEBSOCKET_UPLOAD_SMOKE_TEST
void webSocketEvent(const WStype_t type, uint8_t *payload, const size_t length) {
  switch (type) {
    case WStype_CONNECTED:
      websocketConnected = true;
      Serial.printf("WebSocket connected: %s\n", payload != nullptr ? reinterpret_cast<const char *>(payload) : "");
      break;
    case WStype_DISCONNECTED:
      websocketConnected = false;
      Serial.println("WebSocket disconnected");
      break;
    case WStype_TEXT:
      Serial.printf("WebSocket response: %.*s\n", static_cast<int>(length), payload);
      break;
    case WStype_ERROR:
      Serial.printf("WebSocket error: %.*s\n", static_cast<int>(length), payload);
      break;
    default:
      break;
  }
}

void setupWebSocketUpload() {
  Serial.println();
  Serial.println("========================================");
  Serial.println("ESP32-CAM PlatformIO WebSocket continuous upload test");
  printHardwareInfo();
  Serial.printf("WebSocket destination: ws://%s:%u%s\n",
                strlen(WS_UPLOAD_HOST) > 0 ? WS_UPLOAD_HOST : "not configured",
                static_cast<unsigned int>(WS_UPLOAD_PORT),
                WS_UPLOAD_PATH);
  Serial.printf("Device ID: %s\n", DEVICE_ID);
  Serial.printf("Capture interval: %lu ms\n", static_cast<unsigned long>(CAPTURE_INTERVAL_MS));
  Serial.printf("Frame limit: %lu\n", static_cast<unsigned long>(CAPTURE_FRAME_LIMIT));

  wifiConnected = connectWiFiForUpload("WebSocket upload test", false);
  if (!wifiConnected) {
    Serial.println("WebSocket upload skipped because Wi-Fi/configuration is not ready.");
    Serial.println("========================================");
    return;
  }
  if (strlen(WS_UPLOAD_HOST) == 0 || strcmp(WS_UPLOAD_HOST, "YOUR_MAC_LAN_IP") == 0) {
    Serial.println("WebSocket upload skipped: WS_UPLOAD_HOST is not configured.");
    Serial.println("========================================");
    return;
  }

  const bool initOk = initCamera("ESP32-CAM PlatformIO WebSocket upload camera step",
                                 psramFound() ? "QVGA" : "QQVGA",
                                 "Starting WebSocket client for continuous capture...");
  if (!initOk) {
    return;
  }

  String websocketHeaders;
  if (strlen(DEVICE_API_KEY) > 0) {
    websocketHeaders = String("X-API-Key: ") + DEVICE_API_KEY;
    webSocket.setExtraHeaders(websocketHeaders.c_str());
  }
  webSocket.begin(WS_UPLOAD_HOST, WS_UPLOAD_PORT, WS_UPLOAD_PATH);
  webSocket.onEvent(webSocketEvent);
  webSocket.setReconnectInterval(5000);
  Serial.println("WebSocket client started");
  Serial.println("========================================");
}

#if FLASH_LED_ENABLED
constexpr int kFlashLedcChannel = 4;  // camera XCLK uses LEDC channel/timer 0

void setupFlashLed() {
  ledcSetup(kFlashLedcChannel, 5000, 8);
  ledcAttachPin(FLASH_LED_PIN, kFlashLedcChannel);
  ledcWrite(kFlashLedcChannel, 0);
  configTime(FLASH_TZ_OFFSET_SECONDS, 0, NTP_SERVER);
  Serial.printf("Flash LED enabled: pin=%d brightness=%d%% night_window=%02d:00-%02d:00 (UTC offset %+d s)\n",
                FLASH_LED_PIN,
                FLASH_LED_BRIGHTNESS_PCT,
                FLASH_NIGHT_START_HOUR,
                FLASH_NIGHT_END_HOUR,
                FLASH_TZ_OFFSET_SECONDS);
}

bool isFlashWindowNow() {
  const time_t now = time(nullptr);
  if (now < 1700000000) {
    return false;  // clock not synced yet; skip flash rather than guess
  }
  struct tm local = {};
  localtime_r(&now, &local);
  const int hour = local.tm_hour;
  if (FLASH_NIGHT_START_HOUR == FLASH_NIGHT_END_HOUR) {
    return true;  // equal hours mean flash around the clock
  }
  if (FLASH_NIGHT_START_HOUR < FLASH_NIGHT_END_HOUR) {
    return hour >= FLASH_NIGHT_START_HOUR && hour < FLASH_NIGHT_END_HOUR;
  }
  return hour >= FLASH_NIGHT_START_HOUR || hour < FLASH_NIGHT_END_HOUR;
}
#endif

void captureAndUploadWebSocketFrame() {
  if (!wifiConnected || !websocketConnected) {
    return;
  }
  if (CAPTURE_FRAME_LIMIT > 0 && websocketFramesSent >= CAPTURE_FRAME_LIMIT) {
    return;
  }

#if FLASH_LED_ENABLED
  const bool flashOn = isFlashWindowNow();
  if (flashOn) {
    ledcWrite(kFlashLedcChannel, (255 * FLASH_LED_BRIGHTNESS_PCT) / 100);
    // fb_count=1 + GRAB_WHEN_EMPTY queues a frame captured before the LED
    // went on — discard it so the uploaded frame is actually lit.
    camera_fb_t *stale = esp_camera_fb_get();
    if (stale != nullptr) {
      esp_camera_fb_return(stale);
    }
    delay(FLASH_LED_WARMUP_MS);
  }
#endif
  camera_fb_t *fb = esp_camera_fb_get();
#if FLASH_LED_ENABLED
  if (flashOn) {
    ledcWrite(kFlashLedcChannel, 0);
  }
#endif
  if (fb == nullptr) {
    jpegCaptureSucceeded = false;
    websocketUploadSucceeded = false;
    Serial.println("WebSocket frame capture failed: esp_camera_fb_get returned null");
    return;
  }

  jpegCaptureSucceeded = (fb->format == PIXFORMAT_JPEG && fb->len > 0);
  if (!jpegCaptureSucceeded) {
    websocketUploadSucceeded = false;
    Serial.println("WebSocket upload skipped: captured frame is not a non-empty JPEG");
    esp_camera_fb_return(fb);
    return;
  }

  websocketUploadSucceeded = webSocket.sendBIN(fb->buf, fb->len);
  if (websocketUploadSucceeded) {
    ++websocketFramesSent;
  }
  Serial.printf("WebSocket frame upload: %s size=%u frames_sent=%lu\n",
                websocketUploadSucceeded ? "queued" : "failed",
                fb->len,
                static_cast<unsigned long>(websocketFramesSent));
  esp_camera_fb_return(fb);
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
#elif defined(WEBSOCKET_UPLOAD_SMOKE_TEST)
  setupWebSocketUpload();
#if FLASH_LED_ENABLED
  setupFlashLed();
#endif
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
#ifdef WEBSOCKET_UPLOAD_SMOKE_TEST
  webSocket.loop();
  if (now - lastCaptureMs >= CAPTURE_INTERVAL_MS) {
    lastCaptureMs = now;
    captureAndUploadWebSocketFrame();
  }
#endif
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
#elif defined(WEBSOCKET_UPLOAD_SMOKE_TEST)
    Serial.printf("alive: millis=%lu wifi=%s ws=%s camera_init=%s jpeg_capture=%s ws_upload=%s frames_sent=%lu free_heap=%u free_psram=%u\n",
                  static_cast<unsigned long>(now),
                  wifiConnected ? "ok" : "failed",
                  websocketConnected ? "connected" : "disconnected",
                  cameraInitSucceeded ? "ok" : "failed",
                  jpegCaptureSucceeded ? "ok" : "failed",
                  websocketUploadSucceeded ? "ok" : "failed",
                  static_cast<unsigned long>(websocketFramesSent),
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
