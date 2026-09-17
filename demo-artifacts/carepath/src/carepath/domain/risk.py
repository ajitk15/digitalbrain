"""Readmission risk scoring. FR-06.

A transparent, deterministic model rather than a learned one. Care coordinators
have to be able to say why a patient appeared on their list, and a score nobody
can explain is a score nobody acts on.

The weights below are illustrative and carry no clinical validity. See
ADR-0003 for why the scoring stayed in-process for release 1.0.
"""

from dataclasses import dataclass
from datetime import date

#: LOINC codes this model reads, with the range considered unremarkable.
NORMAL_RANGES: dict[str, tuple[float, float]] = {
    "8867-4": (60.0, 100.0),  # Heart rate, beats per minute
    "8480-6": (90.0, 140.0),  # Systolic blood pressure, mm Hg
    "2708-6": (94.0, 100.0),  # Oxygen saturation, percent
    "718-7": (12.0, 17.0),  # Haemoglobin, g/dL
}

WEIGHT_PRIOR_ADMISSION = 0.18
WEIGHT_ABNORMAL_OBSERVATION = 0.09
WEIGHT_AGE_OVER_75 = 0.15
WEIGHT_LONG_STAY = 0.12


@dataclass(frozen=True)
class RiskFactor:
    """One reason the score is what it is, in words a coordinator can repeat."""

    code: str
    description: str
    weight: float


@dataclass(frozen=True)
class RiskAssessment:
    """A score between 0 and 1, and the factors that produced it."""

    score: float
    factors: list[RiskFactor]
    needs_outreach: bool


def age_on(birth_date: date, reference: date) -> int:
    """Whole years between two dates, counting an unreached birthday as short."""
    years = reference.year - birth_date.year
    if (reference.month, reference.day) < (birth_date.month, birth_date.day):
        years -= 1
    return years


def is_abnormal(code: str, value: float) -> bool:
    """True when an observation falls outside the range this model expects.

    An unknown code is not abnormal. A model that treated every code it had
    never seen as a risk signal would score every new instrument as an
    emergency on the day it was installed.
    """
    window = NORMAL_RANGES.get(code)
    if window is None:
        return False
    low, high = window
    return value < low or value > high


def assess(
    birth_date: date,
    reference: date,
    prior_admissions: int,
    abnormal_observations: int,
    longest_stay_days: int,
    threshold: float,
) -> RiskAssessment:
    """Combine the factors into one score, capped at 1.0.

    Capping rather than normalising: a patient with nine prior admissions and a
    fortnight in a bed is not more than certain to come back, and a score above
    one would break every gauge that renders it.
    """
    factors: list[RiskFactor] = []
    if prior_admissions:
        factors.append(
            RiskFactor(
                "prior-admissions",
                f"{prior_admissions} admission(s) already recorded",
                WEIGHT_PRIOR_ADMISSION * prior_admissions,
            )
        )
    if abnormal_observations:
        factors.append(
            RiskFactor(
                "abnormal-observations",
                f"{abnormal_observations} observation(s) outside the expected range",
                WEIGHT_ABNORMAL_OBSERVATION * abnormal_observations,
            )
        )
    if age_on(birth_date, reference) > 75:
        factors.append(RiskFactor("age", "Older than 75", WEIGHT_AGE_OVER_75))
    if longest_stay_days >= 7:
        factors.append(
            RiskFactor("length-of-stay", f"Longest stay {longest_stay_days} days", WEIGHT_LONG_STAY)
        )
    score = min(1.0, round(sum(factor.weight for factor in factors), 4))
    return RiskAssessment(score, factors, score >= threshold)
