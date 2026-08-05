from __future__ import annotations

import functools
import hmac

import logging

from flask import Flask, current_app, jsonify, request

_logger = logging.getLogger(__name__)


def validate_auth_config(app: Flask) -> None:
    """Normalise authentication settings and reject ambiguous credentials."""
    for key in ("DEVICE_API_KEY", "ADMIN_TOKEN"):
        value = app.config.get(key, "")
        stripped = value.strip()
        if value and not stripped:
            raise ValueError(f"Config {key} must not be whitespace-only")
        elif stripped != value:
            app.config[key] = stripped

    shared_key_device_id = app.config.get("SHARED_KEY_DEVICE_ID", "")
    if isinstance(shared_key_device_id, str):
        app.config["SHARED_KEY_DEVICE_ID"] = shared_key_device_id.strip()
    else:
        app.config["SHARED_KEY_DEVICE_ID"] = ""

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


def _device_api_key_value_set(per_device: dict[str, str]) -> frozenset[str]:
    """O(1)-amortized membership set backing check_device_api_key()'s
    coarse "is this key valid for *some* device" pre-check.

    check_device_api_key() previously ran an O(n_devices) hmac.compare_digest
    scan over every configured per-device key on *every* call -- and it is
    called on every /upload, /api/status_update and /api/log_error request
    (via require_device_auth), plus once more per request just to pick the
    upload rate-limit bucket key (routes._upload_rate_limit_key()), so a
    deployment with many cameras paid that scan twice per request (#205 /
    #226). Building a frozenset of the configured keys is O(n_devices) once;
    membership tests against it are O(1) average afterwards.

    This intentionally trades the per-key hmac.compare_digest() used here
    for hash-based set membership: Python's string hashing (SipHash, salted
    per-process) processes the whole key in roughly length-proportional
    time regardless of content, so it does not reintroduce a
    content-dependent timing oracle the way a naive `==` scan would. The
    check that actually authorizes an action for a specific device --
    check_device_api_key_for_device() -- still does a single
    hmac.compare_digest() against exactly that device's key, so the
    security-sensitive comparison is unchanged; this function only backs
    the coarse existence check used for fail-fast auth and rate-limit
    bucketing.

    The cache is stored on the Flask app (current_app.extensions) keyed by
    the `per_device` dict object's *identity*, not a copy of it or its
    id(). That means it is rebuilt exactly when
    app.config["DEVICE_API_KEYS"] is reassigned to a new dict (including
    directly in tests, which bypass validate_auth_config()) and reused for
    the common case where the same dict object serves every request.
    Keeping a reference to the dict itself (rather than caching by id())
    also avoids a false cache hit if an old dict were garbage collected and
    a new, unrelated one happened to reuse the same address.
    """
    cached = current_app.extensions.get("_device_api_key_value_set")
    if cached is not None and cached[0] is per_device:
        return cached[1]
    value_set = frozenset(per_device.values())
    current_app.extensions["_device_api_key_value_set"] = (per_device, value_set)
    return value_set


def check_device_api_key(provided: str | None) -> bool:
    """Return True if device auth passes (key matches or auth is disabled)."""
    required = current_app.config.get("DEVICE_API_KEY", "")
    per_device = current_app.config.get("DEVICE_API_KEYS", {}) or {}
    if per_device:
        return bool(provided) and provided in _device_api_key_value_set(per_device)
    if not required:
        return True
    if not provided:
        return False
    return _constant_time_equal(required, provided)


def check_device_api_key_for_device(device_id: str, provided: str | None) -> bool:
    """Return True when the provided device API key may act as this device.

    Per-device keys take precedence when configured. The legacy shared-key mode
    is intentionally restricted to one explicitly configured device ID so a
    valid shared key cannot create or impersonate arbitrary camera identities.
    """
    per_device = current_app.config.get("DEVICE_API_KEYS", {}) or {}
    if per_device:
        required = per_device.get(device_id)
        return bool(required and provided and _constant_time_equal(str(required), provided))
    if not check_device_api_key(provided):
        return False
    if not current_app.config.get("DEVICE_API_KEY", ""):
        return True
    expected_device_id = current_app.config.get("SHARED_KEY_DEVICE_ID", "")
    return bool(expected_device_id) and device_id == expected_device_id


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
