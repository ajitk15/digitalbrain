"""Create or repair an isolated development configuration. Never prints generated secrets.

Run on every start, not only the first one. A configuration that already exists is
still checked, because `config/local.toml` records an absolute `secret_directory`
and the file is gitignored: a *clone* regenerates it correctly, but a copied or
moved project folder carries the old machine's path along. Left alone, the next
command fails with "Required mounted secret is unavailable: django_secret_key",
which says nothing about the real cause. Worse, if the old path still exists on
this machine, the move would silently keep reading the old secrets - including
another deployment's AI credentials.

Repair is deliberately narrow: only `secret_directory`, only when the file says
`mode = "development"`, and only by rewriting that one line. Every other key the
operator set - `claude_use_host_login`, `hosts`, `fetch_allow_hosts` - survives.
"""

import os
import secrets
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

TEMPLATE = (
    'mode = "development"\n'
    'hosts = ["localhost", "127.0.0.1", "testserver"]\n'
    'secret_directory = "{expected}"\n'
)


def repair_config(path, expected):
    """Point `secret_directory` at `expected`, leaving every other line alone.

    A line-by-line rewrite rather than a TOML round-trip: the file is edited by
    hand and by scripts/ai_setup.py, and comments an operator wrote are worth
    more than tidy formatting. Returns the previous value, or None if correct.
    """
    with path.open("rb") as source:
        current = tomllib.load(source)
    if current.get("secret_directory") == expected:
        return None
    rewritten, replaced = [], False
    for line in path.read_text(encoding="utf-8").splitlines():
        name, separator, _ = line.partition("=")
        if separator and name.strip() == "secret_directory" and not replaced:
            rewritten.append(f'secret_directory = "{expected}"')
            replaced = True
        else:
            rewritten.append(line)
    if not replaced:
        rewritten.append(f'secret_directory = "{expected}"')
    path.write_text("\n".join(rewritten) + "\n", encoding="utf-8")
    return current.get("secret_directory")


#: docker-compose.dev.yaml's services, as production's configuration names them:
#: the owner connects for migrations and tooling, the runtime role serves. An
#: address, not "localhost": Windows tries ::1 first, which Docker does not
#: publish on, and every connection waited ten seconds for it. The certificates
#: name 127.0.0.1 too.
SERVICES = """\
[database]
name = "digital_brain"
user = "digital_brain_app"
runtime_user = "digital_brain_runtime"
host = "127.0.0.1"
port = 5442
sslrootcert = "{ca}"

[redis]
host = "127.0.0.1"
port = 6390
tls = true
ca_certificate = "{ca}"
"""
CLAMD = 'clamd = "127.0.0.1:3310"'
#: The passwords docker-compose.dev.yaml reads, made once like production's.
SERVICE_SECRETS = ("database_password", "database_runtime_password", "redis_password")


def write_secret(path, value):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as output:
        output.write(value)


def use_services(config, ca):
    """Point the configuration at the development services, once.

    A `[database]` table already there is the developer's own choice and is left
    alone. `clamd` is a top-level key, so it goes before the first table rather
    than after the last, where TOML would read it as part of that table.
    """
    with config.open("rb") as source:
        current = tomllib.load(source)
    if "database" in current:
        return False
    lines = config.read_text(encoding="utf-8").splitlines()
    if "clamd" not in current:
        first_table = next((i for i, line in enumerate(lines) if line.startswith("[")), len(lines))
        # Above the table's own comment, which describes the table.
        while first_table and lines[first_table - 1].startswith("#"):
            first_table -= 1
        lines.insert(first_table, CLAMD)
    text = "\n".join(lines).rstrip("\n") + "\n\n" + SERVICES.format(ca=ca)
    config.write_text(text, encoding="utf-8")
    return True


def runtime_config(config):
    """config/local.runtime.toml: the same file, connecting as the runtime role.

    What the entrypoint does in production - the owner's copy differs only in
    whom it connects as - done in the other direction, because here the owner's
    file is the one people edit. Written on every start, so it never drifts.
    """
    with config.open("rb") as source:
        database = tomllib.load(source).get("database", {})
    if not database.get("runtime_user"):
        return None
    out, table = [], ""
    for line in config.read_text(encoding="utf-8").splitlines():
        if line.startswith("["):
            table = line.strip()
        key = line.partition("=")[0].strip()
        if table == "[database]" and key == "runtime_user":
            continue
        if table == "[database]" and key == "user":
            out.append(f'user = "{database["runtime_user"]}"')
            out.append('password_secret = "database_runtime_password"')
            continue
        out.append(line)
    target = config.with_name("local.runtime.toml")
    header = "# Generated from local.toml by scripts/init_local.py on every start. Do not edit.\n"
    target.write_text(header + "\n".join(out) + "\n", encoding="utf-8")
    return target


def restrict(directory):
    """Owner-only access, by the platform's own mechanism."""
    if os.name == "nt":
        identity = subprocess.check_output(["whoami"], text=True).strip()
        subprocess.run(
            ["icacls", str(directory), "/inheritance:r", "/grant:r", f"{identity}:(OI)(CI)F"],
            check=True,
            capture_output=True,
        )
    else:
        directory.chmod(0o700)


def main(argv=None):
    services = "--services" in (sys.argv[1:] if argv is None else argv)
    secret_dir = ROOT / ".runtime" / "secrets"
    secret_dir.mkdir(parents=True, exist_ok=True)
    restrict(secret_dir)
    key = secret_dir / "django_secret_key"
    if not key.exists():
        descriptor = os.open(key, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as output:
            output.write(secrets.token_urlsafe(64))
    config_dir = ROOT / "config"
    config_dir.mkdir(exist_ok=True)
    config = config_dir / "local.toml"
    expected = secret_dir.as_posix()

    if not config.exists():
        config.write_text(TEMPLATE.format(expected=expected), encoding="utf-8")
        print("Local configuration created.")
    else:
        with config.open("rb") as source:
            current = tomllib.load(source)
        if current.get("mode") != "development":
            sys.stderr.write(
                f"Refusing to touch config/local.toml: mode is {current.get('mode')!r}, "
                "not 'development'.\n"
            )
            return 1
        stale = repair_config(config, expected)
        if stale is not None:
            print(f"Repaired config/local.toml: secret_directory was {stale!r}.")
            print("This project was moved or copied; it now reads secrets from its own folder.")

    if services:
        for name in SERVICE_SECRETS:
            if not (secret_dir / name).exists():
                write_secret(secret_dir / name, secrets.token_urlsafe(32))
        ca = ROOT / ".runtime" / "dev-ca"
        ca.mkdir(parents=True, exist_ok=True)
        if use_services(config, (ca / "ca.crt").as_posix()):
            print("config/local.toml now uses docker-compose.dev.yaml's services.")
        runtime_config(config)

    with config.open("rb") as source:
        assert tomllib.load(source)["mode"] == "development", "Refusing production config."
    print("Local configuration ready. Signing key stored with restricted filesystem permissions.")
    print("No .env was created. No administrator account or default password was created.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
