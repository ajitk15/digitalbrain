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
# What this container is. "all" (the default) is the original single process:
# the web listener with every worker lane inside it. "web" serves requests only
# and "worker" runs `manage.py run_worker` and nothing else - the split that
# docker-compose.scale.yaml sets up, so a deploy or a long provider call in one
# does not stop the other. The worker never runs migrations: it waits for them.
ROLE="${DIGITAL_BRAIN_ROLE:-all}"
case "$ROLE" in
    all|web|worker) ;;
    *) echo "entrypoint: DIGITAL_BRAIN_ROLE must be all, web or worker." >&2; exit 1 ;;
esac
# A clamd daemon to scan through (host:port or an absolute socket path). Set,
# no clamscan or freshclam runs in this container: the daemon keeps its own
# signatures and processing.py still refuses ones more than a week old.
CLAMD="${DIGITAL_BRAIN_CLAMD:-}"

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
# One canonical path, always. The entrypoint's own environment does not reach a
# later `docker exec`, which gets the image default instead - so a config living
# wherever it happened to be rendered means every administrative command fails
# with "Configuration missing" on a perfectly healthy container. An operator's
# mounted file is copied here rather than read in place, so one path is the
# answer for the server and for anyone running manage.py afterwards.
MOUNTED="/etc/digitalbrain/production.toml"
CONFIG="/app/.runtime/production.toml"
mkdir -p /app/.runtime

if [ -f "$MOUNTED" ]; then
    cp "$MOUNTED" "$CONFIG.$$" && mv "$CONFIG.$$" "$CONFIG"
    echo "entrypoint: using the mounted configuration from $MOUNTED"
else
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
        # Let whoever starts a run approve its plan. On by default because this
        # image is deployed single-operator: with it off, a plan can only be
        # approved by a second member holding can_approve, and an instance with
        # one person deadlocks at the review gate with no way out through the
        # UI. Set DIGITAL_BRAIN_SELF_APPROVAL=0 where two people really do
        # review every change - the audit record names the approver either way.
        if [ "${DIGITAL_BRAIN_SELF_APPROVAL:-1}" = "1" ]; then
            echo 'allow_self_approval = true'
        else
            echo 'allow_self_approval = false'
        fi
        # Coolify terminates TLS and sets X-Forwarded-Proto. Safe only because
        # no port is published, and waitress clears any forwarded header the
        # proxy did not set itself.
        echo 'trust_proxy = true'
        # ClamAV, installed in the image. processing.py refuses to convert a
        # document without a scanner, so intake fails closed rather than
        # quietly storing something nobody checked.
        if [ -n "$CLAMD" ]; then
            echo "clamd = \"$CLAMD\""
        else
            echo 'scanner = "/usr/bin/clamscan"'
        fi
        # More than one process serves this deployment once it is split, so a
        # chat answer's Stop and "still writing?" must be visible to each.
        if [ "$ROLE" != "all" ]; then
            echo 'shared_streams = true'
        fi
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
    } > "$CONFIG.$$"
    # Written aside and moved into place: a web and a worker container share
    # this volume, and neither may read the other's half-written file.
    mv "$CONFIG.$$" "$CONFIG"
    echo "entrypoint: rendered configuration for ${DIGITAL_BRAIN_HOSTS:-localhost}"
fi
export DIGITAL_BRAIN_CONFIG="$CONFIG"

# ------------------------------------------------------------- what is mounted
# Printed every start, because this deployment has now failed three times on
# something that is invisible from inside the application: whether the volumes
# the compose file declares actually arrived. A deployment platform is free to
# rewrite or drop them, and "Directory nonexistent" for a path the compose file
# mounts is the kind of thing worth one line of output forever.
echo "entrypoint: mounts"
for path in /run/secrets /app/.runtime /var/lib/clamav /var/lib/digitalbrain/credentials; do
    if mount | grep -q " ${path} "; then
        echo "  ${path}: volume"
    elif [ -d "$path" ]; then
        echo "  ${path}: plain directory in the image - NOT PERSISTENT"
    else
        echo "  ${path}: missing"
    fi
done

# ---------------------------------------------------------------- secrets
# The signing key is generated here when it is absent, rather than relying on a
# separate init service having run first. A platform decides for itself how to
# schedule a one-shot service, and a deployment that crash-loops on
# "Required mounted secret is unavailable" because of that ordering is a worse
# failure than a key this process can simply create.
#
# Still a file, still owner-only - the rule is that secrets are not environment
# variables, not that some other container has to write them. An existing key
# is never replaced: rotating it invalidates every session.
#
# mkdir first: the directory is a mount point when the volume arrives, and
# nothing at all when it does not. Creating it means the process starts either
# way, and the mount report above says which happened - a key on a
# non-persistent path signs sessions that will not survive a restart.
mkdir -p /run/secrets 2>/dev/null || true
if [ ! -w /run/secrets ]; then
    echo "entrypoint: /run/secrets cannot be created or written." >&2
    exit 1
fi
chmod 700 /run/secrets 2>/dev/null || true
if [ "$ROLE" = "worker" ]; then
    # Only the web container writes the key. Two containers each finding it
    # absent and each writing one would leave them signing with different keys.
    waited=0
    while [ ! -f /run/secrets/django_secret_key ]; do
        if [ "$waited" -ge 600 ]; then
            echo "entrypoint: no signing key after 10 minutes; is the web container running?" >&2
            exit 1
        fi
        [ "$waited" -eq 0 ] && echo "entrypoint: waiting for the web container's signing key"
        sleep 5; waited=$((waited + 5))
    done
elif [ ! -f /run/secrets/django_secret_key ]; then
    head -c 64 /dev/urandom | base64 | tr -d '\n=' > /run/secrets/django_secret_key
    chmod 600 /run/secrets/django_secret_key
    echo "entrypoint: generated a signing key"
fi

# ---------------------------------------------------------------- signatures
# processing.py runs clamscan with --fail-if-cvd-older-than=7, so a database
# older than a week rejects every document with "signatures stale" rather than
# letting one through unchecked. Refreshed once now, then daily in the
# background. A failure here is logged and not fatal: an existing database
# inside the window keeps working, and one outside it fails the scan closed,
# which is the behaviour we want anyway.
#
# Only where documents are scanned: not in a web container (the intake lane
# runs in the worker), and not at all when a clamd daemon keeps its own.
FRESHCLAM="${DIGITAL_BRAIN_FRESHCLAM:-1}"
if [ "$ROLE" = "web" ] || [ -n "$CLAMD" ]; then
    FRESHCLAM=0
fi
if [ "$FRESHCLAM" = "1" ]; then
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
# Migrations run in the one web (or all-in-one) container, so there is never a
# second writer to race. A worker waits until they are applied rather than
# starting lanes against a schema that is about to change under them.
if [ "$ROLE" = "worker" ]; then
    waited=0
    until python manage.py migrate --check >/dev/null 2>&1; do
        if [ "$waited" -ge 900 ]; then
            echo "entrypoint: migrations still pending after 15 minutes." >&2
            exit 1
        fi
        [ "$waited" -eq 0 ] && echo "entrypoint: waiting for the web container to apply migrations"
        sleep 5; waited=$((waited + 5))
    done
else
    echo "entrypoint: applying migrations"
    python manage.py migrate --noinput
fi

# Django's own production audit, with warnings fatal. It reads the same
# settings the server is about to use, so a misconfigured proxy or cookie flag
# stops the deploy instead of being discovered by a user.
echo "entrypoint: checking deployment settings"
python manage.py check --deploy --fail-level WARNING

if [ "$ROLE" = "worker" ]; then
    # Comma-separated lane names, or every lane. Each lane is held by one
    # process at a time, so a second worker replica stands by rather than
    # running anything twice.
    exec python manage.py run_worker --lanes "${DIGITAL_BRAIN_LANES:-}"
fi

WORKER_FLAG=""
if [ "$ROLE" = "web" ]; then
    WORKER_FLAG="--no-worker"
fi
exec python scripts/serve.py \
    --host "$HOST" \
    --port "$PORT" \
    --instance "$INSTANCE" \
    --trusted-proxy "${DIGITAL_BRAIN_TRUSTED_PROXY:-*}" \
    $WORKER_FLAG
