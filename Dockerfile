FROM python:3.12-slim

WORKDIR /app
COPY index.html server.py ./
COPY assets ./assets

ENV HOST=0.0.0.0 \
    PORT=8000 \
    YSYX_DB_FILE=/data/ysyx-study-tracker.sqlite3 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN mkdir -p /data

EXPOSE 8000
VOLUME ["/data"]
CMD ["python", "server.py"]
