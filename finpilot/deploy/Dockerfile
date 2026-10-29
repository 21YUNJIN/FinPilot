# FinPilot API + workers image (Linux container) -- DEL-02 / NFR-02
#
# Build from the REPOSITORY ROOT (the compose file does this for you):
#   docker build -f deploy/Dockerfile -t finpilot-api:local .
#
# NOT VERIFIED ON THE REFERENCE HOST: this machine has no Docker/container runtime
# (verified: `docker` is not on PATH), so this image has never been built here.
# The target configuration is documented in deploy/README.md and must be validated
# on a Linux host with a container runtime before being claimed as working.
#
# The same image runs the API and both background workers; the sandbox providers use
# the same image with a different command (see deploy/compose.yaml).

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HOME=/tmp

WORKDIR /app

# --- Python dependencies (editable install of the finpilot package) -----------
# config.py resolves REPO_ROOT as parents[4] of the module file, so an editable
# install at /app makes /app/.env, /app/data and /app/apps resolve as expected.
COPY pyproject.toml /app/pyproject.toml
COPY services /app/services
RUN python -m pip install --no-cache-dir -e /app

# --- Migration chain, seed data, contracts, ops scripts and built clients ------
COPY alembic.ini /app/alembic.ini
COPY migrations /app/migrations
COPY data /app/data
COPY apps /app/apps
COPY contracts /app/contracts
COPY deploy /app/deploy
COPY scripts /app/scripts

# --- Non-root runtime ---------------------------------------------------------
# read_only root filesystem is expected in compose; writable paths are tmpfs (/tmp)
# and the declared named volumes. No secrets are baked into the image.
RUN useradd --uid 10001 --user-group --no-create-home --shell /usr/sbin/nologin finpilot \
    && chmod -R a-w /app/services /app/migrations 2>/dev/null || true
USER 10001:10001

EXPOSE 58000

# healthcheck is declared in compose.yaml (it needs to differ per service)

CMD ["python", "-m", "uvicorn", "finpilot.app:app", "--host", "0.0.0.0", "--port", "58000"]