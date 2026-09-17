"""Build `jira-export.json` in the shape `/rest/api/3/search` actually returns.

The Jira connector in `platform_core/connector_kinds.py` reads three fields per
issue - `key`, `fields.summary` and `fields.description` - and flattens the
description from Atlassian Document Format to plain text. This script produces
that shape from the plain-text bodies below, so the demonstration export is a
faithful stand-in for a live import rather than an approximation of one.

    python build_jira_export.py

Run it from this directory. It writes `jira-export.json` beside itself.
"""

import json
from pathlib import Path

BASE = "https://riverside.atlassian.example"

ISSUES: list[tuple[str, str, str, str, str, str]] = [
    (
        "CARE-401",
        "Patient consent is not checked before a record is exported to a partner",
        "Bug",
        "Highest",
        "2026-09-14T09:12:00.000+0000",
        """BR-05 requires that the patient's recorded consent decision is honoured before their record leaves the organisation. The consent record itself is complete and correct: decisions are captured per purpose, the history is append-only, and consent.current() returns the right answer, including the correct default of withheld for a patient who has never been asked.

Nothing reads it.

GET /fhir/Patient/{patient_id}/$everything checks that the caller holds the fhir:export permission and then builds and returns the bundle. There is no call to the consent service anywhere on that path.

The HIPAA control matrix claims this control as C-09 under 164.502(a) and rates it not implemented. The release notes instruct the requesting party to confirm consent manually. A manual check performed by the party who wants the export is not a control.

Steps to reproduce: register a patient; record purpose data-exchange granted false; request the export as a partner. Expected 403 and an audit record of the refusal. Actual 200 with the full bundle. The seed cohort includes a patient in this state: Priya Raman, MRN RGH-0177310.

Acceptance criteria: an export for a patient whose current data-exchange decision is withheld, or who has never been asked, is refused; the refusal uses the existing ConsentWithheld error, which already maps to 403; the refusal is written to the audit trail, because a refused disclosure is as much a disclosure event as a permitted one; a permitted export continues to work unchanged; tests cover granted, withdrawn, never asked, and withdrawn-then-regranted.""",
    ),
    (
        "CARE-412",
        "Patient search accepts an unbounded limit",
        "Bug",
        "High",
        "2026-09-12T14:40:00.000+0000",
        """GET /patients declares limit with a default of 50 and no upper bound. The value is passed to the database unchanged.

The three sibling listing endpoints for encounters, referrals and consent all declare le=settings.max_page_size and refuse a larger value with 422. Patient search is the odd one out, which is why it survived review: a reviewer reading any one of the other three sees a clamp and moves on.

NFR-06 states that the clamp is the requirement. An endpoint that accepts a caller-supplied limit and passes it through satisfies the letter of taking a limit and none of the intent.

Realised as INC-2026-0814 on 2026-08-14. A coordinator searched for family name A to browse the cohort alphabetically, with limit=100000. The service read the whole patient table, held a transaction past the two-second ceiling in NFR-11, and blocked every write behind it on SQLite's single writer. Forty-five minutes, roughly sixty per cent of requests failing.

Acceptance criteria: GET /patients clamps limit to settings.max_page_size, matching the sibling endpoints exactly; a limit above the maximum returns 422; a test asserts an upper bound on every collection endpoint, not only the ones that have one today, which is postmortem action A-2.""",
    ),
    (
        "CARE-418",
        "Patient name and date of birth are written to the application log",
        "Bug",
        "High",
        "2026-09-10T11:05:00.000+0000",
        """Found during exploratory testing, cycle TC-3. Calling GET /patients/{id}/risk produces a log line reading: Scoring readmission risk for Amara Okonjo born [phone].

Two defects in one line.

First, the patient's name is in a log that NFR-04 says must not carry protected health information. Logs ship to the platform aggregator, where retention and access are governed differently from the clinical record.

Second, the date of birth has been replaced by [phone]. The telephone pattern in security/phi.py matches an ISO-8601 date. Dates are being redacted by accident rather than by rule, and the same pattern will silently mangle any other long numeric value: a LOINC code with a hyphen, an identifier, a measurement.

The redaction filter is working as designed and the design is too broad. Note also that redaction can only catch identifiers with a shape; a name has none, so no filter change fixes the first defect. The log call has to stop passing it.

Acceptance criteria: the scoring log line identifies the patient by surrogate id only, carrying no name and no date of birth; the telephone pattern no longer matches an ISO-8601 date; a test pins that an ISO date passes through redact unchanged; existing redaction tests continue to pass.""",
    ),
    (
        "CARE-425",
        "Referral status changes are not written to the audit trail",
        "Bug",
        "Medium",
        "2026-09-08T16:22:00.000+0000",
        """services/referrals.py transition updates the referral row and returns it. It does not call audit.record.

Every other write path in the service audits: register, read, search, open_encounter, add_observation, referrals.create, consent.record_decision. This is the single exception, and the function already takes the Principal it would need, so the omission is visible in the signature.

NFR-03 requires an audit event in the same transaction as every access to patient data. A referral status change is a change to the patient's clinical record: it is what tells a partner organisation the patient is coming. The HIPAA control matrix rates C-03 partially effective for this reason.

The quarterly review will not catch it. C-10's reconciliation compares access counts against audit counts per endpoint, so an endpoint that writes no audit event at all contributes to neither side and is invisible to the review designed to find exactly this.

Acceptance criteria: transition writes an audit event in the same transaction as the update; the action distinguishes a status change from a creation; the event records the transition that occurred, so an access report shows what changed and not merely that something did; a test asserts the audit event in the same style as the other write-path tests.""",
    ),
    (
        "CARE-431",
        "An auditor receives the full patient record, including telephone and address",
        "Bug",
        "Medium",
        "2026-09-05T10:31:00.000+0000",
        """REG-03, HIPAA 164.502(b) minimum necessary, requires that disclosure is limited to what the purpose needs. The security design states the intent plainly: a caller entitled to know a patient exists is not thereby entitled to their contact details.

The auditor role holds patient:read so that a compliance officer can reconcile an access report against real patients. GET /patients/{id} returns PatientOut to every caller holding that permission, so an auditor receives the name, full date of birth, postal code and telephone number.

A PatientSummary schema already exists in schemas.py, with a masked MRN, the family name and the birth year only. Its docstring says it is returned to a caller who is entitled to know a patient exists but not to read their contact details, an auditor reconciling an access report for instance. Nothing returns it. The schema was designed, documented, and never wired to a route. mask_mrn in security/phi.py is in the same position: written, tested, unused.

Acceptance criteria: a caller with the auditor role receives PatientSummary from GET /patients/{id}; a caller with the clinician or coordinator role continues to receive PatientOut unchanged; the response shape is determined by the caller's role, not by a query parameter the caller chooses; the read is audited identically in both cases; tests cover both roles.""",
    ),
    (
        "CARE-437",
        "Rate limit the authentication path",
        "Story",
        "Medium",
        "2026-09-03T08:50:00.000+0000",
        """NFR-12 requires authentication to be rate-limited per source to resist credential stuffing. It is approved, it is in the threat model, and it is not implemented.

ADR-0002 makes this load-bearing rather than optional. Tokens are static with no expiry beyond a ninety-day rotation, so resistance to guessing has to come from somewhere, and rate limiting is where the ADR says it comes from.

This is not a defect in written code. It is absent code, which is why no coverage report shows it and no test fails: you cannot fail to cover code that was never written.

Acceptance criteria: repeated failed authentications from one source are refused with 429 after a configured threshold; the threshold and window are configuration, not constants; a successful authentication does not consume budget; rate-limit refusals are logged with the correlation identifier and without the presented token; the limiter's state does not leak whether a token exists.

Per-process counters are enough for the pilot's single instance. Say so in the code, because at two instances it silently becomes twice the intended limit.""",
    ),
    (
        "CARE-444",
        "Operations runbook names a deployment that was retired",
        "Task",
        "Low",
        "2026-09-01T13:15:00.000+0000",
        """docs/06-operate/operations-runbook.md says the deployments are carepath-api-blue serving and carepath-api-green standby.

docs/05-release/deployment-runbook.md says they were renamed at release 1.0.0 to carepath-api-green and carepath-api-amber, because blue was read as a colour by half the team and as an environment name by the other half, and two people deployed to the wrong one during the rehearsal.

The operations runbook was last reviewed on 2026-03-18, before release 1.0.0 was planned. Its platformctl logs command names a deployment that no longer exists.

This is R-11 in the risk register: an operational procedure followed confidently from a document that no longer matches the deployment. A runbook with wrong names is worse than no runbook, because it is followed with confidence.

Acceptance criteria: the operations runbook names the deployments as the deployment runbook defines them; every command in it is checked against the deployed shape; the review date is updated; the release plan's overdue runbook review item is closed.""",
    ),
]


def adf(body: str) -> dict:
    """Wrap plain text as Atlassian Document Format, one paragraph per block."""
    return {
        "type": "doc",
        "version": 1,
        "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": paragraph}]}
            for paragraph in body.strip().split("\n\n")
        ],
    }


def main() -> None:
    payload = {
        "expand": "schema,names",
        "startAt": 0,
        "maxResults": 100,
        "total": len(ISSUES),
        "issues": [
            {
                "id": str(10000 + index),
                "key": key,
                "self": f"{BASE}/rest/api/3/issue/{10000 + index}",
                "fields": {
                    "summary": summary,
                    "description": adf(body),
                    "updated": updated,
                    "issuetype": {"name": kind},
                    "priority": {"name": priority},
                },
            }
            for index, (key, summary, kind, priority, updated, body) in enumerate(ISSUES)
        ],
    }
    target = Path(__file__).with_name("jira-export.json")
    target.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"Wrote {target} with {len(ISSUES)} issues.")


if __name__ == "__main__":
    main()
