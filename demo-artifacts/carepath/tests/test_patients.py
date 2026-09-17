"""Patient registration, retrieval and search. FR-01, FR-02, FR-03, NFR-03."""

import pytest

from carepath.domain.identifiers import valid_mrn
from carepath.errors import Conflict, Invalid, NotFound
from carepath.schemas import PatientIn
from carepath.security.audit import events_for
from carepath.services import patients

from .conftest import CLINICIAN


def a_patient(mrn: str = "RGH-0300003", family_name: str = "Lindqvist") -> PatientIn:
    return PatientIn(
        mrn=mrn,
        given_name="Elsa",
        family_name=family_name,
        birth_date="1971-02-08",
        postal_code="RG7 5PP",
        phone=None,
    )


class TestMedicalRecordNumbers:
    def test_a_well_formed_number_is_returned_unchanged(self):
        assert valid_mrn("RGH-0142857") == "RGH-0142857"

    def test_surrounding_whitespace_is_trimmed(self):
        assert valid_mrn("  RGH-0142857  ") == "RGH-0142857"

    @pytest.mark.parametrize(
        "candidate",
        ["rgh-0142857", "RG-0142857", "RGH-142857", "RGH0142857", "", "RGH-01428571"],
    )
    def test_a_malformed_number_is_refused_rather_than_corrected(self, candidate):
        with pytest.raises(ValueError):
            valid_mrn(candidate)


class TestRegistration:
    def test_a_patient_is_registered_and_readable(self, connection, principal):
        created = patients.register(connection, principal, a_patient())
        assert created["mrn"] == "RGH-0300003"
        assert patients.fetch(connection, created["id"])["family_name"] == "Lindqvist"

    def test_a_duplicate_number_is_a_conflict(self, connection, principal):
        patients.register(connection, principal, a_patient())
        with pytest.raises(Conflict):
            patients.register(connection, principal, a_patient())

    def test_a_malformed_number_is_rejected_at_the_service(self, connection, principal):
        with pytest.raises(Invalid):
            patients.register(connection, principal, a_patient(mrn="XX-1"))

    def test_registering_writes_an_audit_event(self, connection, principal):
        created = patients.register(connection, principal, a_patient())
        events = events_for(connection, "patient", created["id"])
        assert [event["action"] for event in events] == ["create"]
        assert events[0]["actor"] == principal.subject


class TestRetrieval:
    def test_an_unknown_patient_is_not_found(self, connection):
        with pytest.raises(NotFound):
            patients.fetch(connection, "no-such-identifier")

    def test_reading_writes_an_audit_event_carrying_the_purpose(
        self, connection, principal, patient
    ):
        patients.read(connection, principal, patient["id"], purpose="payment")
        events = events_for(connection, "patient", patient["id"])
        assert [event["action"] for event in events] == ["create", "read"]
        assert events[-1]["purpose"] == "payment"

    def test_a_search_records_what_was_looked_for_not_what_was_found(
        self, connection, principal, patient
    ):
        results = patients.search(connection, principal, "Hass", limit=10)
        assert [row["id"] for row in results] == [patient["id"]]
        events = events_for(connection, "patient", "family_name=Hass")
        assert [event["action"] for event in events] == ["search"]

    def test_a_search_matches_on_a_prefix_only(self, connection, principal, patient):
        assert patients.search(connection, principal, "assan", limit=10) == []


class TestApi:
    def test_a_patient_can_be_registered_and_read_back(self, client):
        created = client.post(
            "/patients",
            headers=CLINICIAN,
            json={
                "mrn": "RGH-0400004",
                "given_name": "Yusuf",
                "family_name": "Demir",
                "birth_date": "1988-11-30",
                "postal_code": "RG9 2BB",
            },
        )
        assert created.status_code == 201
        identifier = created.json()["id"]
        read = client.get(f"/patients/{identifier}", headers=CLINICIAN)
        assert read.status_code == 200
        assert read.json()["family_name"] == "Demir"

    def test_an_unknown_patient_is_a_404(self, client):
        response = client.get("/patients/no-such-identifier", headers=CLINICIAN)
        assert response.status_code == 404

    def test_a_duplicate_registration_is_a_409(self, client):
        body = {
            "mrn": "RGH-0500005",
            "given_name": "Ana",
            "family_name": "Costa",
            "birth_date": "1990-04-04",
            "postal_code": "RG1 3CC",
        }
        assert client.post("/patients", headers=CLINICIAN, json=body).status_code == 201
        assert client.post("/patients", headers=CLINICIAN, json=body).status_code == 409

    def test_a_malformed_number_is_a_422(self, client):
        response = client.post(
            "/patients",
            headers=CLINICIAN,
            json={
                "mrn": "nonsense",
                "given_name": "Ana",
                "family_name": "Costa",
                "birth_date": "1990-04-04",
                "postal_code": "RG1 3CC",
            },
        )
        assert response.status_code == 422

    def test_every_response_carries_a_correlation_identifier(self, client):
        response = client.get("/health/live")
        assert response.headers["x-correlation-id"]

    def test_a_supplied_correlation_identifier_is_echoed(self, client):
        response = client.get("/health/live", headers={"x-correlation-id": "abc-123"})
        assert response.headers["x-correlation-id"] == "abc-123"
