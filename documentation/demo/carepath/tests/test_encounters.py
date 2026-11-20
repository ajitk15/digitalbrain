"""Encounters and observations. FR-04, FR-05, NFR-06."""

from datetime import UTC, datetime, timedelta

import pytest

from carepath.errors import Invalid, NotFound
from carepath.schemas import EncounterIn, ObservationIn
from carepath.security.audit import events_for
from carepath.services import encounters

from .conftest import CLINICIAN

NOW = datetime(2026, 4, 1, 9, 0, tzinfo=UTC)


def a_stay(hours: int = 48) -> EncounterIn:
    return EncounterIn(
        kind="inpatient",
        facility="Riverside General",
        admitted_at=NOW,
        discharged_at=NOW + timedelta(hours=hours),
    )


class TestOpeningAnEncounter:
    def test_an_encounter_is_recorded_against_the_patient(self, connection, principal, patient):
        created = encounters.open_encounter(connection, principal, patient["id"], a_stay())
        assert created["patient_id"] == patient["id"]
        assert created["facility"] == "Riverside General"

    def test_an_encounter_for_an_unknown_patient_is_not_found(self, connection, principal):
        with pytest.raises(NotFound):
            encounters.open_encounter(connection, principal, "no-such-patient", a_stay())

    def test_a_discharge_before_the_admission_is_refused(self, connection, principal, patient):
        backwards = EncounterIn(
            kind="inpatient",
            facility="Riverside General",
            admitted_at=NOW,
            discharged_at=NOW - timedelta(hours=1),
        )
        with pytest.raises(Invalid):
            encounters.open_encounter(connection, principal, patient["id"], backwards)

    def test_an_open_stay_is_allowed(self, connection, principal, patient):
        open_stay = EncounterIn(
            kind="emergency", facility="Riverside General", admitted_at=NOW, discharged_at=None
        )
        created = encounters.open_encounter(connection, principal, patient["id"], open_stay)
        assert created["discharged_at"] is None

    def test_opening_an_encounter_writes_an_audit_event(self, connection, principal, patient):
        created = encounters.open_encounter(connection, principal, patient["id"], a_stay())
        assert [e["action"] for e in events_for(connection, "encounter", created["id"])] == [
            "create"
        ]


class TestObservations:
    def test_an_observation_is_attached_to_the_encounter(self, connection, principal, patient):
        stay = encounters.open_encounter(connection, principal, patient["id"], a_stay())
        observation = encounters.add_observation(
            connection,
            principal,
            stay["id"],
            ObservationIn(code="8867-4", value=96.0, unit="/min", recorded_at=NOW),
        )
        assert observation["encounter_id"] == stay["id"]
        assert observation["value"] == 96.0

    def test_an_observation_for_an_unknown_encounter_is_not_found(self, connection, principal):
        with pytest.raises(NotFound):
            encounters.add_observation(
                connection,
                principal,
                "no-such-encounter",
                ObservationIn(code="8867-4", value=96.0, unit="/min", recorded_at=NOW),
            )

    def test_observations_are_gathered_across_every_encounter(
        self, connection, principal, patient
    ):
        first = encounters.open_encounter(connection, principal, patient["id"], a_stay())
        second = encounters.open_encounter(connection, principal, patient["id"], a_stay(hours=4))
        for encounter_id in (first["id"], second["id"]):
            encounters.add_observation(
                connection,
                principal,
                encounter_id,
                ObservationIn(code="8867-4", value=80.0, unit="/min", recorded_at=NOW),
            )
        assert len(encounters.observations_for_patient(connection, patient["id"], 50)) == 2


class TestBounds:
    def test_a_listing_never_returns_more_than_the_limit(self, connection, principal, patient):
        for _ in range(5):
            encounters.open_encounter(connection, principal, patient["id"], a_stay())
        assert len(encounters.for_patient(connection, patient["id"], 3)) == 3

    def test_the_api_refuses_a_limit_above_the_maximum_page_size(self, client, patient):
        response = client.get(
            f"/patients/{patient['id']}/encounters?limit=5000", headers=CLINICIAN
        )
        assert response.status_code == 422
