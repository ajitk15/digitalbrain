"""Synthetic records for a demonstration. Never real patient data.

The cohort is small and deliberately shaped: one patient scores above the
outreach threshold, one sits below it, and one has withdrawn consent to data
exchange. That gives a demonstration something to show at each endpoint without
anyone having to invent a request on the spot.
"""

from datetime import UTC, datetime, timedelta

from .db import transaction
from .schemas import ConsentIn, EncounterIn, ObservationIn, PatientIn, ReferralIn
from .security.auth import TOKENS, Principal
from .services import consent, encounters, patients, referrals

SEED_PRINCIPAL: Principal = next(iter(TOKENS.values()))


def hours_ago(hours: int) -> datetime:
    """A timestamp `hours` before now, in UTC."""
    return datetime.now(UTC) - timedelta(hours=hours)


def run() -> dict[str, str]:
    """Populate an empty database and return the identifiers it created."""
    created: dict[str, str] = {}
    with transaction() as connection:
        already = connection.execute("SELECT COUNT(*) AS total FROM patient").fetchone()
        if already["total"]:
            return created

        high = patients.register(
            connection,
            SEED_PRINCIPAL,
            PatientIn(
                mrn="RGH-0142857",
                given_name="Amara",
                family_name="Okonjo",
                birth_date="1943-03-11",
                postal_code="RG1 4QT",
                phone="+44 118 496 0142",
            ),
        )
        created["high_risk_patient"] = high["id"]
        stay = encounters.open_encounter(
            connection,
            SEED_PRINCIPAL,
            high["id"],
            EncounterIn(
                kind="inpatient",
                facility="Riverside General",
                admitted_at=hours_ago(24 * 21),
                discharged_at=hours_ago(24 * 12),
            ),
        )
        for code, value, unit in (
            ("8867-4", 122.0, "/min"),
            ("2708-6", 88.0, "%"),
            ("718-7", 9.1, "g/dL"),
        ):
            encounters.add_observation(
                connection,
                SEED_PRINCIPAL,
                stay["id"],
                ObservationIn(code=code, value=value, unit=unit, recorded_at=hours_ago(24 * 13)),
            )
        referrals.create(
            connection,
            SEED_PRINCIPAL,
            high["id"],
            ReferralIn(
                specialty="Cardiology",
                reason="Persistent tachycardia after discharge",
                urgency="urgent",
            ),
        )
        consent.record_decision(
            connection,
            SEED_PRINCIPAL,
            high["id"],
            ConsentIn(purpose="data-exchange", granted=True),
        )

        low = patients.register(
            connection,
            SEED_PRINCIPAL,
            PatientIn(
                mrn="RGH-0198221",
                given_name="Tomas",
                family_name="Iversen",
                birth_date="1994-09-02",
                postal_code="RG2 7DE",
                phone=None,
            ),
        )
        created["low_risk_patient"] = low["id"]
        visit = encounters.open_encounter(
            connection,
            SEED_PRINCIPAL,
            low["id"],
            EncounterIn(
                kind="outpatient",
                facility="Northvale Clinic",
                admitted_at=hours_ago(72),
                discharged_at=hours_ago(71),
            ),
        )
        encounters.add_observation(
            connection,
            SEED_PRINCIPAL,
            visit["id"],
            ObservationIn(code="8867-4", value=72.0, unit="/min", recorded_at=hours_ago(71)),
        )

        withheld = patients.register(
            connection,
            SEED_PRINCIPAL,
            PatientIn(
                mrn="RGH-0177310",
                given_name="Priya",
                family_name="Raman",
                birth_date="1968-12-19",
                postal_code="RG4 8LL",
                phone="+44 118 496 0177",
            ),
        )
        created["consent_withheld_patient"] = withheld["id"]
        consent.record_decision(
            connection,
            SEED_PRINCIPAL,
            withheld["id"],
            ConsentIn(purpose="data-exchange", granted=False),
        )
    return created
