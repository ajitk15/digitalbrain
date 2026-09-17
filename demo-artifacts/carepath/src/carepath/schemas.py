"""Request and response shapes.

Named `schemas` rather than `models` because in this codebase a model is a thing
in `carepath.domain`; these are the wire contract, and the two are allowed to
diverge. The API specification in `docs/03-build/api-specification.md` is
generated from them.
"""

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field

from .domain.referral_state import ReferralStatus


class PatientIn(BaseModel):
    """A new patient. FR-01."""

    mrn: str = Field(examples=["RGH-0142857"])
    given_name: str = Field(min_length=1, max_length=100)
    family_name: str = Field(min_length=1, max_length=100)
    birth_date: date
    postal_code: str = Field(min_length=2, max_length=12)
    phone: str | None = Field(default=None, max_length=32)


class PatientOut(BaseModel):
    """A patient as returned to a caller entitled to the whole record."""

    id: str
    mrn: str
    given_name: str
    family_name: str
    birth_date: date
    postal_code: str
    phone: str | None = None


class PatientSummary(BaseModel):
    """The minimum necessary view. REG-03.

    Returned to a caller who is entitled to know a patient exists but not to
    read their contact details - an auditor reconciling an access report, for
    instance.
    """

    id: str
    mrn_masked: str
    family_name: str
    birth_year: int


class EncounterIn(BaseModel):
    """An admission or an outpatient attendance. FR-04."""

    kind: Literal["inpatient", "outpatient", "emergency"]
    facility: str = Field(min_length=1, max_length=120)
    admitted_at: datetime
    discharged_at: datetime | None = None


class EncounterOut(BaseModel):
    id: str
    patient_id: str
    kind: str
    facility: str
    admitted_at: datetime
    discharged_at: datetime | None = None


class ObservationIn(BaseModel):
    """One measurement, identified by its LOINC code. FR-05."""

    code: str = Field(examples=["8867-4"], max_length=20)
    value: float
    unit: str = Field(max_length=20)
    recorded_at: datetime


class ObservationOut(BaseModel):
    id: str
    encounter_id: str
    code: str
    value: float
    unit: str
    recorded_at: datetime


class ReferralIn(BaseModel):
    """A request for specialist input. FR-07."""

    specialty: str = Field(min_length=1, max_length=80)
    reason: str = Field(min_length=1, max_length=500)
    urgency: Literal["routine", "urgent", "two-week-wait"] = "routine"


class ReferralOut(BaseModel):
    id: str
    patient_id: str
    specialty: str
    reason: str
    status: ReferralStatus
    urgency: str
    created_by: str
    created_at: datetime
    updated_at: datetime


class ReferralTransition(BaseModel):
    """A requested move along the referral lifecycle. FR-08."""

    status: ReferralStatus


class ConsentIn(BaseModel):
    """A patient's decision about one use of their record. FR-09."""

    purpose: Literal["treatment", "research", "data-exchange"]
    granted: bool


class ConsentOut(BaseModel):
    id: str
    patient_id: str
    purpose: str
    granted: bool
    recorded_at: datetime


class RiskFactorOut(BaseModel):
    code: str
    description: str
    weight: float


class RiskOut(BaseModel):
    """A readmission risk score and its reasons. FR-06."""

    patient_id: str
    score: float
    needs_outreach: bool
    factors: list[RiskFactorOut]
    degraded: bool = False


class AuditEventOut(BaseModel):
    id: str
    actor: str
    actor_role: str
    action: str
    resource_type: str
    resource_id: str
    purpose: str
    occurred_at: datetime
    correlation_id: str


class HealthOut(BaseModel):
    status: str
    version: str
    environment: str
