from __future__ import annotations

import functools
import hmac

from flask import current_app, jsonify, request


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
