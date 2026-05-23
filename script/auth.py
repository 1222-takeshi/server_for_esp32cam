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


def _constant_time_equal(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


def check_device_api_key(provided: str | None) -> bool:
    """Return True if device auth passes (key matches or auth is disabled)."""
    required = current_app.config.get("DEVICE_API_KEY", "")
    if not required:
        return True
    if not provided:
        return False
    return _constant_time_equal(required, provided)


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
