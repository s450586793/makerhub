FROM node:20-slim AS frontend-build

WORKDIR /frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
COPY app/static/css/app.css /app/static/css/app.css
RUN npm run build


FROM python:3.11-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1
ENV MAKERHUB_CONFIG_DIR=/app/config
ENV MAKERHUB_LOGS_DIR=/app/logs
ENV MAKERHUB_STATE_DIR=/app/state
ENV MAKERHUB_ARCHIVE_DIR=/app/archive
ENV MAKERHUB_LOCAL_DIR=/app/local

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN mkdir -p /app/config /app/logs /app/state /app/archive /app/local

COPY app ./app
COPY VERSION ./VERSION
COPY frontend/package.json ./frontend/package.json
COPY --from=frontend-build /frontend/dist ./frontend/dist

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
