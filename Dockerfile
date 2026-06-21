FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        libjpeg62-turbo \
        libopenjp2-7 \
        libtiff6 \
        libheif1 \
        libgpiod2 \
        libgl1 \
        libglib2.0-0 \
        curl \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --uid 1000 --create-home --shell /usr/sbin/nologin vibeframe \
 && mkdir -p /photos /var/cache/vibeframe /var/lib/vibeframe \
 && chown -R vibeframe:vibeframe /var/cache/vibeframe /var/lib/vibeframe

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src

ARG INSTALL_RPI=1
RUN pip install --upgrade pip && \
    if [ "$INSTALL_RPI" = "1" ]; then \
        pip install ".[rpi]"; \
    else \
        pip install "."; \
    fi

USER vibeframe

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${VIBEFRAME_WEB_PORT:-8080}/healthz" || exit 1

ENTRYPOINT ["python", "-m", "vibeframe"]
