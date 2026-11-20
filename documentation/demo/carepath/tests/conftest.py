"""Shared fixtures.

The database environment variable is set before anything from `carepath` is
imported, because `carepath.config` reads the environment once at import. A
fixture that reset it afterwards would be resetting a value the settings object
had already copied.
"""

import os

os.environ.setdefault("CAREPATH_DATABASE", ":memory:")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from carepath import db  # noqa: E402
from carepath.app import create_app  # noqa: E402
from carepath.schemas import PatientIn  # noqa: E402
from carepath.security.auth import TOKENS  # noqa: E402
from carepath.services import patients  # noqa: E402

CLINICIAN = {"Authorization": "Bearer demo-clinician-token"}
COORDINATOR = {"Authorization": "Bearer demo-coordinator-token"}
AUDITOR = {"Authorization": "Bearer demo-auditor-token"}
PARTNER = {"Authorization": "Bearer demo-partner-token"}


@pytest.fixture
def connection():
    """A fresh in-memory database with the schema applied."""
    return db.reset(":memory:")


@pytest.fixture
def principal():
    """The clinician principal, for calling services without a web request."""
    from carepath.security.auth import digest

    return TOKENS[digest("demo-clinician-token")]


@pytest.fixture
def client(connection):
    """A test client over an application bound to the fresh database."""
    return TestClient(create_app())


@pytest.fixture
def patient(connection, principal):
    """One registered patient, for tests that need a subject and not a story."""
    return patients.register(
        connection,
        principal,
        PatientIn(
            mrn="RGH-0100001",
            given_name="Nadia",
            family_name="Hassan",
            birth_date="1955-06-14",
            postal_code="RG1 1AA",
            phone="+44 118 496 0001",
        ),
    )
