FROM python:3.12-slim

WORKDIR /app
COPY index.html server.py ./
COPY assets ./assets

ENV HOST=0.0.0.0
ENV PORT=8000
ENV YSYX_DB_FILE=/data/ysyx_state.sqlite3

EXPOSE 8000
CMD ["python", "server.py"]
