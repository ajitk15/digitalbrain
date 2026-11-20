"""Consent. FR-09, BR-05."""

import pytest

from carepath.errors import NotFound
from carepath.schemas import ConsentIn
from carepath.services import consent

from .conftest import CLINICIAN, COORDINATOR


class TestRecording:
    def test_a_decision_is_recorded_against_the_patient(self, connection, principal, patient):
        recorded = consent.record_decision(
            connection, principal, patient["id"], ConsentIn(purpose="research", granted=True)
        )
        assert recorded["patient_id"] == patient["id"]
        assert recorded["granted"] == 1

    def test_a_decision_for_an_unknown_patient_is_not_found(self, connection, principal):
        with pytest.raises(NotFound):
            consent.record_decision(
                connection, principal, "nobody", ConsentIn(purpose="research", granted=True)
            )


class TestCurrentPosition:
    def test_a_patient_who_was_never_asked_has_not_agreed(self, connection, patient):
        assert consent.current(connection, patient["id"], "data-exchange") is False

    def test_the_latest_decision_wins(self, connection, principal, patient):
        consent.record_decision(
            connection, principal, patient["id"], ConsentIn(purpose="research", granted=True)
        )
        assert consent.current(connection, patient["id"], "research") is True
        consent.record_decision(
            connection, principal, patient["id"], ConsentIn(purpose="research", granted=False)
        )
        assert consent.current(connection, patient["id"], "research") is False

    def test_purposes_are_independent(self, connection, principal, patient):
        consent.record_decision(
            connection, principal, patient["id"], ConsentIn(purpose="research", granted=True)
        )
        assert consent.current(connection, patient["id"], "research") is True
        assert consent.current(connection, patient["id"], "data-exchange") is False

    def test_the_history_keeps_every_decision(self, connection, principal, patient):
        for granted in (True, False, True):
            consent.record_decision(
                connection,
                principal,
                patient["id"],
                ConsentIn(purpose="research", granted=granted),
            )
        assert len(consent.history(connection, patient["id"], 50)) == 3


class TestApi:
    def test_a_coordinator_records_a_decision_and_reads_it_back(self, client, patient):
        created = client.post(
            f"/patients/{patient['id']}/consent",
            headers=COORDINATOR,
            json={"purpose": "data-exchange", "granted": True},
        )
        assert created.status_code == 201
        history = client.get(f"/patients/{patient['id']}/consent", headers=COORDINATOR)
        assert history.status_code == 200
        assert len(history.json()) == 1

    def test_a_clinician_may_read_but_not_record_consent(self, client, patient):
        assert (
            client.get(f"/patients/{patient['id']}/consent", headers=CLINICIAN).status_code == 200
        )
        refused = client.post(
            f"/patients/{patient['id']}/consent",
            headers=CLINICIAN,
            json={"purpose": "research", "granted": True},
        )
        assert refused.status_code == 403

    def test_an_unrecognised_purpose_is_refused(self, client, patient):
        response = client.post(
            f"/patients/{patient['id']}/consent",
            headers=COORDINATOR,
            json={"purpose": "marketing", "granted": True},
        )
        assert response.status_code == 422
