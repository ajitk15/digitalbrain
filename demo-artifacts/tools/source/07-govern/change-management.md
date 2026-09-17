# Change management — CarePath

| | |
| --- | --- |
| **Document** | GOV-CAREPATH-003 |
| **Version** | 1.2 |
| **Status** | Active |
| **Owner** | Delivery Manager |

## Classes of change

| Class | Examples | Approval | Lead time |
| --- | --- | --- | --- |
| **Standard** | Dependency patch, documentation, a test | Peer review | None |
| **Normal** | A new endpoint, a schema change, a change to a role's grants | Peer review plus the release review | One release cycle |
| **Significant** | Anything touching audit, consent, authentication or the FHIR contract | Release review plus Information Governance | Two release cycles |
| **Emergency** | Production is degraded or a control is failing | On-call plus one senior engineer, retrospectively reviewed within two working days | Immediate |

A change to `security/audit.py`, `services/consent.py` or `security/auth.py` is
**always** significant, regardless of size. A one-line change to an audit path
is a one-line change to a regulatory control.

## Pull request requirements

- [ ] Names the requirement identifiers it affects
- [ ] Tests for both the success and the refusal path
- [ ] Traceability matrix updated if coverage changed
- [ ] Coding standards checklist completed
- [ ] For a significant change: the control matrix reviewed and, if its effectiveness rating moves, updated in the same change

The last one matters. A control matrix updated in a separate change is a control
matrix that drifts, and this one already carries a rating (C-09) that describes
an intention rather than an implementation.

## Waivers

A waiver permits release with a known finding open.

**Rules, tightened after INC-2026-0814:**

1. **One waiver, one finding.** A waiver covering several findings is invalid. W-01 covered three and carried the weakest rationale onto the strongest finding (R-10).
2. The rationale must address **exposure** (who can reach it) **and blast radius** (what one authorised user can do with it) separately. W-01 addressed only exposure, which is why T-01 became INC-2026-0814.
3. Every waiver has an expiry — a date or a named event, never "until fixed".
4. Waivers are reviewed at every subsequent release review, not only at the one that granted them.
5. A waiver on a finding that touches a **business** requirement, rather than a non-functional one, goes to the Clinical Systems Board as its own agenda item.

Rule 5 exists because of T-03. It was waived alongside two non-functional
findings and was the only one of the three that defeated a stated business
requirement (BR-05), and the board minute records no discussion of it.

## Emergency changes

Emergency authority covers restoring service. It does not cover:

- Disabling an audit write to get past a failing audit table. **Take the service out of the pool instead.** A disclosure that cannot be recorded is a disclosure that is not made.
- Widening a role's grants to unblock a user.
- Bypassing a consent check.

Each of those is a control, and a control suspended under time pressure is a
control that is found suspended six months later.

## Records

Every change keeps: the pull request, the review, the release record, the
deployment record with its snapshot identifier, and any waiver. Retained for six
years, alongside the audit trail (NFR-09).
