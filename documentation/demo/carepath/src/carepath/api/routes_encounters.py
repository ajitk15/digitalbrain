"""Encounter and observation endpoints. FR-04, FR-05."""

from fastapi import APIRouter, Depends, Query

from ..config import settings
from ..schemas import EncounterIn, EncounterOut, ObservationIn, ObservationOut
from ..security.auth import Principal
from ..security.rbac import Permission
from ..services import encounters
from .deps import require, unit_of_work

router = APIRouter(tags=["encounters"])


@router.post("/patients/{patient_id}/encounters", response_model=EncounterOut, status_code=201)
def open_encounter(
    patient_id: str,
    payload: EncounterIn,
    connection=Depends(unit_of_work),
    principal: Principal = Depends(require(Permission.ENCOUNTER_WRITE)),
) -> dict:
    """Record an admission or an attendance. FR-04."""
    return encounters.open_encounter(connection, principal, patient_id, payload)


@router.get("/patients/{patient_id}/encounters", response_model=list[EncounterOut])
def list_encounters(
    patient_id: str,
    limit: int = Query(default=50, le=settings.max_page_size),
    connection=Depends(unit_of_work),
    principal: Principal = Depends(require(Permission.ENCOUNTER_READ)),
) -> list[dict]:
    """Encounters for one patient, newest first. FR-04, NFR-06."""
    return encounters.for_patient(connection, patient_id, limit)


@router.post(
    "/encounters/{encounter_id}/observations", response_model=ObservationOut, status_code=201
)
def add_observation(
    encounter_id: str,
    payload: ObservationIn,
    connection=Depends(unit_of_work),
    principal: Principal = Depends(require(Permission.ENCOUNTER_WRITE)),
) -> dict:
    """Attach one measurement to an encounter. FR-05."""
    return encounters.add_observation(connection, principal, encounter_id, payload)
