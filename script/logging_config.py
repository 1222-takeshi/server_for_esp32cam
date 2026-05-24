from __future__ import annotations

import logging

from flask import Flask


def configure_logging(app: Flask) -> None:
    """Configure JSON or human-readable logging based on LOG_FORMAT config."""
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
