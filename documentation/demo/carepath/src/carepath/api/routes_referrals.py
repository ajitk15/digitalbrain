"""Referral endpoints. FR-07, FR-08."""

from fastapi import APIRouter, Depends, Query

from ..config import settings
from ..schemas import ReferralIn, ReferralOut, ReferralTransition
from ..security.auth import Principal
from ..security.rbac import Permission
from ..services import referrals
from .deps import require, unit_of_work

router = APIRouter(tags=["referrals"])


@router.post("/patients/{patient_id}/referrals", response_model=ReferralOut, status_code=201)
def create_referral(
    patient_id: str,
    payload: ReferralIn,
    connection=Depends(unit_of_work),
    principal: Principal = Depends(require(Permission.REFERRAL_WRITE)),
) -> dict:
    """Raise a referral in draft. FR-07."""
    return referrals.create(connection, principal, patient_id, payload)


@router.get("/patients/{patient_id}/referrals", response_model=list[ReferralOut])
def list_referrals(
    patient_id: str,
    limit: int = Query(default=50, le=settings.max_page_size),
    connection=Depends(unit_of_work),
    principal: Principal = Depends(require(Permission.REFERRAL_READ)),
) -> list[dict]:
    """Referrals raised for one patient. FR-07, NFR-06."""
    return referrals.for_patient(connection, patient_id, limit)


@router.post("/referrals/{referral_id}/status", response_model=ReferralOut)
def move_referral(
    referral_id: str,
    payload: ReferralTransition,
    connection=Depends(unit_of_work),
    principal: Principal = Depends(require(Permission.REFERRAL_WRITE)),
) -> dict:
    """Move a referral along its lifecycle. FR-08."""
    return referrals.transition(connection, principal, referral_id, payload.status)
