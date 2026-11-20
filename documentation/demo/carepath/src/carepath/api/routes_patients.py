"""Patient endpoints. FR-01, FR-02, FR-03, FR-06."""

from fastapi import APIRouter, Depends, Query

from ..schemas import PatientIn, PatientOut, RiskOut
from ..security.auth import Principal
from ..security.rbac import Permission
from ..services import patients, risk
from .deps import require, unit_of_work

router = APIRouter(prefix="/patients", tags=["patients"])


@router.post("", response_model=PatientOut, status_code=201)
def create_patient(
    payload: PatientIn,
    connection=Depends(unit_of_work),
    principal: Principal = Depends(require(Permission.PATIENT_WRITE)),
) -> dict:
    """Register a patient. FR-01."""
    return patients.register(connection, principal, payload)


@router.get("", response_model=list[PatientOut])
def search_patients(
    family_name: str = Query(min_length=1),
    limit: int = Query(default=50),
    connection=Depends(unit_of_work),
    principal: Principal = Depends(require(Permission.PATIENT_READ)),
) -> list[dict]:
    """Find patients by family name. FR-03."""
    return patients.search(connection, principal, family_name, limit)


@router.get("/{patient_id}", response_model=PatientOut)
def read_patient(
    patient_id: str,
    connection=Depends(unit_of_work),
    principal: Principal = Depends(require(Permission.PATIENT_READ)),
) -> dict:
    """Read one patient. FR-02."""
    return patients.read(connection, principal, patient_id)


@router.get("/{patient_id}/risk", response_model=RiskOut)
def read_risk(
    patient_id: str,
    connection=Depends(unit_of_work),
    principal: Principal = Depends(require(Permission.RISK_READ)),
) -> dict:
    """Readmission risk and the factors behind it. FR-06."""
    return risk.score_for(connection, principal, patient_id)
