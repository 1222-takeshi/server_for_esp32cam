#include <Arduino.h>
#include <ArduinoJson.h>
#include <ArduinoOTA.h>
#include <WebSocketsClient.h>
#include <WiFi.h>
#include <esp_camera.h>
#include <esp_system.h>
#include <esp_wifi.h>
#include <esp_timer.h>
#include <cstring>

#include "high_rate_frame.h"
#include "high_rate_schedule.h"
#include "operating_profile.h"

#if __has_include("esp32cam_config.h")
#include "esp32cam_config.h"
#else
#define WIFI_SSID ""
#define WIFI_PASSWORD ""
#define DEVICE_ID "esp32cam-01"
#define DEVICE_API_KEY ""
#define WS_UPLOAD_HOST ""
#define OTA_HOSTNAME "esp32cam-01"
#define OTA_PORT 3232
#define OTA_PASSWORD ""
#endif

#ifndef HIGH_RATE_WS_PORT
#define HIGH_RATE_WS_PORT 8766
#endif
#ifndef HIGH_RATE_WS_PATH
#define HIGH_RATE_WS_PATH "/ws/cameras/" DEVICE_ID "/live"
#endif
#ifndef HIGH_RATE_INTERVAL_MS
#define HIGH_RATE_INTERVAL_MS 100
#endif
static_assert(high_rate::supportedCaptureInterval(HIGH_RATE_INTERVAL_MS),
              "HIGH_RATE_INTERVAL_MS must be 95, 100, 190 or 200");

namespace {
constexpr bool profilesEnabled =
#ifdef OPERATING_PROFILES
    true;
#else
    false;
#endif
constexpr size_t headerSize = profilesEnabled ? operating::kHeaderSize : high_rate::kHeaderSize;
static_assert(WEBSOCKETS_MAX_HEADER_SIZE == 14, "Recheck WebSockets headerToPayload layout");
operating::Profile profile;
uint32_t lastServerMs = 0;
WebSocketsClient webSocket;
uint8_t *packet = nullptr;
uint64_t bootId = 0;
uint64_t frameSeq = 0;
uint64_t lastAcquiredUs = 0;
uint32_t lastAttemptMs = 0;
uint32_t lastReconnectMs = 0;
uint32_t lastStatusMs = 0;
uint32_t attempts = 0;
uint32_t captured = 0;
uint32_t captureFailures = 0;
uint32_t stale = 0;
uint32_t sent = 0;
uint32_t sendFailures = 0;
bool connected = false;
bool otaStarted = false;
bool ready = false;
bool websocketEnabled = false;

camera_config_t cameraConfig() {
  camera_config_t c = {};
  c.ledc_channel = LEDC_CHANNEL_0;
  c.ledc_timer = LEDC_TIMER_0;
  c.pin_d0 = 5; c.pin_d1 = 18; c.pin_d2 = 19; c.pin_d3 = 21;
  c.pin_d4 = 36; c.pin_d5 = 39; c.pin_d6 = 34; c.pin_d7 = 35;
  c.pin_xclk = 0; c.pin_pclk = 22; c.pin_vsync = 25; c.pin_href = 23;
  c.pin_sccb_sda = 26; c.pin_sccb_scl = 27;
  c.pin_pwdn = 32; c.pin_reset = -1;
  c.xclk_freq_hz = 20000000;
  c.pixel_format = PIXFORMAT_JPEG;
  c.frame_size = FRAMESIZE_QVGA;
  c.jpeg_quality = 12;
  c.fb_count = 2;
  c.fb_location = CAMERA_FB_IN_PSRAM;
  c.grab_mode = CAMERA_GRAB_LATEST;
  return c;
}

void onWebSocketEvent(WStype_t type, uint8_t *payload, size_t length) {
  if (type == WStype_CONNECTED) {
    connected = true;
    lastServerMs = millis();
    if (profilesEnabled) {
      profile.reset(esp_timer_get_time());
      char hello[220];
      snprintf(hello, sizeof(hello),
          "{\"type\":\"profile.hello\",\"schema\":2,\"boot_id\":%llu,\"generation_floor\":%llu,\"profiles\":[\"eco\",\"tracking\"]}",
          static_cast<unsigned long long>(bootId), static_cast<unsigned long long>(profile.highWater));
      webSocket.sendTXT(hello);
    }
  } else if (type == WStype_DISCONNECTED || type == WStype_ERROR) {
    connected = false;
    if (profilesEnabled) profile.reset(esp_timer_get_time());
  } else if (type == WStype_TEXT && length <= 256) {
    JsonDocument doc;
    if (deserializeJson(doc, payload, length) != DeserializationError::Ok ||
        !doc["type"].is<const char *>()) return;
    if (profilesEnabled && strcmp(doc["type"].as<const char *>(), "profile.set") == 0) {
      if (doc.size() != 4 || !doc["boot_id"].is<uint64_t>() ||
          doc["boot_id"].as<uint64_t>() != bootId || !doc["generation"].is<uint64_t>() ||
          !doc["mode"].is<const char *>()) return;
      const JsonString mode = doc["mode"].as<JsonString>();
      const bool track = mode == "tracking";
      const bool changed = doc["generation"].as<uint64_t>() != profile.generation;
      if ((!track && mode != "eco") ||
          !profile.apply(doc["generation"].as<uint64_t>(), track, esp_timer_get_time())) return;
      lastServerMs = millis();
      if (changed) lastAttemptMs = millis();
      char ack[220];
      snprintf(ack, sizeof(ack),
          "{\"type\":\"profile.ack\",\"boot_id\":%llu,\"generation\":%llu,\"mode\":\"%s\",\"applied_us\":%llu}",
          static_cast<unsigned long long>(bootId),
          static_cast<unsigned long long>(profile.generation), track ? "tracking" : "eco",
          static_cast<unsigned long long>(profile.appliedUs));
      webSocket.sendTXT(ack);
      return;
    }
    if (
        strcmp(doc["type"].as<const char *>(), "clock.ping") != 0 ||
        !doc["id"].is<const char *>()) {
      return;
    }
    const JsonString nonce = doc["id"].as<JsonString>();
    const char *id = nonce.c_str();
    const size_t idLength = nonce.size();
    if (idLength == 0 || idLength > 32) return;
    for (size_t i = 0; i < idLength; ++i) {
      const char c = id[i];
      if (!((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f') ||
            (c >= 'A' && c <= 'F'))) return;
    }
    if (strlen(id) != idLength) return;
    lastServerMs = millis();
    char response[160];
    const int n = snprintf(response, sizeof(response),
                           "{\"type\":\"clock.pong\",\"id\":\"%s\",\"boot_id\":%llu,\"device_us\":%llu}",
                           id, static_cast<unsigned long long>(bootId),
                           static_cast<unsigned long long>(esp_timer_get_time()));
    if (n > 0 && static_cast<size_t>(n) < sizeof(response)) webSocket.sendTXT(response);
  }
}

void sendStatus() {
  char response[240];
  const int n = snprintf(response, sizeof(response),
      "{\"type\":\"capture.status\",\"boot_id\":%llu,\"attempts\":%lu,\"captured\":%lu,"
      "\"capture_failures\":%lu,\"stale\":%lu,\"sent\":%lu,\"send_failures\":%lu}",
      static_cast<unsigned long long>(bootId),
      static_cast<unsigned long>(attempts), static_cast<unsigned long>(captured),
      static_cast<unsigned long>(captureFailures), static_cast<unsigned long>(stale),
      static_cast<unsigned long>(sent), static_cast<unsigned long>(sendFailures));
  if (n > 0 && static_cast<size_t>(n) < sizeof(response)) webSocket.sendTXT(response);
}

void captureAndSend() {
  ++attempts;
  camera_fb_t *fb = esp_camera_fb_get();
  if (fb == nullptr) {
    ++captureFailures;
    return;
  }
  const uint64_t capturedUs = static_cast<uint64_t>(fb->timestamp.tv_sec) * 1000000ULL +
                              static_cast<uint64_t>(fb->timestamp.tv_usec);
  if (!high_rate::isFresh(capturedUs, lastAcquiredUs) ||
      (profilesEnabled && !profile.accepts(capturedUs))) {
    ++stale;
    esp_camera_fb_return(fb);
    return;
  }
  lastAcquiredUs = capturedUs;
  ++frameSeq;
  ++captured;
  if (fb->format != PIXFORMAT_JPEG || fb->len == 0 || fb->len > high_rate::kMaxJpegBytes) {
    ++captureFailures;
    esp_camera_fb_return(fb);
    return;
  }
  const size_t jpegLength = fb->len;
  // Reserve library headroom so WS header + ECF header + JPEG share one write.
  uint8_t *const payload = packet + WEBSOCKETS_MAX_HEADER_SIZE;
  memcpy(payload + headerSize, fb->buf, jpegLength);
  esp_camera_fb_return(fb);
  const uint64_t sentUs = static_cast<uint64_t>(esp_timer_get_time());
  const bool encoded = profilesEnabled ?
      operating::encodeHeader(payload, bootId, frameSeq, capturedUs, sentUs, jpegLength, profile.generation) :
      high_rate::encodeHeader(payload, bootId, frameSeq, capturedUs, sentUs, jpegLength);
  if (!encoded) {
    ++sendFailures;
    return;
  }
  const size_t length = headerSize + jpegLength;
  // Small frames retain the library's existing internal-buffer/masking path.
  const bool inlineHeader = length >= 1400;
  if (webSocket.sendBIN(inlineHeader ? packet : payload, length, inlineHeader)) ++sent;
  else ++sendFailures;
}
}  // namespace

void setup() {
  Serial.begin(115200);
  delay(100);
  bootId = (static_cast<uint64_t>(esp_random()) << 32) | esp_random();
  if (bootId == 0) bootId = 1;
  if (strlen(WIFI_SSID) == 0 || strlen(OTA_PASSWORD) == 0) {
    Serial.println("High-rate capture disabled: Wi-Fi and OTA password are required.");
    return;
  }
  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);
  wifi_ps_type_t sleepMode;
  const bool lowLatencyWiFi = esp_wifi_get_ps(&sleepMode) == ESP_OK &&
                             sleepMode == WIFI_PS_NONE;
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  ready = true;  // The OTA recovery loop remains active if capture setup fails.
  if (!lowLatencyWiFi) {
    Serial.println("High-rate capture disabled: Wi-Fi power-save remains enabled; OTA remains available.");
    return;
  }
  if (!psramFound()) {
    Serial.println("High-rate capture disabled: PSRAM is required; OTA remains available.");
    return;
  }
  const camera_config_t config = cameraConfig();
  if (esp_camera_init(&config) != ESP_OK) {
    Serial.println("High-rate camera initialization failed; OTA remains available.");
    return;
  }
  packet = static_cast<uint8_t *>(ps_malloc(WEBSOCKETS_MAX_HEADER_SIZE + headerSize + high_rate::kMaxJpegBytes));
  if (packet == nullptr) {
    Serial.println("High-rate packet allocation failed; OTA remains available.");
    esp_camera_deinit();
    return;
  }
  if (strlen(WS_UPLOAD_HOST) == 0 || strlen(DEVICE_API_KEY) == 0) {
    Serial.println("High-rate WebSocket disabled: host and device key are required.");
    return;
  }
  webSocket.setExtraHeaders((String("X-API-Key: ") + DEVICE_API_KEY).c_str());
  webSocket.begin(WS_UPLOAD_HOST, HIGH_RATE_WS_PORT, HIGH_RATE_WS_PATH);
  webSocket.onEvent(onWebSocketEvent);
  webSocket.setReconnectInterval(1000);
  websocketEnabled = true;
}

void loop() {
  if (!ready) { delay(100); return; }
  const uint32_t now = millis();
  if (WiFi.status() != WL_CONNECTED) {
    connected = false;
    if (now - lastReconnectMs >= 5000) {
      lastReconnectMs = now;
      WiFi.reconnect();
    }
    delay(2);
    return;
  }
  if (!otaStarted) {
    ArduinoOTA.setHostname(OTA_HOSTNAME);
    ArduinoOTA.setPort(OTA_PORT);
    ArduinoOTA.setPassword(OTA_PASSWORD);
    ArduinoOTA.begin();
    otaStarted = true;
  }
  ArduinoOTA.handle();
  if (websocketEnabled) webSocket.loop();
  if (profilesEnabled && connected && static_cast<uint32_t>(millis() - lastServerMs) >= 5000) {
    profile.reset(esp_timer_get_time());
    connected = false;
    webSocket.disconnect();
  }
  const uint32_t interval = profilesEnabled ? profile.intervalMs() : HIGH_RATE_INTERVAL_MS;
  if (connected && high_rate::captureDue(millis(), interval, lastAttemptMs)) {
    captureAndSend();
  }
  if (connected && now - lastStatusMs >= 1000) {
    lastStatusMs = now;
    sendStatus();
  }
  delay(1);
}
