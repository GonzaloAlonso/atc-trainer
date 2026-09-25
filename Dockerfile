# Visor ATC — © 2026 Gonzalo Alonso. All rights reserved.
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
COPY server.py LICENSE ./

# Recordings and user accounts (SQLite) live on a volume so they survive container upgrades.
RUN useradd --system --uid 10001 --no-create-home visor \
    && mkdir -p /data && chown visor /data
USER visor
VOLUME ["/data"]

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4)"

# Version metadata last, so a new release doesn't invalidate the cached layers above.
ARG VISOR_VERSION=dev
ARG VCS_REF=unknown
ARG BUILD_DATE=
ARG SOURCE_URL=
LABEL org.opencontainers.image.title="Visor ATC" \
      org.opencontainers.image.description="3D air traffic control simulator on OpenSky data, with a decision-AI API" \
      org.opencontainers.image.version="${VISOR_VERSION}" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.created="${BUILD_DATE}" \
      org.opencontainers.image.source="${SOURCE_URL}" \
      org.opencontainers.image.authors="Gonzalo Alonso" \
      org.opencontainers.image.vendor="Gonzalo Alonso" \
      org.opencontainers.image.licenses="LicenseRef-Proprietary"
ENV VISOR_VERSION=${VISOR_VERSION} \
    VISOR_COMMIT=${VCS_REF} \
    VISOR_BUILD_DATE=${BUILD_DATE} \
    VISOR_SOURCE_URL=${SOURCE_URL}

CMD ["python", "server.py"]
