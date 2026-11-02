# syntax=docker/dockerfile:1
FROM python:3.13-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 UV_LINK_MODE=copy
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg ca-certificates && rm -rf /var/lib/apt/lists/*
RUN --mount=type=secret,id=proxy_ca \
    if [ -f /run/secrets/proxy_ca ]; then export PIP_CERT=/run/secrets/proxy_ca; fi; \
    pip install --no-cache-dir uv==0.12.23
COPY pyproject.toml uv.lock ./
RUN --mount=type=secret,id=proxy_ca \
    if [ -f /run/secrets/proxy_ca ]; then export SSL_CERT_FILE=/run/secrets/proxy_ca; fi; \
    uv sync --frozen --no-dev --no-install-project
COPY src ./src
COPY data ./data
COPY migrations ./migrations
COPY alembic.ini ./
RUN --mount=type=secret,id=proxy_ca \
    if [ -f /run/secrets/proxy_ca ]; then export SSL_CERT_FILE=/run/secrets/proxy_ca; fi; \
    uv sync --frozen --no-dev
RUN chmod -R a+rX /app/src /app/data /app/migrations && chmod a+r /app/alembic.ini /app/pyproject.toml /app/uv.lock
RUN useradd --uid 10001 --create-home guide && mkdir -p /app/var/audio && chown -R guide:guide /app/var
USER guide
ENV PATH="/app/.venv/bin:$PATH" SITE_URL=https://involta.ru/guide BASE_PATH=/guide
EXPOSE 3000
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import os,urllib.request; urllib.request.build_opener(urllib.request.ProxyHandler({})).open('http://127.0.0.1:3000'+os.environ.get('BASE_PATH','/guide')+'/health/ready',timeout=3)"
CMD ["uvicorn", "involta_guide.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "3000", "--no-access-log", "--no-proxy-headers"]
