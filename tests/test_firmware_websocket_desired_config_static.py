"""Guard the WebSocket firmware wiring that PlatformIO compilation cannot exercise."""

from pathlib import Path


SOURCE = (
    Path(__file__).resolve().parents[1]
    / "firmware"
    / "esp32cam-http-upload"
    / "src"
    / "main.cpp"
).read_text(encoding="utf-8")


def test_websocket_boot_and_loop_poll_desired_config() -> None:
    setup = SOURCE.split("void setup() {", 1)[1].split("void loop() {", 1)[0]
    loop = SOURCE.split("void loop() {", 1)[1]

    assert "setupWebSocketUpload();" in setup
    assert "pollDesiredConfig();" in setup.split("setupWebSocketUpload();", 1)[1]
    assert "now - lastDesiredConfigPollMs >= pollIntervalMs" in loop
    assert "pollDesiredConfig();" in loop
    assert "desiredConfig.captureIntervalSeconds * 1000UL" in loop
    assert "CAPTURE_INTERVAL_MS" in loop


def test_restart_and_ota_require_successful_ack_and_are_not_replayed() -> None:
    polling = SOURCE.split("bool pollDesiredConfig() {", 1)[1].split(
        "void runRequestedOtaWindow() {", 1
    )[0]
    assert polling.index("const bool acknowledged = sendDesiredConfigAck(version, true, message);") < polling.index(
        "desiredConfigVersion = version;"
    )
    assert polling.index("const bool acknowledged = sendDesiredConfigAck(version, true, message);") < polling.index(
        "restartGate.recordAck(acknowledged"
    )
    assert polling.index("desiredConfigVersion = version;") < polling.index(
        "lastRestartHandledVersion = desiredConfigVersion;"
    )
    assert "lastOtaHandledVersion = desiredConfigVersion;" in polling
    assert "RTC_DATA_ATTR uint32_t lastRestartHandledVersion" in SOURCE
    assert "uint32_t desiredConfigVersion = 0;" in SOURCE
    loop = SOURCE.split("void loop() {", 1)[1]
    assert "restartGate.consumeIfReady(requestedOtaWindowActive, &restartVersion)" in loop
    assert "desiredConfig.restartRequested && desiredConfigVersion != lastRestartHandledVersion" not in loop


def test_ota_requires_password_and_has_bounded_websocket_window() -> None:
    assert 'message = "ota_requested_but_ota_password_empty"' in SOURCE
    loop = SOURCE.split("void loop() {", 1)[1]
    assert "if (requestedOtaWindowActive)" in loop
    assert "requestedOtaWindowStartedMs >= requestedOtaWindowDurationMs" in loop
    assert "ArduinoOTA.end();" in loop
    assert 'message = "ota_window_already_active"' in SOURCE


def test_websocket_poll_bounds_connect_and_read_and_rejects_missing_sensor() -> None:
    timeouts = SOURCE.split("void setDesiredConfigTimeouts(HTTPClient &http) {", 1)[1].split(
        "bool sendDesiredConfigAck", 1
    )[0]
    assert "http.setConnectTimeout(DESIRED_CONFIG_WS_TIMEOUT_MS);" in timeouts
    assert "http.setTimeout(DESIRED_CONFIG_WS_TIMEOUT_MS);" in timeouts
    assert SOURCE.count("setDesiredConfigTimeouts(http);") == 2
    assert 'if (sensor == nullptr) {\n    message = "camera_apply_failed";' in SOURCE


def test_ota_initialization_precedes_success_ack() -> None:
    polling = SOURCE.split("bool pollDesiredConfig() {", 1)[1].split(
        "void runRequestedOtaWindow() {", 1
    )[0]
    assert polling.index("if (otaPrepared && !setupArduinoOta())") < polling.index(
        "const bool acknowledged = sendDesiredConfigAck(version, true, message);"
    )
    assert 'sendDesiredConfigAck(version, false, desiredConfigMessage);' in polling
    assert "ArduinoOTA.end();" in polling
    assert "bool setupArduinoOta()" in SOURCE
