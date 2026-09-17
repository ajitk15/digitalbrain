"""Runtime configuration.

NFR-07 requires that credentials are not compiled into the image. Everything
here is read once at import and is overridable for tests, which is what lets the
suite run against an in-memory database without a temporary file.
"""

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    """Immutable settings for one process."""

    database_url: str = os.environ.get("CAREPATH_DATABASE", "carepath.sqlite3")
    environment: str = os.environ.get("CAREPATH_ENVIRONMENT", "development")
    #: Maximum rows any list endpoint may return. NFR-06.
    max_page_size: int = int(os.environ.get("CAREPATH_MAX_PAGE_SIZE", "100"))
    #: Readmission risk above which a care coordinator is asked to make contact.
    outreach_threshold: float = float(os.environ.get("CAREPATH_OUTREACH_THRESHOLD", "0.65"))
    #: Years an audit record is retained before it may be archived. NFR-09.
    audit_retention_years: int = 6

    @property
    def is_production(self) -> bool:
        return self.environment == "production"


settings = Settings()
