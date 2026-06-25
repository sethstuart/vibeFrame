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
ARG INSTALL_PROFILE=0
RUN set -eu; \
    pip install --upgrade pip; \
    EXTRAS=""; \
    if [ "$INSTALL_RPI" = "1" ]; then EXTRAS="rpi"; fi; \
    if [ "$INSTALL_PROFILE" = "1" ]; then \
        if [ -n "$EXTRAS" ]; then EXTRAS="$EXTRAS,profile"; else EXTRAS="profile"; fi; \
    fi; \
    if [ -n "$EXTRAS" ]; then SPEC=".[$EXTRAS]"; else SPEC="."; fi; \
    echo "Installing $SPEC"; \
    if [ "$INSTALL_RPI" = "1" ]; then \
        apt-get update; \
        apt-get install -y --no-install-recommends build-essential python3-dev; \
        pip install "$SPEC"; \
        apt-get purge -y --auto-remove build-essential python3-dev; \
        rm -rf /var/lib/apt/lists/*; \
    else \
        pip install "$SPEC"; \
    fi

USER vibeframe

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${VIBEFRAME_WEB_PORT:-8080}/healthz" || exit 1

ENTRYPOINT ["python", "-m", "vibeframe"]
