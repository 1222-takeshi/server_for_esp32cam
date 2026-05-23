FROM python:3.11-slim

WORKDIR /app

RUN useradd --create-home --shell /bin/sh appuser

COPY pyproject.toml ./
RUN pip install --no-cache-dir -e .

COPY script/ ./script/
COPY templates/ ./templates/

RUN mkdir -p uploads data && chown -R appuser:appuser /app

USER appuser

ENV ESP32CAM_UPLOAD_DIR=/app/uploads \
    ESP32CAM_DB_PATH=/app/data/esp32cam.sqlite3 \
    PORT=8080

EXPOSE 8080

CMD ["python", "script/server.py"]
