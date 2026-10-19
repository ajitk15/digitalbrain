"""Copy this machine's SQLite database into the development PostgreSQL, once.

Run by start-all.cmd after `apply_migrations`, while config/local.toml points at
docker-compose.dev.yaml's PostgreSQL. It reads `.runtime/local.sqlite3` through a
configuration of its own with no `[database]` table - migrating it to the current
schema first, so the export matches the tables it is loaded into - exports every
row but content types, permissions, sessions and sign-in lockouts (made again
by migrations, or not worth keeping), and loads the export as the owner.

Copied, never moved: the SQLite file stays where it was, as the backup - migrated
forward, as every start before this one did, and otherwise untouched.
A database that already holds a user is never loaded into - two histories are not
merged - and `.runtime/moved-to-postgres` records that the copy was made, so it
runs once. Secrets are files and are not part of either database.
"""

import os
import subprocess
import sys
import tomllib
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUNTIME = ROOT / ".runtime"
SQLITE = RUNTIME / "local.sqlite3"
MARKER = RUNTIME / "moved-to-postgres"
EXPORT = RUNTIME / "sqlite-export.json"
EXCLUDED = ("contenttypes", "auth.permission", "sessions", "axes")


def sqlite_config():
    """A copy of local.toml without the services, so Django opens the SQLite file."""
    source = Path(os.environ.get("DIGITAL_BRAIN_CONFIG", ROOT / "config" / "local.toml"))
    current = tomllib.loads(source.read_text(encoding="utf-8"))
    path = RUNTIME / "sqlite.toml"
    path.write_text(
        'mode = "development"\n'
        f"hosts = {json_list(current.get('hosts', ['localhost']))}\n"
        f'secret_directory = "{current["secret_directory"]}"\n',
        encoding="utf-8",
    )
    return path


def json_list(values):
    return "[" + ", ".join(f'"{value}"' for value in values) + "]"


def manage(config, *arguments):
    environment = {**os.environ, "DIGITAL_BRAIN_CONFIG": str(config)}
    subprocess.run([sys.executable, str(ROOT / "manage.py"), *arguments], check=True,
                   cwd=ROOT, env=environment)


def main():
    if MARKER.exists():
        print("found   the SQLite data was copied to PostgreSQL on " +
              MARKER.read_text(encoding="utf-8").strip())
        return 0
    sys.path.insert(0, str(ROOT))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "digitalbrain.settings")
    import django

    django.setup()
    from django.conf import settings
    from django.core.management import call_command

    from platform_core.models import User

    if settings.DATABASES["default"]["ENGINE"] != "django.db.backends.postgresql":
        print("skipped this configuration is not PostgreSQL; nothing to copy into")
        return 0
    if not SQLITE.exists():
        MARKER.write_text(f"{datetime.now(UTC):%Y-%m-%d %H:%M} UTC (no SQLite file)\n",
                          encoding="utf-8")
        print("found   no SQLite database to copy")
        return 0
    if User.objects.exists():
        print("skipped PostgreSQL already holds users; the SQLite data is not merged into it.")
        print(f"        {SQLITE} is untouched.")
        return 0
    config = sqlite_config()
    print(f"copying {SQLITE} into PostgreSQL (the file stays as it is)")
    try:
        manage(config, "migrate", "--noinput", "-v", "0")
        manage(config, "dumpdata", "--natural-foreign", "--natural-primary",
               *[f"--exclude={name}" for name in EXCLUDED], "--output", str(EXPORT))
        # One transaction: a load that fails leaves PostgreSQL empty, so the
        # next start tries again rather than finding half a copy.
        call_command("loaddata", str(EXPORT), verbosity=1)
    finally:
        config.unlink(missing_ok=True)
        EXPORT.unlink(missing_ok=True)
    # Audit rows older than the months made ahead land in the default partition;
    # this moves them where they belong, as a deploy does.
    call_command("partitions")
    MARKER.write_text(f"{datetime.now(UTC):%Y-%m-%d %H:%M} UTC\n", encoding="utf-8")
    print(f"copied  every row; {SQLITE} kept as the backup")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
