# syntax=docker/dockerfile:1
# Base image digest: python:3.12-slim index, read from the Docker Hub registry API on 2026-11-25.
ARG PYTHON_IMAGE=python:3.12-slim@sha256:191083cdf6c7bda8c76158983163ead5d5cf7b77cf3033e805bb0f5296f51b4b

FROM ${PYTHON_IMAGE} AS builder
COPY --from=ghcr.io/astral-sh/uv:0.12.11@sha256:79c6f4776b851471cc73b7d21d0cc834bb94383c292e83640d27eff512864df7 /uv /uvx /bin/
ENV UV_COMPILE_BYTECODE=0 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never UV_PYTHON_PREFERENCE=only-system
WORKDIR /app
# Dependency layer first (cached until the lockfile changes). Default groups only: no e2e.
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev --no-editable --no-install-project
# Then the project itself, installed non-editable into the venv.
COPY src/ ./src/
RUN uv sync --frozen --no-dev --no-editable

FROM ${PYTHON_IMAGE} AS runtime
RUN groupadd --gid 10001 app \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin app \
    && mkdir /data && chown 10001:10001 /data
COPY --from=builder /app/.venv /app/.venv
COPY --chmod=0755 --chown=0:0 docker/entrypoint.sh /usr/local/bin/entrypoint.sh
ENV DATA_DIR=/data \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH=/app/.venv/bin:$PATH
WORKDIR /app
VOLUME /data
EXPOSE 8000
# Root only so the entrypoint can chown a root-owned mounted /data; it then drops to
# uid/gid 10001 with no capabilities (setpriv, util-linux) before exec'ing the CMD.
USER root
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["sh", "-c", "python -c \"import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8000')+'/healthz', timeout=4)\""]
CMD ["sh", "-c", "exec uvicorn exit_archive.web.app:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1 --proxy-headers"]
