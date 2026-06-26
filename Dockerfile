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
 && mkdir -p /photos /var/cache/vibeframe /var/cache/vibeframe/numba /var/lib/vibeframe \
 && chown -R vibeframe:vibeframe /var/cache/vibeframe /var/lib/vibeframe

# numba caches JIT'd code per-function in this dir. Default would be
# __pycache__ next to the module, but that's installed as root-owned in
# site-packages so the unprivileged vibeframe user can't write there.
ENV NUMBA_CACHE_DIR=/var/cache/vibeframe/numba

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src

ARG INSTALL_RPI=1
ARG INSTALL_PROFILE=0
ARG INSTALL_DITHER=1
RUN set -eu; \
    pip install --upgrade pip; \
    EXTRAS=""; \
    add_extra() { if [ -z "$EXTRAS" ]; then EXTRAS="$1"; else EXTRAS="$EXTRAS,$1"; fi; }; \
    if [ "$INSTALL_RPI" = "1" ]; then add_extra rpi; fi; \
    if [ "$INSTALL_DITHER" = "1" ]; then add_extra dither; fi; \
    if [ "$INSTALL_PROFILE" = "1" ]; then add_extra profile; fi; \
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

# Generous timeout — image refreshes can briefly starve asyncio and cause
# legitimate healthz responses to take many seconds. Restarting the container
# mid-refresh would be much worse than a slow healthz reply.
HEALTHCHECK --interval=60s --timeout=30s --start-period=60s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${VIBEFRAME_WEB_PORT:-8080}/healthz" || exit 1

ENTRYPOINT ["python", "-m", "vibeframe"]
