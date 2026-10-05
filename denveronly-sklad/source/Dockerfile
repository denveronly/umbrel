FROM python:3.12-slim
LABEL org.opencontainers.image.source="https://github.com/denveronly/umbrel" \
      org.opencontainers.image.description="Склады — аренда, коммунальные, акты"
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DATA_DIR=/data TZ=Europe/Kyiv
RUN apt-get update && apt-get install -y --no-install-recommends fonts-dejavu-core tzdata \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app/ .
# Leaflet кладём внутрь образа, чтобы карта работала без интернета
ADD https://unpkg.com/leaflet@1.9.4/dist/leaflet.js https://unpkg.com/leaflet@1.9.4/dist/leaflet.css static/vendor/
ADD https://unpkg.com/leaflet@1.9.4/dist/images/marker-icon.png https://unpkg.com/leaflet@1.9.4/dist/images/marker-shadow.png static/vendor/images/
RUN chmod -R a+r static/vendor
VOLUME /data
EXPOSE 8080
CMD ["gunicorn", "-b", "0.0.0.0:8080", "-w", "2", "--timeout", "120", "app:app"]
