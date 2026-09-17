"""Referrals between providers. FR-07, FR-08.

The lifecycle map lives in `carepath.domain.referral_state`; this module is
about persistence and the trail.
"""

import sqlite3
from datetime import UTC, datetime

from ..domain.identifiers import new_id
from ..domain.referral_state import ReferralStatus, can_move
from ..errors import Conflict, NotFound
from ..schemas import ReferralIn
from ..security.audit import record
from ..security.auth import Principal
from .patients import fetch as fetch_patient


def create(
    connection: sqlite3.Connection,
    principal: Principal,
    patient_id: str,
    payload: ReferralIn,
) -> dict:
    """Raise a referral in `draft`. Nothing leaves the organisation yet."""
    fetch_patient(connection, patient_id)
    referral_id = new_id()
    now = datetime.now(UTC).isoformat()
    connection.execute(
        "INSERT INTO referral (id, patient_id, specialty, reason, status, urgency,"
        " created_by, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            referral_id,
            patient_id,
            payload.specialty,
            payload.reason,
            str(ReferralStatus.DRAFT),
            payload.urgency,
            principal.subject,
            now,
            now,
        ),
    )
    record(connection, principal, "create", "referral", referral_id, "treatment")
    return fetch(connection, referral_id)


def fetch(connection: sqlite3.Connection, referral_id: str) -> dict:
    row = connection.execute("SELECT * FROM referral WHERE id = ?", (referral_id,)).fetchone()
    if row is None:
        raise NotFound("No such referral.")
    return dict(row)


def for_patient(connection: sqlite3.Connection, patient_id: str, limit: int) -> list[dict]:
    """Referrals raised for one patient, newest first, bounded. NFR-06."""
    rows = connection.execute(
        "SELECT * FROM referral WHERE patient_id = ? ORDER BY created_at DESC LIMIT ?",
        (patient_id, limit),
    ).fetchall()
    return [dict(row) for row in rows]


def transition(
    connection: sqlite3.Connection,
    principal: Principal,
    referral_id: str,
    proposed: ReferralStatus,
) -> dict:
    """Move a referral along its lifecycle, refusing a move the map forbids.

    A refused transition is a conflict rather than a validation error: the
    request is well formed and would have been accepted a moment earlier, and
    the caller wants to know that the state moved underneath them.
    """
    referral = fetch(connection, referral_id)
    current = ReferralStatus(referral["status"])
    if not can_move(current, proposed):
        raise Conflict(f"A referral cannot move from {current} to {proposed}.")
    connection.execute(
        "UPDATE referral SET status = ?, updated_at = ? WHERE id = ?",
        (str(proposed), datetime.now(UTC).isoformat(), referral_id),
    )
    return fetch(connection, referral_id)
