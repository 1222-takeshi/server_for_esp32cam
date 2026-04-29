#include <Arduino.h>
#include <HTTPClient.h>
#include <WiFi.h>
#include <ESPmDNS.h>
#include <ArduinoOTA.h>
#include "esp_camera.h"
#include <time.h>
#include "config.h"

static const char *NTP_SERVER_1 = "pool.ntp.org";
static const char *NTP_SERVER_2 = "time.google.com";
static const uint32_t WIFI_CONNECT_TIMEOUT_MS = 15000;
static const uint32_t TIME_SYNC_TIMEOUT_MS = 10000;
static uint32_t lastOtaProgressLogMs = 0;


// AI Thinker ESP32-CAM pin map.
// Update this block if you are using a different board variant.
#define PWDN_GPIO_NUM     32
#define RESET_GPIO_NUM    -1
#define XCLK_GPIO_NUM      0
#define SIOD_GPIO_NUM     26
#define SIOC_GPIO_NUM     27

#define Y9_GPIO_NUM       35
#define Y8_GPIO_NUM       34
#define Y7_GPIO_NUM       39
#define Y6_GPIO_NUM       36
#define Y5_GPIO_NUM       21
#define Y4_GPIO_NUM       19
#define Y3_GPIO_NUM       18
#define Y2_GPIO_NUM        5
#define VSYNC_GPIO_NUM    25
#define HREF_GPIO_NUM     23
#define PCLK_GPIO_NUM     22

static bool connectWiFi();
static bool initCamera();
static bool syncTime();
static const char *getOtaHostname();
static void configureOta();
static void waitForOtaWindow();
static String buildCapturedAtUtc(time_t capturedAt);
static bool sendSnapshot(camera_fb_t *frameBuffer, time_t capturedAt);
static void finishCycle(const char *message);
static void goToDeepSleep();

void setup() {
  Serial.begin(115200);
  delay(200);
  Serial.println();
  Serial.println("ESP32-CAM snapshot uploader starting");

  if (!connectWiFi()) {
    finishCycle("Wi-Fi connection failed");
  }

  if (ENABLE_OTA) {
    configureOta();
    if (ENABLE_DEEP_SLEEP) {
      waitForOtaWindow();
    }
  }

  if (!syncTime()) {
    finishCycle("Time synchronization failed");
  }

  delay(1000);

  if (!initCamera()) {
    finishCycle("Camera initialization failed");
  }

  const time_t capturedAt = time(nullptr);
  camera_fb_t *frameBuffer = esp_camera_fb_get();
  if (frameBuffer == nullptr) {
    finishCycle("Failed to capture frame");
  }

  const bool uploaded = sendSnapshot(frameBuffer, capturedAt);
  esp_camera_fb_return(frameBuffer);

  finishCycle(uploaded ? "Upload completed successfully" : "Upload failed");
}

void loop() {
  delay(1000);
}

static bool connectWiFi() {
  Serial.printf("Connecting to Wi-Fi SSID: %s\n", WIFI_SSID);
  WiFi.mode(WIFI_STA);
  if (ENABLE_OTA) {
    WiFi.setHostname(getOtaHostname());
  }
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);

  const unsigned long start = millis();
  while (WiFi.status() != WL_CONNECTED) {
    delay(500);
    Serial.print(".");
    if (millis() - start > WIFI_CONNECT_TIMEOUT_MS) {
      Serial.println();
      Serial.println("Wi-Fi connection timeout");
      return false;
    }
  }

  Serial.println();
  Serial.print("Wi-Fi connected, IP address: ");
  Serial.println(WiFi.localIP());
  return true;
}

static bool initCamera() {
  camera_config_t config = {};
  config.ledc_channel = LEDC_CHANNEL_0;
  config.ledc_timer = LEDC_TIMER_0;
  config.pin_d0 = Y2_GPIO_NUM;
  config.pin_d1 = Y3_GPIO_NUM;
  config.pin_d2 = Y4_GPIO_NUM;
  config.pin_d3 = Y5_GPIO_NUM;
  config.pin_d4 = Y6_GPIO_NUM;
  config.pin_d5 = Y7_GPIO_NUM;
  config.pin_d6 = Y8_GPIO_NUM;
  config.pin_d7 = Y9_GPIO_NUM;
  config.pin_xclk = XCLK_GPIO_NUM;
  config.pin_pclk = PCLK_GPIO_NUM;
  config.pin_vsync = VSYNC_GPIO_NUM;
  config.pin_href = HREF_GPIO_NUM;
  config.pin_sccb_sda = SIOD_GPIO_NUM;
  config.pin_sccb_scl = SIOC_GPIO_NUM;
  config.pin_pwdn = PWDN_GPIO_NUM;
  config.pin_reset = RESET_GPIO_NUM;
  config.xclk_freq_hz = 20000000;
  config.pixel_format = PIXFORMAT_JPEG;

  // Keep defaults conservative so the example survives modest power rails
  // and stays below the server's 1 MiB default upload limit more reliably.
  if (psramFound()) {
    config.frame_size = FRAMESIZE_VGA;
    config.jpeg_quality = 15;
    config.fb_count = 1;
    config.fb_location = CAMERA_FB_IN_PSRAM;
    config.grab_mode = CAMERA_GRAB_WHEN_EMPTY;
  } else {
    config.frame_size = FRAMESIZE_QVGA;
    config.jpeg_quality = 20;
    config.fb_count = 1;
    config.fb_location = CAMERA_FB_IN_DRAM;
    config.grab_mode = CAMERA_GRAB_WHEN_EMPTY;
  }

  esp_err_t err = esp_camera_init(&config);
  if (err != ESP_OK) {
    Serial.printf("esp_camera_init failed: 0x%x\n", err);
    return false;
  }

  Serial.println("Camera initialized");
  return true;
}

static const char *getOtaHostname() {
  return OTA_HOSTNAME[0] != '\0' ? OTA_HOSTNAME : DEVICE_ID;
}

static void configureOta() {
  const char *hostname = getOtaHostname();

  ArduinoOTA.setPort(OTA_PORT);
  ArduinoOTA.setHostname(hostname);
  if (OTA_PASSWORD[0] != '\0') {
    ArduinoOTA.setPassword(OTA_PASSWORD);
  }

  ArduinoOTA
    .onStart([]() {
      Serial.println("OTA update started");
    })
    .onEnd([]() {
      Serial.println("\nOTA update finished");
    })
    .onProgress([](unsigned int progress, unsigned int total) {
      if (millis() - lastOtaProgressLogMs > 500) {
        Serial.printf("OTA progress: %u%%\n", progress / (total / 100));
        lastOtaProgressLogMs = millis();
      }
    })
    .onError([](ota_error_t error) {
      Serial.printf("OTA error[%u]\n", error);
    });

  ArduinoOTA.begin();

  Serial.printf("OTA ready on %s.local:%u\n", hostname, OTA_PORT);
  if (OTA_PASSWORD[0] == '\0') {
    Serial.println("OTA password is disabled; set OTA_PASSWORD before using OTA on a shared network");
  }
}

static void waitForOtaWindow() {
  if (OTA_WAIT_WINDOW_MS == 0) {
    return;
  }

  Serial.printf("Waiting %u ms for OTA before capture\n", OTA_WAIT_WINDOW_MS);
  const unsigned long start = millis();
  while (millis() - start < OTA_WAIT_WINDOW_MS) {
    ArduinoOTA.handle();
    delay(10);
  }
  Serial.println("OTA boot window closed");
}

static bool syncTime() {
  configTime(0, 0, NTP_SERVER_1, NTP_SERVER_2);
  Serial.println("Waiting for NTP time sync");

  struct tm timeInfo;
  const unsigned long start = millis();
  while (!getLocalTime(&timeInfo)) {
    delay(500);
    if (millis() - start > TIME_SYNC_TIMEOUT_MS) {
      return false;
    }
  }

  Serial.printf("Time synchronized: %04d-%02d-%02d %02d:%02d:%02d UTC\n",
                timeInfo.tm_year + 1900,
                timeInfo.tm_mon + 1,
                timeInfo.tm_mday,
                timeInfo.tm_hour,
                timeInfo.tm_min,
                timeInfo.tm_sec);
  return true;
}

static String buildCapturedAtUtc(time_t capturedAt) {
  struct tm timeInfo;
  gmtime_r(&capturedAt, &timeInfo);

  char buffer[32];
  strftime(buffer, sizeof(buffer), "%Y-%m-%dT%H:%M:%SZ", &timeInfo);
  return String(buffer);
}

static bool sendSnapshot(camera_fb_t *frameBuffer, time_t capturedAt) {
  if (frameBuffer == nullptr || frameBuffer->len == 0) {
    Serial.println("Invalid frame buffer");
    return false;
  }

  if (frameBuffer->len > SERVER_MAX_UPLOAD_BYTES) {
    Serial.printf("Captured frame is %u bytes, above the default server limit of %u bytes\n",
                  static_cast<unsigned>(frameBuffer->len),
                  static_cast<unsigned>(SERVER_MAX_UPLOAD_BYTES));
    return false;
  }

  WiFiClient client;
  HTTPClient http;

  if (!http.begin(client, SERVER_URL)) {
    Serial.println("Failed to start HTTP client");
    return false;
  }

  const String capturedAtHeader = buildCapturedAtUtc(capturedAt);
  http.addHeader("Content-Type", "image/jpeg");
  http.addHeader("X-Device-Id", DEVICE_ID);
  http.addHeader("X-Captured-At", capturedAtHeader);

  Serial.printf("Uploading %u bytes to %s with captured_at=%s\n",
                static_cast<unsigned>(frameBuffer->len),
                SERVER_URL,
                capturedAtHeader.c_str());

  const int responseCode = http.POST(frameBuffer->buf, frameBuffer->len);
  if (responseCode <= 0) {
    Serial.printf("HTTP POST failed: %s\n", http.errorToString(responseCode).c_str());
    http.end();
    return false;
  }

  Serial.printf("HTTP response code: %d\n", responseCode);
  const String responseBody = http.getString();
  Serial.println("Response body:");
  Serial.println(responseBody);
  http.end();
  return responseCode >= 200 && responseCode < 300;
}

static void finishCycle(const char *message) {
  Serial.println(message);

  if (ENABLE_DEEP_SLEEP) {
    WiFi.disconnect(true, true);
    WiFi.mode(WIFI_OFF);
    goToDeepSleep();
  }

  if (ENABLE_OTA) {
    Serial.println("Keeping Wi-Fi alive for OTA updates");
    while (true) {
      ArduinoOTA.handle();
      delay(10);
    }
  }

  WiFi.disconnect(true, true);
  WiFi.mode(WIFI_OFF);
  while (true) {
    delay(1000);
  }
}

static void goToDeepSleep() {
  Serial.printf("Entering deep sleep for %llu seconds\n", DEEP_SLEEP_SECONDS);
  esp_sleep_enable_timer_wakeup(DEEP_SLEEP_SECONDS * 1000000ULL);
  Serial.flush();
  esp_deep_sleep_start();
}
