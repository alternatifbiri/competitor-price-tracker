FROM python:3.12-slim

WORKDIR /app

# Persistent data lives here. Mount a volume or the history is lost
# on every redeploy.
ENV DATA_DIR=/data \
    PYTHONUNBUFFERED=1 \
    TZ=UTC

RUN pip install --no-cache-dir "httpx>=0.27,<0.29"

COPY tracker.py config.json ./

RUN mkdir -p /data/logs /data/exports /data/reports
VOLUME ["/data"]

CMD ["python", "tracker.py", "--run"]
