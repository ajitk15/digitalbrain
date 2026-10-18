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
# "migrate" applies them, writes the signing key if there is none, checks the
# deployment settings and exits - the one-shot job docker-compose.scale.yaml
# runs before any web or worker container starts.
ROLE="${DIGITAL_BRAIN_ROLE:-all}"
case "$ROLE" in
    all|web|worker|migrate) ;;
    *) echo "entrypoint: DIGITAL_BRAIN_ROLE must be all, web, worker or migrate." >&2; exit 1 ;;
esac
# Whether this container applies migrations or waits for them. A worker always
# waits. A web container applies them unless a migrate job does (then "wait"),
# which is what lets it run as more than one replica. Applying takes a lock
# either way (manage.py apply_migrations), so two at once is safe, not just rare.
MIGRATIONS="${DIGITAL_BRAIN_MIGRATIONS:-apply}"
case "$MIGRATIONS" in
    apply|wait) ;;
    *) echo "entrypoint: DIGITAL_BRAIN_MIGRATIONS must be apply or wait." >&2; exit 1 ;;
esac
[ "$ROLE" = "worker" ] && MIGRATIONS="wait"
[ "$ROLE" = "migrate" ] && MIGRATIONS="apply"
# A clamd daemon to scan through (host:port or an absolute socket path). Set,
# no clamscan or freshclam runs in this container: the daemon keeps its own
# signatures and processing.py still refuses ones more than a week old.
CLAMD="${DIGITAL_BRAIN_CLAMD:-}"
# Redis as host:port. Set, chat answers stream through it and resume after a
# dropped connection, and Stop and the API rate limit reach every process.
REDIS="${DIGITAL_BRAIN_REDIS:-}"
case "$REDIS" in
    ""|*[!A-Za-z0-9.:-]*) [ -z "$REDIS" ] || { echo "entrypoint: DIGITAL_BRAIN_REDIS must be host:port." >&2; exit 1; } ;;
    *:*) ;;
    *) echo "entrypoint: DIGITAL_BRAIN_REDIS must be host:port." >&2; exit 1 ;;
esac
# An OTLP/HTTP collector as host:port (docker-compose.observability.yaml runs
# one). Set, traces and metrics go to it over https, its certificate issued by
# the same CA as Postgres's. Unset, nothing is sent and the SDK is never loaded.
OTEL="${DIGITAL_BRAIN_OTEL:-}"
case "$OTEL" in
    ""|*[!A-Za-z0-9.:-]*) [ -z "$OTEL" ] || { echo "entrypoint: DIGITAL_BRAIN_OTEL must be host:port." >&2; exit 1; } ;;
    *:*) ;;
    *) echo "entrypoint: DIGITAL_BRAIN_OTEL must be host:port." >&2; exit 1 ;;
esac

# Where uploaded originals live: an S3 bucket (and its region), the same store
# development uses. The keys are the mounted files originals_access_key_id and
# originals_secret_access_key, never variables. Unset, they stay on the
# app-runtime volume as before.
BUCKET="${DIGITAL_BRAIN_ORIGINALS_BUCKET:-}"
REGION="${DIGITAL_BRAIN_ORIGINALS_REGION:-us-west-2}"
case "$BUCKET" in
    ""|[a-z0-9]*[a-z0-9]) ;;
    *) echo "entrypoint: DIGITAL_BRAIN_ORIGINALS_BUCKET must be a bucket name." >&2; exit 1 ;;
esac
case "$BUCKET" in *[!a-z0-9.-]*) echo "entrypoint: DIGITAL_BRAIN_ORIGINALS_BUCKET must be a bucket name." >&2; exit 1 ;; esac
case "$REGION" in ""|*[!a-z0-9-]*) echo "entrypoint: DIGITAL_BRAIN_ORIGINALS_REGION must be a region name." >&2; exit 1 ;; esac

# Where the migrate job writes the secrets every other container reads
# (deploy/runtime_secrets.py). docker-compose.scale.yaml sets it: there only
# secrets-init and the migrate job mount the store, platform-secrets, which
# holds the owner's database password, and the web, worker and sandbox
# containers mount the set written here at /run/secrets instead. Unset - the
# all-in-one container - nothing is copied and /run/secrets is the store.
RUNTIME_SECRETS="${DIGITAL_BRAIN_RUNTIME_SECRETS:-}"
case "$RUNTIME_SECRETS" in
    "") ;;
    /run/secrets|/run/secrets/*) echo "entrypoint: DIGITAL_BRAIN_RUNTIME_SECRETS must not be inside /run/secrets." >&2; exit 1 ;;
    /*) [ "$ROLE" = "migrate" ] || { echo "entrypoint: only the migrate job writes DIGITAL_BRAIN_RUNTIME_SECRETS." >&2; exit 1; } ;;
    *) echo "entrypoint: DIGITAL_BRAIN_RUNTIME_SECRETS must be an absolute path." >&2; exit 1 ;;
esac

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
# What the migrate step reads: the same, but connecting as the owner of the
# tables, and naming the runtime role it creates for everything else. Only
# rendered, never mounted - an operator's own file is used for both.
OWNER_CONFIG="/app/.runtime/production-owner.toml"
# The platform connects as a role that can read and write rows and do nothing
# else (platform_core/database_roles.py); the role Postgres's image creates is
# a superuser, which can do anything the server can and is never held to row
# security. DIGITAL_BRAIN_DATABASE_RUNTIME_ROLE=0 connects as the owner again -
# set it on every service of a split deployment, because the migrate job then
# gives the owner's password to the runtime secret set instead of the runtime
# role's.
RUNTIME_ROLE="${DIGITAL_BRAIN_DATABASE_RUNTIME_ROLE:-1}"
case "$RUNTIME_ROLE" in
    0|1) ;;
    *) echo "entrypoint: DIGITAL_BRAIN_DATABASE_RUNTIME_ROLE must be 0 or 1." >&2; exit 1 ;;
esac
mkdir -p /app/.runtime

# Demo is an explicit production capability, not development mode. Keep TLS,
# secure cookies, scanning and the restricted database role in force.
DEMONSTRATION="${DIGITAL_BRAIN_DEMONSTRATION:-0}"
case "$DEMONSTRATION" in
    0|1) ;;
    *) echo "entrypoint: DIGITAL_BRAIN_DEMONSTRATION must be 0 or 1." >&2; exit 1 ;;
esac
if [ "$DEMONSTRATION" = "1" ] && [ "$MIGRATIONS" = "apply" ]; then
    python scripts/init_vps_demo.py
fi

if [ -f "$MOUNTED" ]; then
    cp "$MOUNTED" "$CONFIG.$$" && mv "$CONFIG.$$" "$CONFIG"
    cp "$CONFIG" "$OWNER_CONFIG.$$" && mv "$OWNER_CONFIG.$$" "$OWNER_CONFIG"
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
        if [ "$DEMONSTRATION" = "1" ]; then
            echo 'demonstration = true'
            echo 'allow_demo_reset = true'
            echo 'code_factory_local_write = true'
            echo 'code_factory_local_tests = true'
            echo 'code_graph_local_roots = ["/var/lib/digitalbrain/demo"]'
            echo 'connector_secret_directory = "/var/lib/digitalbrain/connectors"'
        fi
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
        # Redis does that and more (answers resume after a dropped connection),
        # so the database lease is only for a split deployment without it.
        if [ "$ROLE" != "all" ] && [ -z "$REDIS" ]; then
            echo 'shared_streams = true'
        fi
        echo "import_max_files = ${DIGITAL_BRAIN_IMPORT_MAX_FILES:-100}"
        # Scanned PDF pages and images are read by the Tesseract this image
        # installs, as they are in development. DIGITAL_BRAIN_OCR=0 turns it off.
        if [ "${DIGITAL_BRAIN_OCR:-1}" = "1" ]; then
            echo 'ocr = true'
        fi
        # Sign in with Microsoft Entra ID, pinned to one tenant (sso.py): both
        # ids or neither, which load_config checks; the client secret is the
        # mounted entra_client_secret. DIGITAL_BRAIN_SCIM=1 also lets the
        # directory provision accounts (scim.py) - creates and deactivates,
        # never grants - with its bearer token the mounted scim_token.
        if [ -n "${DIGITAL_BRAIN_ENTRA_TENANT:-}" ]; then
            echo "entra_tenant = \"$DIGITAL_BRAIN_ENTRA_TENANT\""
            echo "entra_client_id = \"${DIGITAL_BRAIN_ENTRA_CLIENT_ID:-}\""
            if [ "${DIGITAL_BRAIN_SCIM:-0}" = "1" ]; then
                echo 'scim_provisioning = true'
            fi
        fi
        echo ''
        echo '[database]'
        echo 'name = "digital_brain"'
        if [ "$RUNTIME_ROLE" = "1" ]; then
            echo 'user = "digital_brain_runtime"'
            echo 'password_secret = "database_runtime_password"'
        else
            echo 'user = "digital_brain_app"'
        fi
        # Must match the certificate's SAN exactly, or sslmode=verify-full
        # rejects a certificate that is otherwise perfectly valid.
        echo 'host = "postgres"'
        echo 'port = 5432'
        echo 'sslrootcert = "/var/lib/digitalbrain/pgcerts/ca.crt"'
        if [ -n "$REDIS" ]; then
            # The password is the mounted redis_password file, never here. The
            # certificate is issued by the same CA as Postgres's, for "redis".
            echo ''
            echo '[redis]'
            echo "host = \"${REDIS%%:*}\""
            echo "port = ${REDIS##*:}"
            echo 'tls = true'
            echo 'ca_certificate = "/var/lib/digitalbrain/pgcerts/ca.crt"'
        fi
        if [ -n "$BUCKET" ]; then
            echo ''
            echo '[originals]'
            echo 'store = "s3"'
            echo "bucket = \"$BUCKET\""
            echo "region = \"$REGION\""
        fi
        if [ -n "$OTEL" ]; then
            # Ids and timings only (telemetry.ATTRIBUTES); a collector's
            # credential, if it needs one, is the mounted otel_authorization.
            echo ''
            echo '[otel]'
            echo "endpoint = \"https://$OTEL\""
            echo 'ca_certificate = "/var/lib/digitalbrain/pgcerts/ca.crt"'
        fi
    } > "$CONFIG.$$"
    # Written aside and moved into place: a web and a worker container share
    # this volume, and neither may read the other's half-written file.
    mv "$CONFIG.$$" "$CONFIG"
    # The owner's copy differs only in whom it connects as.
    if [ "$RUNTIME_ROLE" = "1" ]; then
        sed -e 's/^user = "digital_brain_runtime"$/user = "digital_brain_app"\nruntime_user = "digital_brain_runtime"/' \
            -e '/^password_secret = /d' "$CONFIG" > "$OWNER_CONFIG.$$"
    else
        cp "$CONFIG" "$OWNER_CONFIG.$$"
    fi
    mv "$OWNER_CONFIG.$$" "$OWNER_CONFIG"
    echo "entrypoint: rendered configuration for ${DIGITAL_BRAIN_HOSTS:-localhost}"
fi
export DIGITAL_BRAIN_CONFIG="$CONFIG"
if grep -q '^store = "s3"$' "$CONFIG"; then
    for name in originals_access_key_id originals_secret_access_key; do
        [ -f "/run/secrets/$name" ] || echo "entrypoint: originals are in S3 but /run/secrets/$name is not mounted; uploads will fail until it is." >&2
    done
fi
if grep -q '^entra_tenant = ' "$CONFIG"; then
    [ -f /run/secrets/entra_client_secret ] || echo "entrypoint: Microsoft sign-in is configured but /run/secrets/entra_client_secret is not mounted; every Microsoft sign-in will fail until it is." >&2
fi
if grep -q '^scim_provisioning = true$' "$CONFIG"; then
    [ -f /run/secrets/scim_token ] || echo "entrypoint: SCIM provisioning is on but /run/secrets/scim_token is not mounted; the directory's calls are refused until it is." >&2
fi

# ------------------------------------------------------------- what is mounted
# Printed every start, because this deployment has now failed three times on
# something that is invisible from inside the application: whether the volumes
# the compose file declares actually arrived. A deployment platform is free to
# rewrite or drop them, and "Directory nonexistent" for a path the compose file
# mounts is the kind of thing worth one line of output forever.
echo "entrypoint: mounts"
for path in /run/secrets $RUNTIME_SECRETS /app/.runtime /var/lib/clamav /var/lib/digitalbrain/credentials; do
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
# Only whoever applies the migrations writes the platform's own secrets here. A
# container that waits for them - every worker, the sandbox, a web replica
# beside a migrate job - only reads them, so it may be given the secrets
# read-only, as the worker and the sandbox are. In a split deployment what it
# reads is the migrate job's runtime set, not the store (RUNTIME_SECRETS above).
if [ "$MIGRATIONS" = "apply" ] && [ ! -w /run/secrets ]; then
    echo "entrypoint: /run/secrets cannot be created or written." >&2
    exit 1
fi
if [ ! -r /run/secrets ] || [ ! -x /run/secrets ]; then
    echo "entrypoint: /run/secrets cannot be read." >&2
    exit 1
fi
chmod 700 /run/secrets 2>/dev/null || true
if [ "$MIGRATIONS" = "wait" ]; then
    # Whoever applies the migrations writes the key - in a split deployment,
    # into the runtime set this container reads. A container that waits for
    # them waits for the key too, so it never starts signing with one of its own.
    waited=0
    while [ ! -f /run/secrets/django_secret_key ]; do
        if [ "$waited" -ge 600 ]; then
            echo "entrypoint: no signing key after 10 minutes; has the migrate job or the web container run?" >&2
            exit 1
        fi
        [ "$waited" -eq 0 ] && echo "entrypoint: waiting for the signing key"
        sleep 5; waited=$((waited + 5))
    done
elif [ ! -f /run/secrets/django_secret_key ]; then
    # Written aside and linked into place: a link fails if the name exists, so
    # two containers finding it absent at once still end up with one key, the
    # first one's, rather than each signing with the key it wrote.
    head -c 64 /dev/urandom | base64 | tr -d '\n=' > "/run/secrets/.django_secret_key.$$"
    chmod 600 "/run/secrets/.django_secret_key.$$"
    if ln "/run/secrets/.django_secret_key.$$" /run/secrets/django_secret_key 2>/dev/null; then
        echo "entrypoint: generated a signing key"
    fi
    rm -f "/run/secrets/.django_secret_key.$$"
fi
# The runtime role's password, made the same way where secrets-init has not.
if [ "$MIGRATIONS" = "apply" ] && [ ! -f /run/secrets/database_runtime_password ]; then
    head -c 32 /dev/urandom | base64 | tr -d '\n=+/' > "/run/secrets/.database_runtime_password.$$"
    chmod 600 "/run/secrets/.database_runtime_password.$$"
    ln "/run/secrets/.database_runtime_password.$$" /run/secrets/database_runtime_password 2>/dev/null || true
    rm -f "/run/secrets/.database_runtime_password.$$"
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
if [ "$ROLE" = "web" ] || [ "$ROLE" = "migrate" ] || [ -n "$CLAMD" ]; then
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
# Applied under a lock (manage.py apply_migrations), so a second container
# applying at the same moment waits and then finds nothing left to do. One that
# waits instead - every worker, and a web replica beside a migrate job - does
# not start against a schema that is about to change under it.
if [ "$MIGRATIONS" = "wait" ]; then
    waited=0
    until python manage.py migrate --check >/dev/null 2>&1; do
        if [ "$waited" -ge 900 ]; then
            echo "entrypoint: migrations still pending after 15 minutes." >&2
            exit 1
        fi
        [ "$waited" -eq 0 ] && echo "entrypoint: waiting for migrations to be applied"
        sleep 5; waited=$((waited + 5))
    done
else
    echo "entrypoint: applying migrations"
    # As the owner; the run also creates the runtime role and grants it every
    # table, so it exists before anything below connects as it.
    DIGITAL_BRAIN_CONFIG="$OWNER_CONFIG" python manage.py apply_migrations
fi

# Django's own production audit, with warnings fatal. It reads the same
# settings the server is about to use, so a misconfigured proxy or cookie flag
# stops the deploy instead of being discovered by a user.
echo "entrypoint: checking deployment settings"
python manage.py check --deploy --fail-level WARNING

if [ "$ROLE" = "migrate" ]; then
    if [ -n "$RUNTIME_SECRETS" ]; then
        # Last, once the runtime role's password is the one the server holds:
        # the signing key, the Redis password, the database password the
        # runtime configuration names and every credential, but never the
        # other database password - the owner's, unless
        # DIGITAL_BRAIN_DATABASE_RUNTIME_ROLE=0 makes it the one they connect with.
        python /app/deploy/runtime_secrets.py "$CONFIG" "$RUNTIME_SECRETS"
    fi
    echo "entrypoint: migrations applied; nothing else runs in this container"
    exit 0
fi

if [ "$ROLE" = "worker" ]; then
    # Comma-separated lane names, or every lane - docker-compose.scale.yaml
    # gives the worker and the sandbox one share each. Every lane but
    # maintenance claims row by row and runs in every replica; maintenance is
    # held by one process at a time, so a second stands by for it.
    # DIGITAL_BRAIN_CONCURRENCY ("intake=4") gives a lane more threads.
    exec python manage.py run_worker --lanes "${DIGITAL_BRAIN_LANES:-}"         --concurrency "${DIGITAL_BRAIN_CONCURRENCY:-}"
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
