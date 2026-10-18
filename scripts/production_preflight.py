"""Check production security defaults with ephemeral test-only mounted secrets.

Does not connect to PostgreSQL or validate any real deployment.
"""

import os
import secrets
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
with tempfile.TemporaryDirectory() as temporary:
    directory = Path(temporary)
    for name in ("django_secret_key", "database_password", "database_runtime_password"):
        secret = directory / name
        secret.write_text(secrets.token_urlsafe(64))
        secret.chmod(0o600)
    base = (
        'mode = "production"\nhosts = ["brain.example.com"]\n'
        'csrf_origins = ["https://brain.example.com"]\n'
        f'secret_directory = "{directory.as_posix()}"\n'
        '[database]\nname="test"\nhost="postgres.example.com"\n'
        'sslrootcert="/etc/ssl/certs/ca.pem"\n'
    )
    # Both configurations the entrypoint renders: the one everything runs as
    # (the runtime role), and the owner's copy the migrate step reads.
    for name, database in [
        ("runtime", 'user="test_runtime"\npassword_secret="database_runtime_password"\n'),
        ("owner", 'user="test"\nruntime_user="test_runtime"\n'),
    ]:
        config = directory / f"production-{name}.toml"
        config.write_text(base + database)
        env = os.environ.copy()
        env["DIGITAL_BRAIN_CONFIG"] = str(config)
        subprocess.run(
            [sys.executable, str(ROOT / "manage.py"), "check", "--deploy", "--fail-level",
             "WARNING"],
            env=env,
            cwd=ROOT,
            check=True,
        )
print("Production defaults passed Django checks. Real infrastructure is not validated.")
