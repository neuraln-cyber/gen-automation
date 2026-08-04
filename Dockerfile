# syntax=docker/dockerfile:1.7.1@sha256:a57df69d0ea827fb7266491f2813635de6f17269be881f696fbfdf2d83dda33e

FROM python:3.14-slim@sha256:cea0e6040540fb2b965b6e7fb5ffa00871e632eef63719f0ea54bca189ce14a6

LABEL org.opencontainers.image.title="gen-automation-control-plane" \
      org.opencontainers.image.description="Private generation automation control plane" \
      org.opencontainers.image.source="https://github.com/neuraln-cyber/gen-automation"

ENV HOME=/home/app \
    PATH=/usr/local/bin:/usr/local/sbin:/usr/sbin:/usr/bin:/sbin:/bin \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONNOUSERSITE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src

WORKDIR /app

COPY requirements.lock ./

RUN python3.12 -m pip install \
        --only-binary=:all: \
        --require-hashes \
        --no-deps \
        -r requirements.lock \
    && python3.12 -m pip check \
    && python3.12 -c "import sys; assert sys.version_info[:2] == (3, 12)" \
    && groupadd --system --gid 10001 app \
    && useradd \
        --system \
        --create-home \
        --uid 10001 \
        --gid app \
        --home-dir /home/app \
        --shell /usr/sbin/nologin \
        app

COPY alembic.ini ./
COPY migrations ./migrations
COPY src ./src

USER 10001:10001

EXPOSE 8000

HEALTHCHECK --interval=20s --timeout=3s --start-period=30s --retries=3 \
    CMD ["python3.12", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health/live', timeout=2).read()"]

CMD ["python3.12", "-m", "uvicorn", "gen_automation.app:app", "--host", "0.0.0.0", "--port", "8000", "--no-proxy-headers", "--no-access-log", "--no-server-header", "--no-date-header"]
