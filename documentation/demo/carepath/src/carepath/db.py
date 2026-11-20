"""SQLite access and schema.

A repository of plain SQL rather than an object-relational mapper, so that the
statement a reviewer reads is the statement the database runs. Every table that
holds patient data has an `audit_event` row written in the same transaction as
the read or the write - see `carepath.security.audit` for why that matters
(NFR-03, REG-01).
"""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager

from .config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS patient (
    id            TEXT PRIMARY KEY,
    mrn           TEXT NOT NULL UNIQUE,
    given_name    TEXT NOT NULL,
    family_name   TEXT NOT NULL,
    birth_date    TEXT NOT NULL,
    postal_code   TEXT NOT NULL,
    phone         TEXT,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS encounter (
    id            TEXT PRIMARY KEY,
    patient_id    TEXT NOT NULL REFERENCES patient(id),
    kind          TEXT NOT NULL,
    admitted_at   TEXT NOT NULL,
    discharged_at TEXT,
    facility      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS observation (
    id            TEXT PRIMARY KEY,
    encounter_id  TEXT NOT NULL REFERENCES encounter(id),
    code          TEXT NOT NULL,
    value         REAL NOT NULL,
    unit          TEXT NOT NULL,
    recorded_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS referral (
    id            TEXT PRIMARY KEY,
    patient_id    TEXT NOT NULL REFERENCES patient(id),
    specialty     TEXT NOT NULL,
    reason        TEXT NOT NULL,
    status        TEXT NOT NULL,
    urgency       TEXT NOT NULL,
    created_by    TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS consent (
    id            TEXT PRIMARY KEY,
    patient_id    TEXT NOT NULL REFERENCES patient(id),
    purpose       TEXT NOT NULL,
    granted       INTEGER NOT NULL,
    recorded_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_event (
    id            TEXT PRIMARY KEY,
    actor         TEXT NOT NULL,
    actor_role    TEXT NOT NULL,
    action        TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id   TEXT NOT NULL,
    purpose       TEXT NOT NULL,
    occurred_at   TEXT NOT NULL,
    correlation_id TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_encounter_patient ON encounter(patient_id);
CREATE INDEX IF NOT EXISTS idx_observation_encounter ON observation(encounter_id);
CREATE INDEX IF NOT EXISTS idx_referral_patient ON referral(patient_id);
CREATE INDEX IF NOT EXISTS idx_consent_patient ON consent(patient_id);
CREATE INDEX IF NOT EXISTS idx_audit_resource ON audit_event(resource_type, resource_id);
"""

_connection: sqlite3.Connection | None = None


def connect(database: str | None = None) -> sqlite3.Connection:
    """Open the process-wide connection and create the schema if it is absent."""
    global _connection
    if _connection is not None:
        return _connection
    target = database or settings.database_url
    _connection = sqlite3.connect(target, check_same_thread=False)
    _connection.row_factory = sqlite3.Row
    _connection.execute("PRAGMA foreign_keys = ON")
    _connection.executescript(SCHEMA)
    return _connection


def reset(database: str | None = None) -> sqlite3.Connection:
    """Drop the process-wide connection and open a fresh one. Tests only."""
    global _connection
    if _connection is not None:
        _connection.close()
        _connection = None
    return connect(database)


@contextmanager
def transaction() -> Iterator[sqlite3.Connection]:
    """One unit of work.

    The audit write and the thing it describes share this transaction, so a
    rolled-back read leaves no audit record claiming it happened, and a
    successful read cannot leave the trail behind.
    """
    connection = connect()
    try:
        yield connection
    except Exception:
        connection.rollback()
        raise
    connection.commit()
