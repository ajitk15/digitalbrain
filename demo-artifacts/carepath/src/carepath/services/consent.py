"""Patient consent. FR-09, BR-05.

Consent is append-only. A patient who grants and then withdraws leaves two rows,
and the current position is the most recent row for that purpose. Overwriting
would make "when did they withdraw" unanswerable, which is the question that
matters when a partner asks why an export stopped.
"""

import sqlite3
from datetime import UTC, datetime

from ..domain.identifiers import new_id
from ..schemas import ConsentIn
from ..security.audit import record
from ..security.auth import Principal
from .patients import fetch as fetch_patient


def record_decision(
    connection: sqlite3.Connection,
    principal: Principal,
    patient_id: str,
    payload: ConsentIn,
) -> dict:
    """Append the patient's current decision about one purpose."""
    fetch_patient(connection, patient_id)
    consent_id = new_id()
    connection.execute(
        "INSERT INTO consent (id, patient_id, purpose, granted, recorded_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (
            consent_id,
            patient_id,
            payload.purpose,
            1 if payload.granted else 0,
            datetime.now(UTC).isoformat(),
        ),
    )
    record(connection, principal, "create", "consent", consent_id, "operations")
    row = connection.execute("SELECT * FROM consent WHERE id = ?", (consent_id,)).fetchone()
    return dict(row)


def current(connection: sqlite3.Connection, patient_id: str, purpose: str) -> bool:
    """The patient's latest decision about `purpose`.

    Absent consent is withheld consent. A patient who has never been asked has
    not agreed, and defaulting the other way would make silence a permission.

    Ordered by insertion order and not by timestamp alone. A patient who
    withdraws immediately after granting - a checkbox ticked and then unticked,
    which is a thing people do - produces two rows whose timestamps can be
    identical at the resolution the clock offers, and the answer to "what do
    they currently allow" must not depend on which one the database happens to
    return first.
    """
    row = connection.execute(
        "SELECT granted FROM consent WHERE patient_id = ? AND purpose = ?"
        " ORDER BY recorded_at DESC, rowid DESC LIMIT 1",
        (patient_id, purpose),
    ).fetchone()
    return bool(row["granted"]) if row else False


def history(connection: sqlite3.Connection, patient_id: str, limit: int) -> list[dict]:
    """Every decision this patient has recorded, newest first, bounded."""
    rows = connection.execute(
        "SELECT * FROM consent WHERE patient_id = ? ORDER BY recorded_at DESC, rowid DESC"
        " LIMIT ?",
        (patient_id, limit),
    ).fetchall()
    return [dict(row) for row in rows]
