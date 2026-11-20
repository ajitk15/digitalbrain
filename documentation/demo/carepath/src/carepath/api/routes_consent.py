"""Consent endpoints. FR-09, BR-05."""

from fastapi import APIRouter, Depends, Query

from ..config import settings
from ..schemas import ConsentIn, ConsentOut
from ..security.auth import Principal
from ..security.rbac import Permission
from ..services import consent
from .deps import require, unit_of_work

router = APIRouter(prefix="/patients/{patient_id}/consent", tags=["consent"])


@router.post("", response_model=ConsentOut, status_code=201)
def record_consent(
    patient_id: str,
    payload: ConsentIn,
    connection=Depends(unit_of_work),
    principal: Principal = Depends(require(Permission.CONSENT_WRITE)),
) -> dict:
    """Append the patient's decision about one purpose. FR-09."""
    return consent.record_decision(connection, principal, patient_id, payload)


@router.get("", response_model=list[ConsentOut])
def consent_history(
    patient_id: str,
    limit: int = Query(default=50, le=settings.max_page_size),
    connection=Depends(unit_of_work),
    principal: Principal = Depends(require(Permission.CONSENT_READ)),
) -> list[dict]:
    """Every decision this patient has recorded. FR-09, NFR-06."""
    return consent.history(connection, patient_id, limit)
