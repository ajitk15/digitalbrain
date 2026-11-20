"""Patient registration and retrieval. FR-01, FR-02, FR-03.

Every function takes the principal making the request. That is not decoration:
the audit row cannot be written without it, and a service that could be called
without one would be a service that could read a record anonymously.
"""

import sqlite3
from datetime import UTC, datetime

from ..domain.identifiers import new_id, valid_mrn
from ..errors import Conflict, Invalid, NotFound
from ..schemas import PatientIn
from ..security.audit import record
from ..security.auth import Principal


def register(connection: sqlite3.Connection, principal: Principal, payload: PatientIn) -> dict:
    """Create a patient, refusing a medical record number already in use.

    The uniqueness check and the insert are both inside the caller's
    transaction, and the column carries a UNIQUE constraint as well. The check
    gives a useful error; the constraint is what actually holds when two
    registrations race.
    """
    try:
        mrn = valid_mrn(payload.mrn)
    except ValueError as error:
        raise Invalid(str(error)) from error
    existing = connection.execute("SELECT id FROM patient WHERE mrn = ?", (mrn,)).fetchone()
    if existing:
        raise Conflict("That medical record number is already registered.")
    patient_id = new_id()
    connection.execute(
        "INSERT INTO patient (id, mrn, given_name, family_name, birth_date, postal_code,"
        " phone, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            patient_id,
            mrn,
            payload.given_name,
            payload.family_name,
            payload.birth_date.isoformat(),
            payload.postal_code,
            payload.phone,
            datetime.now(UTC).isoformat(),
        ),
    )
    record(connection, principal, "create", "patient", patient_id, "treatment")
    return fetch(connection, patient_id)


def fetch(connection: sqlite3.Connection, patient_id: str) -> dict:
    """Read one patient row without auditing. Internal callers only."""
    row = connection.execute("SELECT * FROM patient WHERE id = ?", (patient_id,)).fetchone()
    if row is None:
        raise NotFound("No such patient.")
    return dict(row)


def read(
    connection: sqlite3.Connection,
    principal: Principal,
    patient_id: str,
    purpose: str = "treatment",
) -> dict:
    """Read one patient and record that it was read. FR-02, NFR-03."""
    patient = fetch(connection, patient_id)
    record(connection, principal, "read", "patient", patient_id, purpose)
    return patient


def search(
    connection: sqlite3.Connection,
    principal: Principal,
    family_name: str,
    limit: int,
) -> list[dict]:
    """Find patients by family name. FR-03.

    One audit row for the search itself rather than one per result: the
    question an investigator asks is what the clinician looked for, and a
    hundred rows saying the same thing at the same millisecond obscure it.
    """
    rows = connection.execute(
        "SELECT * FROM patient WHERE family_name LIKE ? ORDER BY family_name LIMIT ?",
        (f"{family_name}%", limit),
    ).fetchall()
    record(connection, principal, "search", "patient", f"family_name={family_name}", "treatment")
    return [dict(row) for row in rows]
