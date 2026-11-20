"""Authentication, authorisation and redaction. NFR-04, NFR-05, REG-02."""

from carepath.security.auth import Principal, principal_for
from carepath.security.phi import mask_mrn, redact
from carepath.security.rbac import GRANTS, Permission, Role, allows

from .conftest import AUDITOR, CLINICIAN, PARTNER


class TestAuthentication:
    def test_a_request_without_a_token_is_refused(self, client):
        response = client.get("/patients/anything")
        assert response.status_code == 401
        assert response.headers["www-authenticate"] == "Bearer"

    def test_a_token_that_is_not_issued_is_refused(self, client):
        response = client.get(
            "/patients/anything", headers={"Authorization": "Bearer not-a-real-token"}
        )
        assert response.status_code == 401

    def test_a_scheme_other_than_bearer_is_refused(self, client):
        response = client.get("/patients/anything", headers={"Authorization": "Basic abc123"})
        assert response.status_code == 401

    def test_a_known_token_resolves_to_a_principal(self):
        principal = principal_for("demo-clinician-token")
        assert isinstance(principal, Principal)
        assert principal.role is Role.CLINICIAN

    def test_an_unknown_token_resolves_to_nothing(self):
        assert principal_for("demo-clinician-tokeN") is None
        assert principal_for("") is None


class TestAuthorisation:
    def test_a_partner_may_not_read_a_patient_directly(self, client, patient):
        response = client.get(f"/patients/{patient['id']}", headers=PARTNER)
        assert response.status_code == 403

    def test_an_auditor_may_not_register_a_patient(self, client):
        response = client.post(
            "/patients",
            headers=AUDITOR,
            json={
                "mrn": "RGH-0200002",
                "given_name": "Test",
                "family_name": "Person",
                "birth_date": "1980-01-01",
                "postal_code": "RG1 1AA",
            },
        )
        assert response.status_code == 403

    def test_a_clinician_may_read_a_patient(self, client, patient):
        response = client.get(f"/patients/{patient['id']}", headers=CLINICIAN)
        assert response.status_code == 200

    def test_an_unknown_role_holds_no_permissions(self):
        assert not allows("registrar", Permission.PATIENT_READ)

    def test_every_role_in_the_table_is_a_declared_role(self):
        assert set(GRANTS) == set(Role)


class TestRedaction:
    def test_a_medical_record_number_is_removed(self):
        assert redact("looked up RGH-0142857 today") == "looked up [mrn] today"

    def test_a_telephone_number_is_removed(self):
        assert "[phone]" in redact("call +44 118 496 0142 back")

    def test_an_email_address_is_removed(self):
        assert redact("mail a.okonjo@example.com") == "mail [email]"

    def test_text_with_no_identifier_is_unchanged(self):
        assert redact("scoring complete") == "scoring complete"

    def test_a_masked_number_keeps_the_facility_and_the_last_two_digits(self):
        assert mask_mrn("RGH-0142857") == "RGH-****57"
