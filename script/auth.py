from __future__ import annotations

import functools
import hmac

import logging

from flask import Flask, current_app, jsonify, request

_logger = logging.getLogger(__name__)


def validate_auth_config(app: Flask) -> None:
    """Warn and normalise whitespace-only keys at startup."""
    for key in ("DEVICE_API_KEY", "ADMIN_TOKEN"):
        value = app.config.get(key, "")
        stripped = value.strip()
        if value and not stripped:
            _logger.warning(
                "Config %s is set to whitespace-only — treating as disabled. "
                "Set a non-empty value to enable authentication.",
                key,
            )
            app.config[key] = ""
        elif stripped != value:
            app.config[key] = stripped

    device_keys = app.config.get("DEVICE_API_KEYS", {})
    if not isinstance(device_keys, dict):
        _logger.warning("Config DEVICE_API_KEYS is not a mapping — ignoring it.")
        app.config["DEVICE_API_KEYS"] = {}
    else:
        app.config["DEVICE_API_KEYS"] = {
            str(device_id).strip(): str(api_key).strip()
            for device_id, api_key in device_keys.items()
            if str(device_id).strip() and str(api_key).strip()
        }


def _constant_time_equal(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


def check_device_api_key(provided: str | None) -> bool:
    """Return True if device auth passes (key matches or auth is disabled)."""
    required = current_app.config.get("DEVICE_API_KEY", "")
    per_device = current_app.config.get("DEVICE_API_KEYS", {}) or {}
    if per_device:
        return bool(provided) and any(_constant_time_equal(str(key), provided) for key in per_device.values())
    if not required:
        return True
    if not provided:
        return False
    return _constant_time_equal(required, provided)


def check_device_api_key_for_device(device_id: str, provided: str | None) -> bool:
    """Return True when the provided device API key may act as this device."""
    per_device = current_app.config.get("DEVICE_API_KEYS", {}) or {}
    if per_device:
        required = per_device.get(device_id)
        return bool(required and provided and _constant_time_equal(str(required), provided))
    return check_device_api_key(provided)


def check_admin_token(provided: str | None) -> bool:
    """Return True if admin auth passes (token matches or auth is disabled)."""
    required = current_app.config.get("ADMIN_TOKEN", "")
    if not required:
        return True
    if not provided:
        return False
    return _constant_time_equal(required, provided)


def require_device_auth(f):
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        if not check_device_api_key(request.headers.get("X-API-Key")):
            return jsonify({"message": "Unauthorized"}), 401
        return f(*args, **kwargs)
    return decorated


def require_admin_auth(f):
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        auth_header = request.headers.get("Authorization", "")
        token = auth_header.removeprefix("Bearer ") if auth_header.startswith("Bearer ") else None
        if not check_admin_token(token):
            response = jsonify({"message": "Unauthorized"})
            response.headers["WWW-Authenticate"] = 'Bearer realm="ESP32CAM Admin"'
            return response, 401
        return f(*args, **kwargs)
    return decorated
