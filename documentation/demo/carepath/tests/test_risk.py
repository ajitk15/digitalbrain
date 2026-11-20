"""Readmission risk. FR-06."""

from datetime import UTC, date, datetime, timedelta

import pytest

from carepath.domain.risk import age_on, assess, is_abnormal
from carepath.schemas import EncounterIn, ObservationIn
from carepath.services import encounters, risk

from .conftest import AUDITOR, CLINICIAN

TODAY = date(2026, 4, 1)
NOW = datetime(2026, 4, 1, 9, 0, tzinfo=UTC)


class TestAge:
    def test_a_birthday_already_passed_counts_in_full(self):
        assert age_on(date(1950, 1, 1), TODAY) == 76

    def test_a_birthday_still_to_come_counts_short(self):
        assert age_on(date(1950, 12, 31), TODAY) == 75

    def test_the_birthday_itself_counts_in_full(self):
        assert age_on(date(1950, 4, 1), TODAY) == 76


class TestObservationRanges:
    @pytest.mark.parametrize(("code", "value"), [("8867-4", 122.0), ("2708-6", 88.0)])
    def test_a_value_outside_the_range_is_abnormal(self, code, value):
        assert is_abnormal(code, value)

    @pytest.mark.parametrize(("code", "value"), [("8867-4", 72.0), ("2708-6", 98.0)])
    def test_a_value_inside_the_range_is_not(self, code, value):
        assert not is_abnormal(code, value)

    def test_an_unknown_code_is_never_abnormal(self):
        assert not is_abnormal("99999-9", 1_000_000.0)


class TestScoring:
    def test_a_patient_with_nothing_recorded_scores_zero(self):
        assessment = assess(date(1990, 1, 1), TODAY, 0, 0, 0, threshold=0.65)
        assert assessment.score == 0.0
        assert assessment.factors == []
        assert assessment.needs_outreach is False

    def test_every_contributing_factor_is_named(self):
        assessment = assess(date(1940, 1, 1), TODAY, 2, 3, 9, threshold=0.65)
        codes = {factor.code for factor in assessment.factors}
        assert codes == {"prior-admissions", "abnormal-observations", "age", "length-of-stay"}

    def test_the_score_never_exceeds_one(self):
        assessment = assess(date(1930, 1, 1), TODAY, 20, 20, 30, threshold=0.65)
        assert assessment.score == 1.0

    def test_outreach_is_flagged_at_the_threshold(self):
        assessment = assess(date(1990, 1, 1), TODAY, 1, 0, 0, threshold=0.18)
        assert assessment.needs_outreach is True

    def test_a_short_stay_does_not_count_as_a_long_one(self):
        assessment = assess(date(1990, 1, 1), TODAY, 0, 0, 6, threshold=0.65)
        assert assessment.factors == []


class TestStayLength:
    def test_an_open_stay_counts_as_zero(self):
        assert risk.stay_length_days(NOW.isoformat(), None) == 0

    def test_whole_days_are_counted(self):
        end = (NOW + timedelta(days=9, hours=3)).isoformat()
        assert risk.stay_length_days(NOW.isoformat(), end) == 9


class TestService:
    def test_a_patient_with_no_history_scores_zero(self, connection, principal, patient):
        result = risk.score_for(connection, principal, patient["id"], today=TODAY)
        assert result["score"] == 0.0
        assert result["degraded"] is False

    def test_history_raises_the_score_and_explains_why(self, connection, principal, patient):
        stay = encounters.open_encounter(
            connection,
            principal,
            patient["id"],
            EncounterIn(
                kind="inpatient",
                facility="Riverside General",
                admitted_at=NOW - timedelta(days=10),
                discharged_at=NOW,
            ),
        )
        encounters.add_observation(
            connection,
            principal,
            stay["id"],
            ObservationIn(code="8867-4", value=130.0, unit="/min", recorded_at=NOW),
        )
        result = risk.score_for(connection, principal, patient["id"], today=TODAY)
        assert result["score"] > 0
        assert {factor["code"] for factor in result["factors"]} == {
            "prior-admissions",
            "abnormal-observations",
            "length-of-stay",
        }


class TestApi:
    def test_a_clinician_reads_a_score(self, client, patient):
        response = client.get(f"/patients/{patient['id']}/risk", headers=CLINICIAN)
        assert response.status_code == 200
        assert response.json()["patient_id"] == patient["id"]

    def test_an_auditor_may_not_read_a_score(self, client, patient):
        assert (
            client.get(f"/patients/{patient['id']}/risk", headers=AUDITOR).status_code == 403
        )

    def test_a_score_for_an_unknown_patient_is_a_404(self, client):
        assert client.get("/patients/nobody/risk", headers=CLINICIAN).status_code == 404
