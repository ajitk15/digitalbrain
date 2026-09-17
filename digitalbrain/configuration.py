"""Non-secret TOML configuration and mounted secrets; no dotenv secret loading."""

import os
import re
import tomllib
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured
from django.views.decorators.debug import sensitive_variables

ROOT = Path(__file__).resolve().parent.parent


def load_config():
    path = Path(os.environ.get("DIGITAL_BRAIN_CONFIG", ROOT / "config/local.toml"))
    if not path.is_file():
        raise ImproperlyConfigured("Configuration missing. Run scripts/init_local.py first.")
    with path.open("rb") as source:
        config = tomllib.load(source)
    allowed = {
        "mode",
        "hosts",
        "csrf_origins",
        "secret_directory",
        "database",
        "trust_proxy",
        "scanner",
        "scan_documents",
        "claude_use_host_login",
        "fetch_allow_hosts",
        "import_max_files",
        "sharepoint_tenant",
        "sharepoint_client_id",
    }
    if set(config) - allowed:
        raise ImproperlyConfigured("Unknown configuration fields; secrets belong in mounted files.")
    if config.get("mode") not in {"development", "production"}:
        raise ImproperlyConfigured("mode must be development or production.")
    if type(config.get("scan_documents", False)) is not bool:
        raise ImproperlyConfigured("scan_documents must be a boolean.")
    if type(config.get("claude_use_host_login", False)) is not bool:
        raise ImproperlyConfigured("claude_use_host_login must be a boolean.")
    limit = config.get("import_max_files", 100)
    # Bounded on both sides. A walk that cannot terminate is the thing the limit
    # exists to prevent, and an operator who sets it to something enormous has
    # removed the bound without removing the setting that claims to impose one.
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ImproperlyConfigured("import_max_files must be a whole number between 1 and 1000.")
    for field in ("sharepoint_tenant", "sharepoint_client_id"):
        value = config.get(field, "")
        if not isinstance(value, str):
            raise ImproperlyConfigured(f"{field} must be a string.")
        if value and not re.fullmatch(r"[A-Za-z0-9._-]{1,100}", value):
            raise ImproperlyConfigured(f"{field} contains unexpected characters.")
    hosts = config.get("fetch_allow_hosts", [])
    if not isinstance(hosts, list) or any(not isinstance(h, str) or not h.strip() for h in hosts):
        raise ImproperlyConfigured("fetch_allow_hosts must be a list of hostnames.")
    if any(h.strip() in {"*", "0.0.0.0", "::"} or h.strip().startswith("*") for h in hosts):
        # An allow-list that allows everything is not an allow-list. Naming each
        # internal host is the whole point of the setting.
        raise ImproperlyConfigured("fetch_allow_hosts cannot contain a wildcard.")
    if config.get("claude_use_host_login") and config.get("mode") == "production":
        # Refused outright rather than quietly ignored: a production deployment that
        # believes it is using per-application credentials must not be silently
        # spending one shared identity.
        raise ImproperlyConfigured(
            "claude_use_host_login is a development convenience and cannot be set in "
            "production. Mount a per-application API key or OAuth token instead."
        )
    db = config.get("database", {})
    if set(db) - {"name", "user", "host", "port", "sslrootcert"}:
        raise ImproperlyConfigured("Database configuration cannot contain credentials.")
    if not config.get("hosts") or "*" in config["hosts"]:
        raise ImproperlyConfigured("Explicit allowed hosts are required.")
    return config


def admin_identity():
    """Only the non-secret bootstrap username is accepted in .env."""
    value = os.environ.get("SITE_ADMIN_USER_ID", "")
    path = ROOT / ".env"
    if path.exists():
        seen = False
        for raw in path.read_text(encoding="utf-8-sig").splitlines():
            raw = raw.strip()
            if not raw or raw.startswith("#"):
                continue
            key, separator, entry = raw.partition("=")
            if not separator or key.strip() != "SITE_ADMIN_USER_ID" or seen:
                raise ImproperlyConfigured(".env may contain only SITE_ADMIN_USER_ID once.")
            seen = True
            value = value or entry.strip().strip("\"'")
    return value.strip()


@sensitive_variables()
def read_secret(directory, name):
    path = Path(directory) / name
    try:
        if os.name != "nt" and path.stat().st_mode & 0o077:
            raise ImproperlyConfigured("Secret files must have owner-only permissions (0600).")
        value = path.read_text(encoding="utf-8").strip()
    except OSError:
        raise ImproperlyConfigured(f"Required mounted secret is unavailable: {name}") from None
    if not value:
        raise ImproperlyConfigured(f"Required mounted secret is empty: {name}")
    return value
