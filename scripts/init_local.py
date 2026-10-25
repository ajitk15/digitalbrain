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

Every start also adds what a development machine switches on (`DEVELOPMENT`),
but only where the file does not mention the key: a key written down, true or
false, is the developer's. Each of them is refused by `load_config` under
production, or harmless there.

`--services` points the file at docker-compose.dev.yaml's services once.
`--no-services` is the start without Docker: when local.toml names those
services it writes `config/local.nodocker.toml`, the same file without them, and
start-all runs everything with that.
"""

import json
import os
import secrets
import shutil
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


def first_table(lines):
    """Where a top-level key goes: before the first table and the comment above it.

    After the last line TOML would read it as part of whichever table ends the
    file - `[redis]` refuses any key it does not know.
    """
    index = next((i for i, line in enumerate(lines) if line.startswith("[")), len(lines))
    while index and lines[index - 1].startswith("#"):
        index -= 1
    return index


#: Windows' default Tesseract location, named when Tesseract is not on PATH.
TESSERACT = Path("C:/Program Files/Tesseract-OCR/tesseract.exe")


def local_roots(root):
    """The folder that holds this checkout, for Code Graph's server folders.

    Its parent, so a repository beside this one can be indexed - unless that is
    a drive root, which `load_config` refuses: then the checkout itself, which
    holds demo-artifacts/carepath.
    """
    parent = root.parent
    return [(root if parent == Path(parent.anchor) else parent).as_posix()]


def development(root):
    """(key, TOML value, comment) a development configuration switches on."""
    managed = (root / ".runtime" / "secrets-managed").as_posix()
    wanted = [
        (
            "managed_secret_directory",
            json.dumps(managed),
            "Where a credential entered on the Credentials screen is written. Must differ\n"
            "# from secret_directory: a mounted credential wins over one typed in a browser.",
        ),
        (
            "claude_use_host_login",
            "true",
            "Development only: use this machine's Claude Code login (its subscription)\n"
            "# when an application has no mounted Claude credential. Refused in production.",
        ),
        (
            "allow_demo_reset",
            "true",
            "Development only: let a platform administrator empty an organization between\n"
            "# demonstrations. Refused in production.",
        ),
        (
            "code_graph_local_roots",
            json.dumps(local_roots(root)),
            "Folders Code Graph may index a repository from (\"From a folder on this\n"
            "# server\"). Any owner or contributor can read code anywhere under these.",
        ),
        (
            "code_factory_local_write",
            "true",
            "Development only: let Code Factory write an approved change into the Code\n"
            "# Graph folder it read. Refused in production, where it opens a pull request.",
        ),
        (
            "code_factory_local_tests",
            "true",
            "Development only: let an approver run that folder's own pytest suite after\n"
            "# Code Factory wrote into it. Refused in production, where CI runs tests.",
        ),
        (
            "ocr",
            "true",
            "Read scanned PDF pages and images with a local Tesseract. Nothing is read by\n"
            "# OCR until one is installed: winget install --id UB-Mannheim.TesseractOCR -e",
        ),
    ]
    if not shutil.which("tesseract") and TESSERACT.is_file():
        wanted.append(
            ("tesseract_path", json.dumps(TESSERACT.as_posix()), "Tesseract, not on PATH.")
        )
    return wanted


def switch_on(path, root):
    """Add every `development` key the file does not mention. Returns the keys added."""
    with path.open("rb") as source:
        current = tomllib.load(source)
    added = [entry for entry in development(root) if entry[0] not in current]
    if not added:
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    block = []
    for key, value, comment in added:
        block += [f"# {comment}", f"{key} = {value}"]
    at = first_table(lines)
    lines[at:at] = block + ([""] if at < len(lines) else [])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return [key for key, _, _ in added]


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
        lines.insert(first_table(lines), CLAMD)
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


#: Tables and top-level keys that name docker-compose.dev.yaml's services. Without
#: them Django opens `.runtime/local.sqlite3`, `web_server` falls back to waitress,
#: and in development nothing scans an upload (production always does).
SERVICE_TABLES = ("[database]", "[redis]")
SERVICE_KEYS = ("clamd", "scan_documents")
#: The SQLite file as it was before the first start without Docker, kept when
#: the data had been moved to PostgreSQL - local.sqlite3 is that move's backup.
SQLITE = ROOT / ".runtime" / "local.sqlite3"
MOVED = ROOT / ".runtime" / "moved-to-postgres"
BEFORE = ROOT / ".runtime" / "local.before-nodocker.sqlite3"


def without_services(config):
    """config/local.nodocker.toml: local.toml without the Docker services, or None.

    Written on every start without Docker, so it never drifts from local.toml,
    and removed when local.toml names no service - a machine that never ran
    Docker runs local.toml itself.
    """
    with config.open("rb") as source:
        current = tomllib.load(source)
    target = config.with_name("local.nodocker.toml")
    if not any(name in current for name in ("database", "redis", *SERVICE_KEYS)):
        target.unlink(missing_ok=True)
        return None
    out, table = [], ""
    for line in config.read_text(encoding="utf-8").splitlines():
        if line.startswith("["):
            table = line.strip()
        service = table in SERVICE_TABLES or (
            not table and line.partition("=")[0].strip() in SERVICE_KEYS
        )
        if service:
            # With the comment above it, which describes the service.
            while out and out[-1].startswith("#"):
                out.pop()
            continue
        out.append(line)
    header = (
        "# Generated from local.toml by scripts/init_local.py on a start without Docker.\n"
        "# Do not edit: SQLite, no Redis, no virus scanner.\n"
    )
    target.write_text(header + "\n".join(out).rstrip("\n") + "\n", encoding="utf-8")
    return target


def keep_backup():
    """Copy local.sqlite3 aside once, before a start without Docker writes to it.

    After `move_to_postgres.py` the file is the backup of what was moved, so it
    is copied first. Returns the date of the move when there was one.
    """
    if not MOVED.exists():
        return None
    if SQLITE.exists() and not BEFORE.exists():
        shutil.copy2(SQLITE, BEFORE)
        print(f"Kept the SQLite backup as {BEFORE.relative_to(ROOT).as_posix()}.")
    return MOVED.read_text(encoding="utf-8").strip() or "an earlier start"


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
    arguments = sys.argv[1:] if argv is None else argv
    services = "--services" in arguments
    local = "--no-services" in arguments
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

    added = switch_on(config, ROOT)
    if added:
        print("Switched on for development: " + ", ".join(added) + ".")
    with config.open("rb") as source:
        managed = tomllib.load(source).get("managed_secret_directory")
    if managed:
        Path(managed).mkdir(parents=True, exist_ok=True)
        restrict(Path(managed))

    if local:
        if without_services(config):
            print("Without Docker: config/local.nodocker.toml is local.toml without")
            print("PostgreSQL, Redis and ClamAV - SQLite, waitress, uploads not scanned.")
            moved = keep_backup()
            if moved:
                print(f"The data was moved to PostgreSQL on {moved}; this start reads")
                print("the SQLite copy from before then. Start Docker to see the rest.")
    elif services:
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
