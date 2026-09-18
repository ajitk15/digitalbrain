#!/bin/sh
# Start one Digital Brain listener, and nothing else.
#
# Order matters and each step fails the container rather than starting a
# half-configured one: a platform that serves sign-in while its migrations are
# pending is worse than a platform that is plainly down.
set -eu

CONFIG="${DIGITAL_BRAIN_CONFIG:?DIGITAL_BRAIN_CONFIG must point at the mounted TOML}"
INSTANCE="${DIGITAL_BRAIN_INSTANCE:-coolify}"
PORT="${DIGITAL_BRAIN_PORT:-8000}"
# Bound to every interface inside the container only because nothing publishes
# a port to the host: Coolify's proxy reaches it over the compose network. See
# the note on --trusted-proxy in scripts/serve.py - the wildcard is sound only
# while that stays true.
HOST="${DIGITAL_BRAIN_HOST:-0.0.0.0}"

if [ ! -f "$CONFIG" ]; then
    echo "entrypoint: $CONFIG is not mounted." >&2
    exit 1
fi

# ---------------------------------------------------------------- signatures
# processing.py runs clamscan with --fail-if-cvd-older-than=7, so a database
# older than a week rejects every document with "signatures stale" rather than
# letting one through unchecked. Refreshed once now, then daily in the
# background. A failure here is logged and not fatal: an existing database that
# is still inside the window keeps working, and one that is not will fail the
# scan closed, which is the behaviour we want anyway.
if [ "${DIGITAL_BRAIN_FRESHCLAM:-1}" = "1" ]; then
    if [ ! -f /var/lib/clamav/main.cvd ] && [ ! -f /var/lib/clamav/main.cld ]; then
        echo "entrypoint: fetching ClamAV signatures for the first time; this takes a few minutes."
        freshclam --quiet --stdout || echo "entrypoint: freshclam failed; documents will fail closed." >&2
    fi
    (
        while true; do
            sleep 86400
            freshclam --quiet --stdout || echo "entrypoint: scheduled freshclam failed." >&2
        done
    ) &
fi

# ------------------------------------------------------------------ database
# Migrations run here rather than in a separate one-shot service because there
# is exactly one app container by design, so there is no second writer to race.
echo "entrypoint: applying migrations"
python manage.py migrate --noinput

# Django's own production audit, with warnings fatal. It reads the same
# settings the server is about to use, so a misconfigured proxy or cookie flag
# stops the deploy instead of being discovered by a user.
echo "entrypoint: checking deployment settings"
python manage.py check --deploy --fail-level WARNING

exec python scripts/serve.py \
    --host "$HOST" \
    --port "$PORT" \
    --instance "$INSTANCE" \
    --trusted-proxy "${DIGITAL_BRAIN_TRUSTED_PROXY:-*}"
