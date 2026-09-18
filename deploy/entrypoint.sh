#!/bin/sh
# Start one Digital Brain listener, and nothing else.
#
# Order matters and each step fails the container rather than starting a
# half-configured one: a platform that serves sign-in while its migrations are
# pending is worse than a platform that is plainly down.
set -eu

INSTANCE="${DIGITAL_BRAIN_INSTANCE:-digitalbrain}"
PORT="${DIGITAL_BRAIN_PORT:-8000}"
# Bound to every interface inside the container only because nothing publishes
# a port to the host: Coolify's proxy reaches it over the compose network. See
# the note on --trusted-proxy in scripts/serve.py - the wildcard is sound only
# while that stays true.
HOST="${DIGITAL_BRAIN_HOST:-0.0.0.0}"

# ------------------------------------------------------------- configuration
# An operator's own file wins if one is mounted. Otherwise it is rendered from
# environment variables, because the non-secret configuration is exactly that -
# non-secret - and requiring a hand-created file on the host made the first
# deploy fail with "not mounted", which says what is missing without saying why
# it should have been there.
#
# Secrets are NOT rendered here and never will be. They are files with
# owner-only permissions, which is the platform's rule and the reason
# read_secret refuses anything else.
MOUNTED="/etc/digitalbrain/production.toml"
RENDERED="/app/.runtime/production.toml"

if [ -f "$MOUNTED" ]; then
    CONFIG="$MOUNTED"
    echo "entrypoint: using the mounted configuration at $MOUNTED"
else
    CONFIG="$RENDERED"
    hosts=""
    origins=""
    # DIGITAL_BRAIN_HOSTS is a comma-separated list; every name the proxy may
    # forward under has to be here or Django answers 400, which looks like a
    # broken deployment rather than the check it is.
    old="$IFS"; IFS=','
    for name in ${DIGITAL_BRAIN_HOSTS:-localhost}; do
        name=$(echo "$name" | tr -d ' ')
        [ -n "$name" ] || continue
        hosts="$hosts\"$name\", "
        origins="$origins\"https://$name\", "
    done
    IFS="$old"

    mkdir -p /app/.runtime
    {
        echo 'mode = "production"'
        echo "hosts = [${hosts%, }]"
        echo "csrf_origins = [${origins%, }]"
        echo 'secret_directory = "/run/secrets"'
        # Lets an application owner set their own provider credentials from the
        # Credentials screen instead of asking an operator for each one. A
        # separate directory, because an operator's mounted credential
        # deliberately takes precedence over one entered in a browser.
        echo 'managed_secret_directory = "/var/lib/digitalbrain/credentials"'
        # Coolify terminates TLS and sets X-Forwarded-Proto. Safe only because
        # no port is published, and waitress clears any forwarded header the
        # proxy did not set itself.
        echo 'trust_proxy = true'
        # ClamAV, installed in the image. processing.py refuses to convert a
        # document without a scanner, so intake fails closed rather than
        # quietly storing something nobody checked.
        echo 'scanner = "/usr/bin/clamscan"'
        echo "import_max_files = ${DIGITAL_BRAIN_IMPORT_MAX_FILES:-100}"
        echo ''
        echo '[database]'
        echo 'name = "digital_brain"'
        echo 'user = "digital_brain_app"'
        # Must match the certificate's SAN exactly, or sslmode=verify-full
        # rejects a certificate that is otherwise perfectly valid.
        echo 'host = "postgres"'
        echo 'port = 5432'
        echo 'sslrootcert = "/var/lib/digitalbrain/pgcerts/ca.crt"'
    } > "$CONFIG"
    echo "entrypoint: rendered configuration for ${DIGITAL_BRAIN_HOSTS:-localhost}"
fi
export DIGITAL_BRAIN_CONFIG="$CONFIG"

# ---------------------------------------------------------------- signatures
# processing.py runs clamscan with --fail-if-cvd-older-than=7, so a database
# older than a week rejects every document with "signatures stale" rather than
# letting one through unchecked. Refreshed once now, then daily in the
# background. A failure here is logged and not fatal: an existing database
# inside the window keeps working, and one outside it fails the scan closed,
# which is the behaviour we want anyway.
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
