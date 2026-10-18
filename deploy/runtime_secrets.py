"""Write the secrets the web, worker and sandbox containers read, and no others.

Run by the migrate job (deploy/entrypoint.sh, DIGITAL_BRAIN_ROLE=migrate) in the
split deployment, docker-compose.scale.yaml. There the `platform-secrets` volume
is the store: secrets-init and the migrate job generate into it, and it holds the
owner's database password - in the Postgres image, a superuser's. Only those two
mount it. Every other container mounts `runtime-secrets` at /run/secrets instead,
and this is what fills it:

* the signing key, the Redis password and the one database password the runtime
  configuration names (`password_secret`), copied from the store on every run,
  because they are made there and rotated there;
* every other file - an application's provider, GitHub or connector credential,
  the SSO client secret - copied when it is absent or the store's copy is newer.
  After the first run the runtime set is where an operator mounts one (through
  the web container, which mounts it writable), and an older copy left in the
  store must not undo that;
* the previous signing key, while the store has one, on every run as well;
* never the other database password: the owner's, unless the runtime
  configuration connects as the owner (DIGITAL_BRAIN_DATABASE_RUNTIME_ROLE=0),
  and the runtime role's when it is not the one used. Either is removed if found.

Standalone, like healthcheck.py - no Django - and it prints names, never values.

    python deploy/runtime_secrets.py <configuration> <runtime secret directory>
"""

import os
import sys
import tomllib
from pathlib import Path

#: The two files a database password may be read from. Must equal
#: digitalbrain.configuration.DATABASE_PASSWORDS, which a test checks.
DATABASE_PASSWORDS = frozenset({"database_password", "database_runtime_password"})
#: Made in the store, so the store's copy always wins. The Redis password is
#: there only when the deployment runs Redis.
GENERATED = ("django_secret_key", "redis_password")
REQUIRED = ("django_secret_key",)
#: Rotated in the store beside the signing key, so it follows the store both
#: ways: copied every run, and removed once the store no longer has it - an old
#: key left here would go on signing in sessions after the rotation ended.
ROTATED = ("django_secret_key_previous",)


def runtime_password(config):
    """The database password file the runtime configuration connects with."""
    name = config.get("database", {}).get("password_secret", "database_password")
    if name not in DATABASE_PASSWORDS:
        raise SystemExit(f"runtime secrets: unknown database.password_secret {name!r}")
    return name


def copy(source, destination):
    """Replace `destination` with `source`'s bytes, mode 0600 from the start."""
    data = source.read_bytes()
    partial = destination.with_name(f".{destination.name}.{os.getpid()}")
    descriptor = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(data)
        # The store's time, so "newer" compares the two edits rather than the copy.
        stat = source.stat()
        os.utime(partial, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        os.replace(partial, destination)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise


def write(store, target, password):
    """Bring `target` up to date from `store`. Returns what changed, by name."""
    store, target = Path(store), Path(target)
    withheld = DATABASE_PASSWORDS - {password}
    always = {*GENERATED, *ROTATED, password}
    for name in (*REQUIRED, password):
        if not (store / name).is_file():
            raise SystemExit(f"runtime secrets: {name} is not in {store}")
    changed = {"written": [], "removed": []}
    ended = {name for name in ROTATED if not (store / name).is_file()}
    for name in sorted(withheld | ended):
        if (target / name).exists():
            (target / name).unlink()
            changed["removed"].append(name)
    for source in sorted(store.iterdir()):
        name = source.name
        # Hidden names are another writer's partial file, or a secret
        # projection's own bookkeeping; directories are never secrets.
        if name.startswith(".") or name in withheld or not source.is_file():
            continue
        destination = target / name
        if destination.is_file():
            same = destination.read_bytes() == source.read_bytes()
            newer = source.stat().st_mtime_ns > destination.stat().st_mtime_ns
            if same or not (name in always or newer):
                continue
        copy(source, destination)
        changed["written"].append(name)
    try:
        target.chmod(0o700)
    except OSError:
        pass
    return changed


def main(argv):
    if len(argv) != 2:
        raise SystemExit("usage: runtime_secrets.py <configuration> <runtime secret directory>")
    config = tomllib.loads(Path(argv[0]).read_text(encoding="utf-8"))
    password = runtime_password(config)
    changed = write(config["secret_directory"], argv[1], password)
    withheld = ", ".join(sorted(DATABASE_PASSWORDS - {password}))
    print(f"runtime secrets: {argv[1]} has {password}, never {withheld}")
    for verb in ("written", "removed"):
        if changed[verb]:
            print(f"  {verb}: {', '.join(changed[verb])}")


if __name__ == "__main__":
    main(sys.argv[1:])
