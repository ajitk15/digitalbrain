"""Encounters and the observations recorded during them. FR-04, FR-05."""

import sqlite3

from ..domain.identifiers import new_id
from ..errors import Invalid, NotFound
from ..schemas import EncounterIn, ObservationIn
from ..security.audit import record
from ..security.auth import Principal
from .patients import fetch as fetch_patient


def open_encounter(
    connection: sqlite3.Connection,
    principal: Principal,
    patient_id: str,
    payload: EncounterIn,
) -> dict:
    """Record an admission or an attendance against an existing patient."""
    fetch_patient(connection, patient_id)
    if payload.discharged_at and payload.discharged_at < payload.admitted_at:
        raise Invalid("An encounter cannot end before it starts.")
    encounter_id = new_id()
    connection.execute(
        "INSERT INTO encounter (id, patient_id, kind, admitted_at, discharged_at, facility)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (
            encounter_id,
            patient_id,
            payload.kind,
            payload.admitted_at.isoformat(),
            payload.discharged_at.isoformat() if payload.discharged_at else None,
            payload.facility,
        ),
    )
    record(connection, principal, "create", "encounter", encounter_id, "treatment")
    return fetch_encounter(connection, encounter_id)


def fetch_encounter(connection: sqlite3.Connection, encounter_id: str) -> dict:
    row = connection.execute(
        "SELECT * FROM encounter WHERE id = ?", (encounter_id,)
    ).fetchone()
    if row is None:
        raise NotFound("No such encounter.")
    return dict(row)


def for_patient(connection: sqlite3.Connection, patient_id: str, limit: int) -> list[dict]:
    """Every encounter for one patient, most recent first, bounded. NFR-06."""
    rows = connection.execute(
        "SELECT * FROM encounter WHERE patient_id = ? ORDER BY admitted_at DESC LIMIT ?",
        (patient_id, limit),
    ).fetchall()
    return [dict(row) for row in rows]


def add_observation(
    connection: sqlite3.Connection,
    principal: Principal,
    encounter_id: str,
    payload: ObservationIn,
) -> dict:
    """Attach one measurement to an encounter. FR-05."""
    fetch_encounter(connection, encounter_id)
    observation_id = new_id()
    connection.execute(
        "INSERT INTO observation (id, encounter_id, code, value, unit, recorded_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (
            observation_id,
            encounter_id,
            payload.code,
            payload.value,
            payload.unit,
            payload.recorded_at.isoformat(),
        ),
    )
    record(connection, principal, "create", "observation", observation_id, "treatment")
    row = connection.execute(
        "SELECT * FROM observation WHERE id = ?", (observation_id,)
    ).fetchone()
    return dict(row)


def observations_for_patient(
    connection: sqlite3.Connection, patient_id: str, limit: int
) -> list[dict]:
    """Observations across every encounter for one patient, bounded. NFR-06."""
    rows = connection.execute(
        "SELECT observation.* FROM observation"
        " JOIN encounter ON encounter.id = observation.encounter_id"
        " WHERE encounter.patient_id = ?"
        " ORDER BY observation.recorded_at DESC LIMIT ?",
        (patient_id, limit),
    ).fetchall()
    return [dict(row) for row in rows]
