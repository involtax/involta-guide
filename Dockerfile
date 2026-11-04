# syntax=docker/dockerfile:1
ARG PYTHON_IMAGE=python:3.13-slim

FROM ${PYTHON_IMAGE} AS base
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Resolve/build dependencies once. No build tooling is copied into runtime.
FROM base AS builder
ENV UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential git ca-certificates \
    && rm -rf /var/lib/apt/lists/*
RUN --mount=type=secret,id=proxy_ca \
    if [ -f /run/secrets/proxy_ca ]; then export PIP_CERT=/run/secrets/proxy_ca; fi; \
    pip install --no-cache-dir uv==0.12.23
COPY pyproject.toml uv.lock ./
RUN --mount=type=secret,id=proxy_ca \
    if [ -f /run/secrets/proxy_ca ]; then export SSL_CERT_FILE=/run/secrets/proxy_ca GIT_SSL_CAINFO=/run/secrets/proxy_ca CMAKE_TLS_CAINFO=/run/secrets/proxy_ca; fi; \
    uv sync --frozen --extra voice-local --no-dev --no-install-project
COPY src ./src
RUN chmod -R a+rX /app/src
RUN --mount=type=secret,id=proxy_ca \
    if [ -f /run/secrets/proxy_ca ]; then export SSL_CERT_FILE=/run/secrets/proxy_ca GIT_SSL_CAINFO=/run/secrets/proxy_ca CMAKE_TLS_CAINFO=/run/secrets/proxy_ca; fi; \
    uv sync --frozen --extra voice-local --no-dev --no-editable

FROM base AS runtime
# ffprobe is required to measure uploaded audio; no compiler or uv in this stage.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates libgomp1 postgresql-client \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 guide \
    && useradd --uid 10001 --gid 10001 --create-home --no-log-init \
       --shell /usr/sbin/nologin guide
COPY --from=builder /app/.venv /app/.venv
RUN /app/.venv/bin/python -c "import sherpa_onnx, importlib.metadata, json; provenance=json.loads(importlib.metadata.distribution('sherpa-onnx').read_text('direct_url.json')); assert provenance['vcs_info']['commit_id']=='3898de1380029e96a3b0a3df9e21e3f5ee5eb7fb', provenance"
COPY data ./data
COPY migrations ./migrations
COPY alembic.ini ./
RUN chmod -R a+rX /app/.venv /app/data /app/migrations \
    && chmod a+r /app/alembic.ini \
    && mkdir -p /app/var/audio /app/var/voice/audio /app/var/asr-models \
    && chown -R guide:guide /app/var
ENV PATH="/app/.venv/bin:$PATH" \
    ROOT_DIR=/app \
    DATA_DIR=/app/var \
    SITE_URL=https://involta.ru/guide \
    BASE_PATH=/guide \
    COOKIE_SECURE=true
USER 10001:10001
EXPOSE 3000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import os,urllib.request; urllib.request.build_opener(urllib.request.ProxyHandler({})).open('http://127.0.0.1:3000'+os.environ.get('BASE_PATH','/guide')+'/health/ready',timeout=3)"]
CMD ["uvicorn", "involta_guide.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "3000", "--no-access-log", "--no-proxy-headers", "--timeout-graceful-shutdown", "15"]
