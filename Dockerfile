# Runtime image for the nas-subtitles worker and CLI.
#
# Models are never baked in: `nas-subs models install` writes them into the
# mounted /models volume. The image therefore builds without network access to
# any model host and runs with HF_HUB_OFFLINE=1 by default.
#
# Digest recorded after a successful `docker compose build` on this arm64 Mac.
FROM python:3.11-slim@sha256:0dd364ba7e10242f07755449e3a3d0e35f9efd987952737b90def6709ab0c5ce AS runtime

# ffmpeg: audio inspection and extraction.
# libgomp1: OpenMP runtime required by CTranslate2.
# ca-certificates: TLS for the one-off `models install` bootstrap.
RUN apt-get update \
    && apt-get install --no-install-recommends -y \
        ffmpeg \
        libgomp1 \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# uv comes from PyPI rather than a second container registry, so the build
# depends on Docker Hub and PyPI only.
RUN pip install --no-cache-dir uv==0.12.23

ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Dependencies resolve from the committed lock only, so the build is
# reproducible and a source edit does not invalidate this layer.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
RUN uv sync --frozen --no-dev

# Model caches live under /models so install and worker agree on the paths.
ENV HF_HUB_OFFLINE=1 \
    XDG_DATA_HOME=/models/xdg \
    XDG_CACHE_HOME=/models/cache \
    HF_HOME=/models/huggingface

# Non-root by default. Compose overrides the numeric ids with APP_UID/APP_GID
# so the mounted volumes stay writable by the host user.
ARG APP_UID=1000
ARG APP_GID=1000
RUN set -eu; \
    if ! getent group "$APP_GID" >/dev/null; then groupadd -g "$APP_GID" app; fi; \
    if ! getent passwd "$APP_UID" >/dev/null; then \
        useradd -u "$APP_UID" -g "$APP_GID" -M -d /nonexistent -s /usr/sbin/nologin app; \
    fi
USER $APP_UID:$APP_GID

# No ENTRYPOINT: Compose passes the full `nas-subs ...` argv, and the
# healthcheck runs `nas-subs health` directly.
CMD ["nas-subs", "daemon", "--config", "/config/config.yaml"]
