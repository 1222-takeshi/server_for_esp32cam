# syntax=docker/dockerfile:1
FROM python:3.11-slim

WORKDIR /app

RUN useradd --create-home --shell /bin/sh appuser

COPY pyproject.toml ./
# Install dependencies against a placeholder package first, so this layer only
# invalidates when pyproject.toml changes rather than on every script/ edit.
# setuptools needs a real `script` directory to discover the package, but its
# content doesn't matter yet -- the actual code is copied in below and, since
# gunicorn inserts the working directory at the front of sys.path, /app/script
# shadows this placeholder at runtime.
RUN mkdir -p script && touch script/__init__.py
RUN --mount=type=cache,target=/root/.cache/pip pip install ".[server]"
RUN rm -rf script

COPY script/ ./script/
COPY templates/ ./templates/
COPY static/ ./static/

RUN mkdir -p uploads data && chown -R appuser:appuser /app

USER appuser

ENV ESP32CAM_UPLOAD_DIR=/app/uploads \
    ESP32CAM_DB_PATH=/app/data/esp32cam.sqlite3 \
    PORT=8080

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:${PORT}/healthz')" || exit 1

CMD gunicorn -k gevent -w 1 --timeout 120 -b "0.0.0.0:${PORT}" script.server:app
