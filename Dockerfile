# ═══════════════════════════════════════════════════════════════════════════
# S3 Backup Flow — Optimized Multi-Stage Dockerfile
#
# Stage 1 (builder): compile Python wheels — never ships to production
# Stage 2 (runtime): lean image with only what the app needs at runtime
#
# Image size targets:
#   builder  ~350 MB  (discarded after build)
#   runtime  ~220 MB  (shipped to ECR / production)
# ═══════════════════════════════════════════════════════════════════════════

# ── Stage 1: build Python wheels ──────────────────────────────────────────
FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build

# Install only what is needed to compile C-extension wheels
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc \
        libssl-dev \
        libffi-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

# Build wheels into /wheels — these pre-compiled artefacts are then
# installed in the runtime stage with no compiler needed there.
RUN pip wheel --no-deps --wheel-dir /wheels -r requirements.txt


# ── Stage 2: runtime image ────────────────────────────────────────────────
FROM python:3.12-slim

# Hardened Python environment
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONFAULTHANDLER=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Install OS-level database clients (mysqldump + pg_dump) and curl (healthcheck)
# Merge into a single RUN layer and scrub apt lists immediately to save space.
RUN apt-get update && apt-get install -y --no-install-recommends \
        default-mysql-client \
        postgresql-client \
        curl \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* /tmp/* /var/tmp/*

# Copy pre-built wheels from the builder stage and install them.
# --no-index + --find-links means pip never touches the network here.
COPY --from=builder /wheels /wheels
RUN pip install --no-index --find-links /wheels /wheels/*.whl \
    && rm -rf /wheels

# ── Non-root user ───────────────────────────────────────────────────────
# Running as root inside a container is an unnecessary privilege escalation
# risk. Drop to an unprivileged system user.
RUN groupadd --system --gid 1001 appgroup \
    && useradd  --system --uid 1001 --gid 1001 \
                --no-create-home --shell /sbin/nologin appuser

# Copy only the application source — everything else is excluded via .dockerignore
COPY --chown=appuser:appgroup app/ ./app/

USER appuser

EXPOSE 8000

# Default entrypoint: FastAPI server.
# Override CMD in docker-compose for the worker / beat containers.
CMD ["uvicorn", "app.main:app", \
     "--host", "0.0.0.0", \
     "--port", "8000", \
     "--workers", "2", \
     "--log-level", "info"]
