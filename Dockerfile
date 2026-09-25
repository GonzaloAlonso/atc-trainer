FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    VISOR_HOST=0.0.0.0 \
    VISOR_PORT=8000 \
    VISOR_DATA_DIR=/data \
    VISOR_NAVDATA_DIR=/app/navdata

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Airports and navaids are static reference data: bake them into the image.
COPY scripts/ scripts/
RUN python scripts/fetch_navdata.py

COPY atc/ atc/
COPY public/ public/
COPY server.py .

# Recordings (SQLite) live on a volume so they survive container upgrades.
RUN useradd --system --uid 10001 --no-create-home visor \
    && mkdir -p /data && chown visor /data
USER visor
VOLUME ["/data"]

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/status', timeout=4)"

# Version metadata last, so a new release doesn't invalidate the cached layers above.
ARG VISOR_VERSION=dev
ARG VCS_REF=unknown
LABEL org.opencontainers.image.title="Visor ATC" \
      org.opencontainers.image.description="3D air traffic control simulator on OpenSky data, with a decision-AI API" \
      org.opencontainers.image.version="${VISOR_VERSION}" \
      org.opencontainers.image.revision="${VCS_REF}"
ENV VISOR_VERSION=${VISOR_VERSION}

CMD ["python", "server.py"]
