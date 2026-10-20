FROM python:3.12-slim
LABEL org.opencontainers.image.source="https://github.com/denveronly/umbrel" \
      org.opencontainers.image.description="Withings → Garmin Connect sync with a web UI (wraps withings-sync)"
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DATA_DIR=/data HOME=/data TZ=Europe/Kyiv
RUN apt-get update && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app/ .
VOLUME /data
EXPOSE 8080
# One worker on purpose: the scheduler thread and a pending Garmin MFA login live in its memory
CMD ["gunicorn", "-b", "0.0.0.0:8080", "-w", "1", "--threads", "4", "--timeout", "120", "app:app"]
