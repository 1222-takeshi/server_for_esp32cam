from __future__ import annotations

import re

# `device_id` (upload/status/log/config endpoints) and `camera_id` (video/
# stream/listing endpoints) are conceptually the same identifier — a single
# device is both the uploader of a `device_id` and the `camera_id` under
# which its files/videos are later queried. They must therefore share one
# max length. This used to be 128 in routes.py's device_id validator but 64
# here, so a device_id accepted at upload/status-update time (up to 128
# chars) could be rejected as "Camera not found" by every camera_id-keyed
# endpoint (video list/upload, stream, upload-metrics) once it exceeded 64
# chars. Keep 128 as the single source of truth (matches docs/openapi.yaml's
# device_id/camera_id `maxLength: 128` entries) so it can't drift again.
CAMERA_ID_MAX_LENGTH = 128
_CAMERA_ID_RE = re.compile(rf"[A-Za-z0-9_\-]{{1,{CAMERA_ID_MAX_LENGTH}}}")


def is_valid_camera_id(value: str) -> bool:
    """True when `value` matches the shared device_id/camera_id character/length rules.

    `routes.py` and `storage.py` previously each carried their own regex for
    this; kept in one place so future changes to the allowed character set
    or length can't drift between the two call sites. `routes.py`'s
    `_validate_device_id` also delegates here for the same reason.
    """
    return bool(value) and _CAMERA_ID_RE.fullmatch(value) is not None
