"""FHIR R4 export for partner systems. FR-10, BR-06, REG-07."""

from fastapi import APIRouter, Depends, Query

from ..config import settings
from ..errors import ConsentWithheld
from ..fhir import bundle_for
from ..security.audit import record
from ..security.auth import Principal
from ..security.rbac import Permission
from ..services import consent
from ..services.patients import fetch as fetch_patient
from .deps import require, unit_of_work

router = APIRouter(prefix="/fhir", tags=["fhir"])


@router.get("/Patient/{patient_id}/$everything")
def export_everything(
    patient_id: str,
    limit: int = Query(default=50, le=settings.max_page_size),
    connection=Depends(unit_of_work),
    principal: Principal = Depends(require(Permission.FHIR_EXPORT)),
) -> dict:
    """One patient's record as a FHIR R4 Bundle. FR-10.

    Export is refused, not merely filtered, when the patient has not
    consented to this use of their record: withdrawn and never-asked both
    resolve to withheld (BR-05), and `consent.current` already defaults the
    latter to False. The patient's existence is checked first so an unknown
    patient still comes back as not-found rather than as a consent refusal.

    The refusal is itself a disclosure decision and is written to the audit
    trail before `ConsentWithheld` is raised. The connection is committed at
    that point because the surrounding unit of work rolls the transaction
    back on any exception, and a refusal that gets rolled back with it is a
    refusal the audit trail never actually saw.
    """
    fetch_patient(connection, patient_id)
    if not consent.current(connection, patient_id, "data-exchange"):
        record(
            connection,
            principal,
            "disclosure-refused",
            "patient",
            patient_id,
            "data-exchange",
        )
        connection.commit()
        raise ConsentWithheld("The patient has not consented to this use of their record.")
    bundle = bundle_for(connection, patient_id, limit)
    record(connection, principal, "export", "patient", patient_id, "data-exchange")
    return bundle
