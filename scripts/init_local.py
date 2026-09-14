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


def main():
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

    with config.open("rb") as source:
        assert tomllib.load(source)["mode"] == "development", "Refusing production config."
    print("Local configuration ready. Signing key stored with restricted filesystem permissions.")
    print("No .env was created. No administrator account or default password was created.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
