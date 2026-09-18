# syntax=docker/dockerfile:1.7
#
# One image, one process. `scripts/serve.py` runs the WSGI listener and hosts
# the document worker as a thread inside it, which is why there is no second
# service here and why this must never be scaled past one replica: the stream
# registry in platform_core/agent_runtime/streaming.py is process-local, so a
# second process makes Stop silently fail to cancel a provider call the
# application is still being billed for.
#
# The Claude Code CLI is NOT installed here. It arrives inside the
# claude-agent-sdk wheel (_bundled/claude) and agent_runtime/claude_runtime.py
# addresses it by that path. Installing a second one on PATH would not be used,
# and a host login must never be reachable - credentials.py blanks HOME and
# CLAUDE_CONFIG_DIR precisely so a run cannot inherit one.

ARG PYTHON_VERSION=3.12-slim-bookworm

# ---------------------------------------------------------------- dependencies
FROM python:${PYTHON_VERSION} AS deps
COPY --from=ghcr.io/astral-sh/uv:0.5 /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/app/.venv
COPY pyproject.toml uv.lock ./
# --frozen: the lock decides, and a lock that no longer matches pyproject fails
# the build rather than quietly resolving something else.
# --no-install-project: this repository is deployed as source, not as a wheel.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

# ------------------------------------------------------------------- static
# collectstatic needs settings, and settings read a signing key in every mode.
# It runs in its own stage with a throwaway key so that key cannot reach the
# image that gets shipped - only the built staticfiles directory is copied on.
FROM deps AS static
COPY . /app
RUN set -eu; \
    mkdir -p /tmp/build-secrets; \
    head -c 64 /dev/urandom | base64 | tr -d '\n' > /tmp/build-secrets/django_secret_key; \
    chmod 600 /tmp/build-secrets/django_secret_key; \
    printf 'mode = "development"\nhosts = ["localhost"]\nsecret_directory = "/tmp/build-secrets"\n' \
      > /tmp/build.toml; \
    DIGITAL_BRAIN_CONFIG=/tmp/build.toml /app/.venv/bin/python manage.py collectstatic --noinput; \
    rm -rf /tmp/build-secrets /tmp/build.toml

# ------------------------------------------------------------------ runtime
FROM python:${PYTHON_VERSION} AS runtime

# clamav supplies clamscan, which processing.py invokes with
# --fail-if-cvd-older-than=7: signatures older than a week fail the scan closed
# rather than passing a file nobody checked. freshclam keeps them inside that
# window. libmagic backs MarkItDown's type sniffing; git is here for Code
# Graph, which reads a repository by cloning it rather than over the REST API.
RUN set -eu; \
    apt-get update; \
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        clamav \
        libmagic1 \
        git \
        ca-certificates \
        tini; \
    rm -rf /var/lib/apt/lists/*

# An unprivileged identity, as docs/deployment.md requires. The uid is fixed so
# a bind-mounted secret directory can be chowned to it from the host - read_secret
# refuses any file carrying group or other permission bits.
RUN groupadd --gid 10001 brain \
    && useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin brain

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH" \
    DIGITAL_BRAIN_CONFIG=/etc/digitalbrain/production.toml

COPY --from=deps  --chown=root:root /app/.venv       /app/.venv
COPY --from=static --chown=root:root /app/staticfiles /app/staticfiles
COPY --chown=root:root . /app

# Written to at runtime: uploaded originals, the event log, and ClamAV's
# signature database. Everything else stays read-only to the service account.
RUN set -eu; \
    mkdir -p /app/.runtime /var/lib/clamav /var/lib/digitalbrain/credentials /var/log/clamav /run/secrets; \
    chown -R brain:brain /app/.runtime /var/lib/clamav /var/lib/digitalbrain /var/log/clamav /run/secrets; \
    chmod 700 /var/lib/digitalbrain/credentials; \
    chmod 700 /run/secrets; \
    chmod +x /app/deploy/entrypoint.sh

USER brain
EXPOSE 8000

# tini reaps the Claude CLI subprocesses a run leaves behind; without an init,
# PID 1 is Python and zombies accumulate across runs.
ENTRYPOINT ["/usr/bin/tini", "--", "/app/deploy/entrypoint.sh"]
