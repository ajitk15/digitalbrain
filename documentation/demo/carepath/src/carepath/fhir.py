"""FHIR R4 export for partner systems. FR-10, BR-06, REG-07.

A Bundle of the resources a receiving system needs to open a record: one
Patient, the Encounters, and the Observations recorded during them. The shapes
follow FHIR R4; the profile claims are deliberately modest, because asserting US
Core conformance means passing the US Core validator and release 1.0 does not
run it. See `docs/02-design/adr/0004-fhir-profile-scope.md`.
"""

import sqlite3

from .services.encounters import for_patient as encounters_for
from .services.encounters import observations_for_patient
from .services.patients import fetch as fetch_patient

FHIR_VERSION = "4.0.1"
SYSTEM_MRN = "urn:oid:2.16.840.1.113883.19.5"
SYSTEM_LOINC = "http://loinc.org"

ENCOUNTER_CLASS = {
    "inpatient": "IMP",
    "outpatient": "AMB",
    "emergency": "EMER",
}


def patient_resource(patient: dict) -> dict:
    """One FHIR Patient resource."""
    return {
        "resourceType": "Patient",
        "id": patient["id"],
        "identifier": [{"system": SYSTEM_MRN, "value": patient["mrn"]}],
        "name": [
            {
                "use": "official",
                "family": patient["family_name"],
                "given": [patient["given_name"]],
            }
        ],
        "birthDate": patient["birth_date"],
        "telecom": (
            [{"system": "phone", "value": patient["phone"], "use": "home"}]
            if patient["phone"]
            else []
        ),
        "address": [{"postalCode": patient["postal_code"]}],
    }


def encounter_resource(encounter: dict) -> dict:
    """One FHIR Encounter resource."""
    period = {"start": encounter["admitted_at"]}
    if encounter["discharged_at"]:
        period["end"] = encounter["discharged_at"]
    return {
        "resourceType": "Encounter",
        "id": encounter["id"],
        "status": "finished" if encounter["discharged_at"] else "in-progress",
        "class": {
            "system": "http://terminology.hl7.org/CodeSystem/v3-ActCode",
            "code": ENCOUNTER_CLASS.get(encounter["kind"], "AMB"),
        },
        "subject": {"reference": f"Patient/{encounter['patient_id']}"},
        "period": period,
    }


def observation_resource(observation: dict) -> dict:
    """One FHIR Observation resource."""
    return {
        "resourceType": "Observation",
        "id": observation["id"],
        "status": "final",
        "code": {"coding": [{"system": SYSTEM_LOINC, "code": observation["code"]}]},
        "encounter": {"reference": f"Encounter/{observation['encounter_id']}"},
        "effectiveDateTime": observation["recorded_at"],
        "valueQuantity": {
            "value": observation["value"],
            "unit": observation["unit"],
            "system": "http://unitsofmeasure.org",
        },
    }


def bundle_for(connection: sqlite3.Connection, patient_id: str, limit: int) -> dict:
    """A searchset Bundle carrying one patient's exportable record."""
    patient = fetch_patient(connection, patient_id)
    entries = [{"resource": patient_resource(patient)}]
    for encounter in encounters_for(connection, patient_id, limit):
        entries.append({"resource": encounter_resource(encounter)})
    for observation in observations_for_patient(connection, patient_id, limit):
        entries.append({"resource": observation_resource(observation)})
    return {
        "resourceType": "Bundle",
        "type": "searchset",
        "meta": {"versionId": FHIR_VERSION},
        "total": len(entries),
        "entry": entries,
    }
