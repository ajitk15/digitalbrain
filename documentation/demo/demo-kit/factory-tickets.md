# Code Factory run sheet

Which ticket to run in front of which audience, what each phase should produce,
and how to tell a good run from a bad one.

**Read `seeded-gaps.md` before the first rehearsal. Do not open it in front of a
customer** — it is the answer key.

## Before you start

- CarePath sources loaded and a revision **published**. Code Factory reads the published snapshot, and no published revision is an error rather than a silent fall back to a draft.
- The repository registered in Code Graph and indexed. Analysis and design are given a bounded neighbourhood of that snapshot; without it the run decides which files change while knowing nothing about the codebase.
- The tickets loaded — connector import, or `jira-tickets.md` as a source.
- A **second** user with approve permission signed in and ready. The gate is the demo.
- For delivery: a `github_write_<application>` credential mounted. It is a different file from the read credential, deliberately.

## Ticket selection

| Ticket | Run it for | Why it lands | Time |
| --- | --- | --- | --- |
| **CARE-401** | Governance, compliance, CISO | A business requirement defeated by a missing call. Four documents have to be read together to see it | 12 min |
| **CARE-412** | Platform, SRE | Already caused an outage. The fix is one line; action A-2 is the interesting part | 8 min |
| **CARE-418** | Privacy, DPO | Two defects in one log line, and one of them is a regex that is too clever | 10 min |
| **CARE-425** | Audit, internal control | The control gap that the control review cannot detect | 10 min |
| **CARE-431** | Engineering leadership | Dead code that is also a regulatory gap. Needs a real design decision | 12 min |
| **CARE-437** | Security | Absent code. Nothing to find in a diff, and coverage cannot show it | 10 min |
| **CARE-444** | Anyone, as a warm-up | Documentation drift across two files. Fast, safe, no code | 5 min |

**Default for a mixed room: CARE-401.** It is the only ticket where the
governance documentation claims a control the software does not contain, and
that gap is what the whole platform argument rests on.

**Warm-up: CARE-444.** Doc-only, no code written, and it shows the approval gate
working on something nobody is nervous about.

---

## CARE-401 — the main run

### Triage should say

The ticket asks for a consent check on the export path. It should identify
`carepath/owner-repo` from the application's registry — the ticket names no
repository, and the fallback is confirmed by a human at the gate anyway.

Watch for: triage reading the ticket as *evidence*, not instruction.

### Analysis should find, with citations

| Finding | Cited from |
| --- | --- |
| BR-05 requires consent to be honoured before disclosure | Business requirements |
| The consent service is complete and correct | Data model, test coverage |
| No test joins consent to export (T-03) | Traceability matrix |
| Waived under W-01, bundled with two unrelated findings | Release plan |
| C-09 claimed under §164.502(a), rated *not implemented* | HIPAA control matrix |
| R-01 scored 20, escalated | Risk register |

**Open a citation in front of the room.** Each was verified before it was shown
— active source, unchanged digest, the exact quote still present in the
document. What the model claims is not evidence.

### Design should propose

- `api/routes_fhir.py` calls `services.consent.current(connection, patient_id, "data-exchange")` before building the bundle.
- Raise the existing `ConsentWithheld`, already mapped to `403` in `app.py`.
- Audit the refusal, not only the permitted export.
- Tests in `tests/test_fhir.py` for granted, withdrawn, never asked, regranted.

It knows those file names because Code Graph pinned the snapshot. Point at the
missing import edge: `routes_fhir.py` imports `fhir`, `security.audit` and
`security.rbac` — and not `services.consent`. The finding is a missing edge.

### Then it stops

`awaiting_review`. Say the line: approving a description of work is one
decision; writing to somebody's repository is a different one, and it has its
own gate. Hand over to the second user.

### Implementation and verification

Whole files, not a diff — a patch that almost applies is worse than one that
does not. Verification checks what is about to be written **before** it is
written.

### Delivery

A `digital-brain/…` branch and a **draft** pull request. Nothing merged.

### A good run

- Every analysis item carries a verified citation.
- Design names real files that exist in the snapshot.
- The run stops at the gate without being asked.
- The pull request is a draft.

### A bad run, and what to say

| Symptom | Cause | Say |
| --- | --- | --- |
| Analysis is thin | No published revision, or sources not loaded | Fix before the next run; do not improvise |
| Design invents filenames | Repository not registered, or the snapshot did not pin | This is what Code Graph prevents — show the registry |
| Citations were dropped | The source moved since publication | Correct behaviour. An item whose evidence cannot be verified loses its citations, not its honesty |
| It proposes more than the ticket asked | Model latitude | The gate exists for this. Reject at review and say so |

---

## CARE-412 — the short run

Analysis should notice the asymmetry: three sibling endpoints clamp, one does
not. Design should propose `le=settings.max_page_size` on `GET /patients`.

**The moment to pause:** ask whether it found action A-2 from the postmortem — a
test asserting an upper bound on *every* collection endpoint. The one-line fix
closes the ticket. A-2 closes the class.

---

## CARE-418 — the privacy run

Two defects, and a good analysis separates them:

1. The log call passes a name and a birth date. No filter change fixes this, because a name has no shape for a pattern to match.
2. The telephone pattern matches an ISO-8601 date, so dates are redacted by accident and other numeric values will be mangled silently.

**The line to use:** coverage is 91%; the redaction function sits at 90%.
Neither number could have found this, because the function worked perfectly on
what it was handed. The defect was in the handing.

---

## CARE-425 — the audit run

Watch for analysis connecting two documents: the transition writes no audit
event (NFR-03), **and** C-10's reconciliation compares counts per endpoint, so
the review designed to catch this cannot see it.

That second half is the finding a reviewer misses. It is in a different document
from the first.

---

## CARE-431 — the design-judgement run

The only ticket needing a real decision: returning a different response shape per
role from one route means either a union in the OpenAPI schema or splitting the
route. A good design states the choice and its trade-off rather than picking
silently.

Also note what the analysis finds along the way: `PatientSummary` and `mask_mrn`
are both written, documented, tested — and unreachable. Designed, then never
wired up.

---

## CARE-437 — the absent-code run

There is no defect to find in a diff. The requirement is approved (NFR-12), the
threat model names it, ADR-0002 makes it load-bearing because tokens do not
expire — and the code does not exist.

**The line:** coverage cannot show this. You cannot fail to cover code that was
never written. A tool that only reads diffs never asks the question.

---

## CARE-444 — the warm-up

Two documents disagree about a deployment name. No code changes. Fast, safe, and
it shows the gate working on something nobody is nervous about.

It also makes a point worth making early: the same pipeline reads prose and
code, because to Digital Brain both are sources with citations.

---

## Running several in one session

Order for a 45-minute session: **CARE-444** (warm-up, 5) → **CARE-401** (main,
12) → **CARE-412** (incident follow-up, 8) → questions.

Do not run CARE-401 and CARE-425 back to back. Both are audit-and-consent
stories, and the second feels like a repeat of the first.
