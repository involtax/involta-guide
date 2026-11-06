# syntax=docker/dockerfile:1.19
ARG PYTHON_IMAGE=python:3.13-slim@sha256:09a17cad02668ae61ece136b4fd36ac3849e14f670ebedb637ea3e15580525bc

FROM ${PYTHON_IMAGE} AS base
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Resolve/build dependencies once. No build tooling is copied into runtime.
FROM base AS dependencies
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
    uv sync --frozen --extra voice-local --no-dev --no-install-project --no-cache \
    && chmod -R a+rX /app/.venv

# Install application metadata and entry points separately from the large venv.
# Exclusions are relative to src; frontend edits must not rebuild this stage.
FROM dependencies AS application
COPY --exclude=involta_guide/web/templates \
     --exclude=involta_guide/web/static \
     --exclude=involta_voice/static src ./src
RUN --mount=type=secret,id=proxy_ca \
    if [ -f /run/secrets/proxy_ca ]; then export SSL_CERT_FILE=/run/secrets/proxy_ca GIT_SSL_CAINFO=/run/secrets/proxy_ca CMAKE_TLS_CAINFO=/run/secrets/proxy_ca; fi; \
    uv pip install --python /app/.venv/bin/python --no-deps --no-cache --target /app/project . \
    && chmod -R a+rX /app/project

FROM base AS runtime
# ffprobe is required to measure uploaded audio; no compiler or uv in this stage.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates libgomp1 postgresql-client \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 guide \
    && useradd --uid 10001 --gid 10001 --create-home --no-log-init \
       --shell /usr/sbin/nologin guide
COPY --from=dependencies /app/.venv /app/.venv
COPY --from=application /app/project /app/project
ENV PATH="/app/project/bin:/app/.venv/bin:$PATH" \
    PYTHONPATH=/app/project \
    ROOT_DIR=/app \
    DATA_DIR=/app/var \
    SITE_URL=https://involta.ru/guide \
    BASE_PATH=/guide \
    COOKIE_SECURE=true
RUN /app/.venv/bin/python -c "import sherpa_onnx, importlib.metadata, json; provenance=json.loads(importlib.metadata.distribution('sherpa-onnx').read_text('direct_url.json')); assert provenance['vcs_info']['commit_id']=='3898de1380029e96a3b0a3df9e21e3f5ee5eb7fb', provenance; from involta_voice.sherpa_tokenizer import tokenizer; assert tokenizer().vocab_size()==1024; from involta_voice.text import catalogue_forms; assert catalogue_forms(['Иваново'], city='Иваново')['иванове']=='Иванове'"
COPY data ./data
COPY migrations ./migrations
COPY alembic.ini ./
RUN chmod -R a+rX /app/data /app/migrations \
    && chmod a+r /app/alembic.ini \
    && mkdir -p /app/var/audio /app/var/voice/audio /app/var/asr-models \
    && chown -R guide:guide /app/var
# Large browser VAD binaries do not change when widget JavaScript changes.
COPY src/involta_voice/static/vad /app/project/involta_voice/static/vad
COPY --exclude=vad src/involta_voice/static /app/project/involta_voice/static
COPY src/involta_guide/web/static /app/project/involta_guide/web/static
# Keep templates last: a text edit changes only this small filesystem layer.
COPY src/involta_guide/web/templates /app/project/involta_guide/web/templates
USER 10001:10001
EXPOSE 3000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import os,urllib.request; urllib.request.build_opener(urllib.request.ProxyHandler({})).open('http://127.0.0.1:3000'+os.environ.get('BASE_PATH','/guide')+'/health/ready',timeout=3)"]
CMD ["uvicorn", "involta_guide.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "3000", "--no-access-log", "--no-proxy-headers", "--timeout-graceful-shutdown", "15"]
