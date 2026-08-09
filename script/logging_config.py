from __future__ import annotations

import logging

from flask import Flask


def configure_logging(app: Flask) -> None:
    """Configure JSON or human-readable logging based on LOG_FORMAT config."""
    retention_logger = logging.getLogger("script.retention")
    retention_logger.setLevel(logging.INFO)
    if not any(
        getattr(handler, "_esp32cam_retention_handler", False)
        for handler in retention_logger.handlers
    ):
        retention_handler = logging.StreamHandler()
        retention_handler._esp32cam_retention_handler = True
        retention_handler.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
        retention_logger.addHandler(retention_handler)
    # caplog captures propagated records in tests; production uses the
    # dedicated stderr handler so Gunicorn/journald receives only scheduler
    # records at INFO level.
    retention_logger.propagate = bool(app.config.get("TESTING", False))

    log_format = app.config.get("LOG_FORMAT", "text")
    if log_format != "json":
        return

    try:
        from pythonjsonlogger import jsonlogger  # type: ignore[import-untyped]
    except ImportError:  # pragma: no cover
        app.logger.warning("python-json-logger not installed; falling back to text logging")
        return

    formatter = jsonlogger.JsonFormatter(
        fmt="%(asctime)s %(levelname)s %(name)s %(message)s",
        rename_fields={"asctime": "timestamp", "levelname": "level", "name": "component"},
    )

    root_logger = logging.getLogger()
    for handler in root_logger.handlers:
        handler.setFormatter(formatter)

    for handler in app.logger.handlers:
        handler.setFormatter(formatter)
    for handler in retention_logger.handlers:
        if getattr(handler, "_esp32cam_retention_handler", False):
            handler.setFormatter(formatter)
