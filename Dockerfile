# reflex-agentlens container image (plan task P7-02.1; REQ-203, REQ-326, Tech Design DD-021).
#
#   docker build -t reflex-agentlens .
#   docker run --rm -p 127.0.0.1:8000:8000 -e AGENTLENS_AUTH_MODE=none \
#     -v agentlens-data:/data reflex-agentlens
#
# builder: uv + Bun install the project non-editable, so the wheel build hook exports the
#          frontend into the installed package (DD-002).
# runtime: the virtualenv only. No Node, Bun, compilers or package caches (REQ-203.b).

ARG PYTHON_IMAGE=python:3.13.16-slim-trixie

FROM ghcr.io/astral-sh/uv:0.11.7 AS uv
FROM oven/bun:1.4.4-slim AS bun

FROM ${PYTHON_IMAGE} AS builder
COPY --from=uv /uv /usr/local/bin/uv
COPY --from=bun /usr/local/bin/bun /usr/local/bin/bun
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/agentlens \
    REFLEX_TELEMETRY_ENABLED=false
WORKDIR /src
# Dependencies first (cached across source changes), then the project itself.
# The workspace members' manifests let uv read the lockfile; only the root package is installed.
COPY pyproject.toml uv.lock README.md LICENSE NOTICE ./
COPY site/pyproject.toml ./site/
COPY examples/pyproject.toml ./examples/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable --package reflex-agentlens --no-install-project
COPY hatch_build.py ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable --package reflex-agentlens \
 && test -f /opt/agentlens/lib/python3.13/site-packages/reflex_agentlens/_frontend/index.html

FROM ${PYTHON_IMAGE} AS runtime
LABEL org.opencontainers.image.title="reflex-agentlens" \
      org.opencontainers.image.description="Self-hosted observability for AI agents, built with Reflex" \
      org.opencontainers.image.source="https://github.com/ecrespo/reflex-agentlens" \
      org.opencontainers.image.licenses="MIT"
# No package manager in the runtime either (REQ-203.b): the venv is complete.
RUN python -m pip uninstall --yes --quiet pip setuptools wheel 2>/dev/null; \
    rm -rf /root/.cache \
 && groupadd --system --gid 10001 agentlens \
 && useradd --system --uid 10001 --gid 10001 --home-dir /data --no-create-home \
      --shell /usr/sbin/nologin agentlens \
 && mkdir -p /data \
 && chown 10001:10001 /data
COPY --from=builder /opt/agentlens /opt/agentlens
# The healthcheck probes serve (8000) and collect (4318) unless the port is set explicitly.
# HOME and the XDG dirs point at writable places so a read-only root filesystem works
# (REQ-326.a: writes go to /data and /tmp only).
ENV PATH=/opt/agentlens/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/data \
    XDG_CACHE_HOME=/tmp/.cache \
    REFLEX_TELEMETRY_ENABLED=false \
    AGENTLENS_SERVER_DATA_DIR=/data \
    AGENTLENS_SERVER_HTTP_HOST=0.0.0.0
USER 10001:10001
WORKDIR /data
VOLUME ["/data"]
EXPOSE 8000 4317
HEALTHCHECK --interval=10s --timeout=3s --start-period=30s --retries=3 \
  CMD ["python", "-c", "import os, urllib.request as u\nports = [os.environ['AGENTLENS_SERVER_HTTP_PORT']] if os.environ.get('AGENTLENS_SERVER_HTTP_PORT') else ['8000', '4318']\nfor p in ports:\n    try:\n        u.urlopen(f'http://127.0.0.1:{p}/healthz', timeout=2)\n        raise SystemExit(0)\n    except OSError:\n        pass\nraise SystemExit(1)"]
ENTRYPOINT ["reflex-agentlens"]
CMD ["serve"]
