from __future__ import annotations

import re

CAMERA_ID_MAX_LENGTH = 64
_CAMERA_ID_RE = re.compile(rf"[A-Za-z0-9_\-]{{1,{CAMERA_ID_MAX_LENGTH}}}")


def is_valid_camera_id(value: str) -> bool:
    """True when `value` matches the shared camera_id character/length rules.

    `routes.py` and `storage.py` previously each carried their own regex for
    this; kept in one place so future changes to the allowed character set
    or length can't drift between the two call sites.
    """
    return bool(value) and _CAMERA_ID_RE.fullmatch(value) is not None
