"""Liveness, readiness and the audit endpoint. FR-11, FR-12, NFR-01, REG-01."""

from carepath import __version__
from carepath.services import patients

from .conftest import AUDITOR, CLINICIAN


class TestHealth:
    def test_liveness_needs_no_token(self, client):
        response = client.get("/health/live")
        assert response.status_code == 200
        assert response.json()["status"] == "live"

    def test_liveness_reports_the_running_version(self, client):
        assert client.get("/health/live").json()["version"] == __version__

    def test_readiness_consults_the_database(self, client):
        response = client.get("/health/ready")
        assert response.status_code == 200
        assert response.json()["status"] == "ready"


class TestAuditEndpoint:
    def test_an_auditor_reads_the_trail_for_a_patient(self, client, connection, principal):
        from carepath.schemas import PatientIn

        created = patients.register(
            connection,
            principal,
            PatientIn(
                mrn="RGH-0900009",
                given_name="Ida",
                family_name="Novak",
                birth_date="1960-05-05",
                postal_code="RG1 9ZZ",
            ),
        )
        response = client.get(f"/audit/patient/{created['id']}", headers=AUDITOR)
        assert response.status_code == 200
        assert [event["action"] for event in response.json()] == ["create"]

    def test_a_clinician_may_not_read_the_trail(self, client, patient):
        response = client.get(f"/audit/patient/{patient['id']}", headers=CLINICIAN)
        assert response.status_code == 403

    def test_a_resource_with_no_history_returns_an_empty_trail(self, client):
        response = client.get("/audit/patient/nobody", headers=AUDITOR)
        assert response.status_code == 200
        assert response.json() == []
