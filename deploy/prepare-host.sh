#!/bin/sh
# Prepare the VPS for a Digital Brain deployment. Run once, as root.
#
# Creates the two directories the compose file bind-mounts, generates the
# platform secrets, and sets the ownership and permissions
# digitalbrain/configuration.py:read_secret insists on. It refuses any file
# carrying group or other permission bits, and the container runs as uid 10001,
# so both have to be right or the process will not start at all - which is the
# check working, not a bug.
#
# It never overwrites a secret that already exists. Rotating one is a deliberate
# act with consequences: replacing django_secret_key invalidates every session.
set -eu

BASE="${DIGITAL_BRAIN_BASE:-/opt/digitalbrain}"
SECRETS="$BASE/secrets"
CONFIG="$BASE/config"
UID_BRAIN=10001
GID_BRAIN=10001

if [ "$(id -u)" -ne 0 ]; then
    echo "Run this as root: it sets ownership to uid $UID_BRAIN." >&2
    exit 1
fi

mkdir -p "$SECRETS" "$CONFIG"

# 0700 on the directory as well as 0600 on each file: a directory anyone can
# list is a directory anyone can learn the application UUIDs from.
chown "$UID_BRAIN:$GID_BRAIN" "$SECRETS"
chmod 700 "$SECRETS"
chmod 755 "$CONFIG"

write_secret() {
    name="$1"
    value="$2"
    path="$SECRETS/$name"
    if [ -f "$path" ]; then
        echo "keeping existing $name"
        return 0
    fi
    printf '%s' "$value" > "$path"
    chown "$UID_BRAIN:$GID_BRAIN" "$path"
    chmod 600 "$path"
    echo "wrote $name"
}

# At least 50 characters is enforced in settings.py; 64 random bytes of base64
# is comfortably past it and comes from the kernel rather than a shell $RANDOM.
write_secret django_secret_key "$(head -c 64 /dev/urandom | base64 | tr -d '\n=' )"
write_secret database_password "$(head -c 32 /dev/urandom | base64 | tr -d '\n=/+' )"

echo
echo "Secrets are in $SECRETS (owner-only, uid $UID_BRAIN)."
echo "Now copy deploy/production.example.toml to $CONFIG/production.toml and edit it."
echo
echo "Per-application provider credentials go in the same directory, one file"
echo "per provider per application, named with the application's UUID:"
echo "  claude_<APPLICATION_UUID>        an API key, or a 'claude setup-token' OAuth token"
echo "  openai_<APPLICATION_UUID>"
echo "  github_<APPLICATION_UUID>        read-only, for connectors and Code Graph"
echo "  github_write_<APPLICATION_UUID>  contents + pull request write; deliberately separate"
echo
echo "Each must be chown $UID_BRAIN:$GID_BRAIN and chmod 600, or it is rejected"
echo "as unreadable rather than silently ignored."
