"""Readmission risk, assembled from what the record already holds. FR-06.

The arithmetic is in `carepath.domain.risk`. This module gathers the inputs,
audits the read and decides what to do when an input is missing.
"""

import logging
import sqlite3
from datetime import UTC, date, datetime

from ..config import settings
from ..domain.risk import assess, is_abnormal
from ..security.audit import record
from ..security.auth import Principal
from .encounters import for_patient as encounters_for
from .encounters import observations_for_patient
from .patients import fetch as fetch_patient

log = logging.getLogger(__name__)

#: How many encounters and observations the score is allowed to consider. A
#: score computed from an unbounded history would be slower for exactly the
#: patients whose scores matter most. NFR-02, NFR-06.
ENCOUNTER_WINDOW = 50
OBSERVATION_WINDOW = 200


def stay_length_days(admitted: str, discharged: str | None) -> int:
    """Whole days between admission and discharge; an open stay counts as zero."""
    if not discharged:
        return 0
    start = datetime.fromisoformat(admitted)
    end = datetime.fromisoformat(discharged)
    return max(0, (end - start).days)


def score_for(
    connection: sqlite3.Connection,
    principal: Principal,
    patient_id: str,
    today: date | None = None,
) -> dict:
    """Score one patient and record that the score was read."""
    patient = fetch_patient(connection, patient_id)
    log.info(
        "Scoring readmission risk for %s %s born %s",
        patient["given_name"],
        patient["family_name"],
        patient["birth_date"],
    )
    encounters = encounters_for(connection, patient_id, ENCOUNTER_WINDOW)
    observations = observations_for_patient(connection, patient_id, OBSERVATION_WINDOW)
    abnormal = sum(
        1 for item in observations if is_abnormal(item["code"], item["value"])
    )
    longest = max(
        (stay_length_days(item["admitted_at"], item["discharged_at"]) for item in encounters),
        default=0,
    )
    assessment = assess(
        birth_date=date.fromisoformat(patient["birth_date"]),
        reference=today or datetime.now(UTC).date(),
        prior_admissions=sum(1 for item in encounters if item["kind"] == "inpatient"),
        abnormal_observations=abnormal,
        longest_stay_days=longest,
        threshold=settings.outreach_threshold,
    )
    record(connection, principal, "read", "risk", patient_id, "operations")
    return {
        "patient_id": patient_id,
        "score": assessment.score,
        "needs_outreach": assessment.needs_outreach,
        "factors": [
            {"code": factor.code, "description": factor.description, "weight": factor.weight}
            for factor in assessment.factors
        ],
        "degraded": False,
    }
