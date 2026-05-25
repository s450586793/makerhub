FROM node:20-slim AS frontend-build

WORKDIR /frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
COPY app/static/css/app.css /app/static/css/app.css
RUN npm run build
RUN npm prune --omit=dev


FROM python:3.11-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1
ENV MAKERHUB_CONFIG_DIR=/app/config/config
ENV MAKERHUB_LOGS_DIR=/app/config/logs
ENV MAKERHUB_STATE_DIR=/app/config/state
ENV MAKERHUB_ARCHIVE_DIR=/app/data
ENV MAKERHUB_LOCAL_DIR=/app/data/local

RUN apt-get update \
    && apt-get install -y --no-install-recommends chromium curl libarchive-tools nodejs \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN scrapling install
RUN mkdir -p /app/config/config /app/config/logs /app/config/state /app/data /app/data/local

COPY app ./app
COPY VERSION ./VERSION
COPY docker/entrypoint.sh ./docker/entrypoint.sh
COPY frontend/package.json ./frontend/package.json
COPY --from=frontend-build /frontend/node_modules ./frontend/node_modules
COPY --from=frontend-build /frontend/dist ./frontend/dist
RUN chmod +x /app/docker/entrypoint.sh

EXPOSE 8000
ENTRYPOINT ["/app/docker/entrypoint.sh"]
CMD ["app"]
