FROM python:3.11-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1
ENV MAKERHUB_CONFIG_DIR=/app/config
ENV MAKERHUB_LOGS_DIR=/app/logs
ENV MAKERHUB_STATE_DIR=/app/state
ENV MAKERHUB_ARCHIVE_DIR=/app/archive
ENV MAKERHUB_LOCAL_DIR=/app/local

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN mkdir -p /app/config /app/logs /app/state /app/archive /app/local

COPY app ./app

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
