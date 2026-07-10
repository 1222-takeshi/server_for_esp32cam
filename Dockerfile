FROM python:3.11-slim

WORKDIR /app

RUN useradd --create-home --shell /bin/sh appuser

COPY pyproject.toml ./
COPY script/ ./script/
RUN pip install --no-cache-dir . && pip install --no-cache-dir "gunicorn>=23,<27" "gevent>=24,<27"

COPY templates/ ./templates/

RUN mkdir -p uploads data && chown -R appuser:appuser /app

USER appuser

ENV ESP32CAM_UPLOAD_DIR=/app/uploads \
    ESP32CAM_DB_PATH=/app/data/esp32cam.sqlite3 \
    PORT=8080

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:${PORT}/healthz')" || exit 1

CMD gunicorn -k gevent -w 1 -b "0.0.0.0:${PORT}" script.server:app
