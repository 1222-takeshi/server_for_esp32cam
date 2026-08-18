# syntax=docker/dockerfile:1
FROM python:3.11-slim

WORKDIR /app

RUN useradd --create-home --shell /bin/sh appuser

# Which pyproject.toml extras to install into the image. Defaults to
# `server` only (gunicorn/gevent) so the common no-detection deployment
# stays small. Pass `--build-arg PIP_EXTRAS=server,ml` to also bundle the
# TFLite/numpy stack needed for `ESP32CAM_DETECTOR=tflite_cpu` -- see
# docs/deployment.md and #349 (setting that env var without this build arg
# installs a server that starts fine but raises at first inference).
ARG PIP_EXTRAS=server

COPY pyproject.toml ./
# Install dependencies against a placeholder package first, so this layer only
# invalidates when pyproject.toml changes rather than on every script/ edit.
# setuptools needs a real `script` directory to discover the package, but its
# content doesn't matter yet -- the actual code is copied in below and, since
# gunicorn inserts the working directory at the front of sys.path, /app/script
# shadows this placeholder at runtime.
RUN mkdir -p script && touch script/__init__.py
RUN --mount=type=cache,target=/root/.cache/pip pip install ".[${PIP_EXTRAS}]"
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
