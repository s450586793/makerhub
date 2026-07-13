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
    && apt-get install -y --no-install-recommends \
        chromium \
        curl \
        libarchive-tools \
        nodejs \
        libnss3 \
        libnspr4 \
        libatk1.0-0 \
        libatk-bridge2.0-0 \
        libcups2 \
        libdbus-1-3 \
        libdrm2 \
        libxkbcommon0 \
        libatspi2.0-0 \
        libxcomposite1 \
        libxdamage1 \
        libxfixes3 \
        libxrandr2 \
        libgbm1 \
        libpango-1.0-0 \
        libcairo2 \
        libasound2 \
        libx11-xcb1 \
        libfontconfig1 \
        libx11-6 \
        libxcb1 \
        libxext6 \
        libxshmfence1 \
        libglib2.0-0 \
        libgtk-3-0 \
        libpangocairo-1.0-0 \
        libcairo-gobject2 \
        libgdk-pixbuf-2.0-0 \
        libxss1 \
        libxtst6 \
        fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN mkdir -p /app/config/config /app/config/logs /app/config/state /app/data /app/data/local

COPY app ./app
COPY compose.yaml ./compose.yaml
COPY VERSION ./VERSION
COPY docker/entrypoint.sh ./docker/entrypoint.sh
COPY frontend/package.json ./frontend/package.json
COPY --from=frontend-build /frontend/node_modules ./frontend/node_modules
COPY --from=frontend-build /frontend/dist ./frontend/dist
RUN chmod +x /app/docker/entrypoint.sh

EXPOSE 8000
ENTRYPOINT ["/app/docker/entrypoint.sh"]
CMD ["app"]
