"""The audit trail. NFR-03, NFR-09, REG-01.

HIPAA 164.312(b) asks for a record of activity in systems holding health
information. The rule this codebase adds on top is *when*: the audit row is
written inside the same transaction as the thing it describes, so there is no
window in which a record was read and the trail does not say so.

Audit rows are never updated and never deleted by application code. Retention is
handled by the archive job described in the operations runbook.
"""

import sqlite3
from datetime import UTC, datetime

from ..domain.identifiers import new_id
from ..logging_setup import correlation_id
from .auth import Principal


def record(
    connection: sqlite3.Connection,
    principal: Principal,
    action: str,
    resource_type: str,
    resource_id: str,
    purpose: str,
) -> str:
    """Write one audit event on `connection` and return its identifier.

    `purpose` is the treatment, payment or operations reason the access was
    made for. It is required rather than optional: an access log that cannot
    say why an record was opened does not answer the question an investigator
    actually asks.
    """
    event_id = new_id()
    connection.execute(
        "INSERT INTO audit_event (id, actor, actor_role, action, resource_type,"
        " resource_id, purpose, occurred_at, correlation_id)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            event_id,
            principal.subject,
            str(principal.role),
            action,
            resource_type,
            resource_id,
            purpose,
            datetime.now(UTC).isoformat(),
            correlation_id.get(),
        ),
    )
    return event_id


def events_for(connection: sqlite3.Connection, resource_type: str, resource_id: str) -> list[dict]:
    """Every recorded access to one resource, oldest first."""
    rows = connection.execute(
        "SELECT * FROM audit_event WHERE resource_type = ? AND resource_id = ?"
        " ORDER BY occurred_at",
        (resource_type, resource_id),
    ).fetchall()
    return [dict(row) for row in rows]
