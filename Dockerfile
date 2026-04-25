# syntax=docker/dockerfile:1.7
# Single image is baked once and reused by both the bot and the API services;
# docker-compose picks the entry point per service. Multi-stage keeps the
# runtime layer slim (no build toolchain in the final image).

# ---------- builder ---------------------------------------------------------
# Canonical astral pattern: start from the official python image and copy the
# uv binary out of the uv image. Avoids the guesswork of composite tags.
FROM python:3.14-slim-trixie AS builder

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_NO_INSTALLER_METADATA=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Native wheels for pycryptodome/crypto-cpp-py need gcc + libssl headers.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential libssl-dev libffi-dev \
    && rm -rf /var/lib/apt/lists/*

# Cache the dep layer by copying manifest first.
COPY pyproject.toml uv.lock* ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev

# Now bring in the source and finalize the venv.
COPY . .
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# ---------- runtime ---------------------------------------------------------
FROM python:3.14-slim-trixie AS runtime

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    API_HOST=0.0.0.0 \
    API_PORT=8000

WORKDIR /app

# Runtime-only libs (libssl is needed by pycryptodome at import time).
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libssl3 libffi8 tini \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 1000 app

# Copy the pre-built virtualenv and application from the builder stage.
COPY --from=builder --chown=app:app /app /app

# Writable mount points for SQLite DB and logs (declared as volumes below).
# Only chown the new directories — the venv tree was already chowned during
# COPY --from=builder. A recursive chown over thousands of venv files
# rewrites the entire layer on overlay/WSL2 and adds ~13 min to every build.
RUN mkdir -p /app/files /app/logs && chown app:app /app/files /app/logs

USER app

# Tini as PID 1 reaps zombies from the multiprocessing workers (queue +
# notifier) the bot spawns, and forwards signals cleanly for docker stop.
ENTRYPOINT ["/usr/bin/tini", "--"]

# Default command: bot. docker-compose overrides it for the API service.
CMD ["python", "-m", "main"]
