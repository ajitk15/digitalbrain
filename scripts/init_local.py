"""Initialize an isolated development configuration. Never prints generated secrets."""

import os
import secrets
import subprocess
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
runtime = ROOT / ".runtime"
secret_dir = runtime / "secrets"
secret_dir.mkdir(parents=True, exist_ok=True)
if os.name == "nt":
    identity = subprocess.check_output(["whoami"], text=True).strip()
    subprocess.run(
        ["icacls", str(secret_dir), "/inheritance:r", "/grant:r", f"{identity}:(OI)(CI)F"],
        check=True,
        capture_output=True,
    )
else:
    secret_dir.chmod(0o700)
key = secret_dir / "django_secret_key"
if not key.exists():
    descriptor = os.open(key, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as output:
        output.write(secrets.token_urlsafe(64))
config_dir = ROOT / "config"
config_dir.mkdir(exist_ok=True)
config = config_dir / "local.toml"
if not config.exists():
    config.write_text(
        'mode = "development"\nhosts = ["localhost", "127.0.0.1", "testserver"]\n'
        f'secret_directory = "{secret_dir.as_posix()}"\n',
        encoding="utf-8",
    )
with config.open("rb") as source:
    assert tomllib.load(source)["mode"] == "development", "Refusing production config."
print("Local configuration ready. Signing key stored with restricted filesystem permissions.")
print("No .env was created. No administrator account or default password was created.")
