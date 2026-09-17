"""Roles and what each one may do. NFR-05, REG-02, REG-03.

One table, consulted by a dependency on every clinical route. A role that is
not listed holds no permissions at all, so adding a role to the token issuer
without deciding what it may do grants nothing rather than everything.
"""

from enum import StrEnum


class Role(StrEnum):
    CLINICIAN = "clinician"
    COORDINATOR = "coordinator"
    AUDITOR = "auditor"
    PARTNER = "partner"


class Permission(StrEnum):
    PATIENT_READ = "patient:read"
    PATIENT_WRITE = "patient:write"
    ENCOUNTER_READ = "encounter:read"
    ENCOUNTER_WRITE = "encounter:write"
    REFERRAL_READ = "referral:read"
    REFERRAL_WRITE = "referral:write"
    CONSENT_READ = "consent:read"
    CONSENT_WRITE = "consent:write"
    RISK_READ = "risk:read"
    FHIR_EXPORT = "fhir:export"
    AUDIT_READ = "audit:read"


GRANTS: dict[Role, frozenset[Permission]] = {
    Role.CLINICIAN: frozenset(
        {
            Permission.PATIENT_READ,
            Permission.PATIENT_WRITE,
            Permission.ENCOUNTER_READ,
            Permission.ENCOUNTER_WRITE,
            Permission.REFERRAL_READ,
            Permission.REFERRAL_WRITE,
            Permission.CONSENT_READ,
            Permission.RISK_READ,
        }
    ),
    Role.COORDINATOR: frozenset(
        {
            Permission.PATIENT_READ,
            Permission.ENCOUNTER_READ,
            Permission.REFERRAL_READ,
            Permission.REFERRAL_WRITE,
            Permission.CONSENT_READ,
            Permission.CONSENT_WRITE,
            Permission.RISK_READ,
        }
    ),
    Role.AUDITOR: frozenset({Permission.AUDIT_READ, Permission.PATIENT_READ}),
    Role.PARTNER: frozenset({Permission.FHIR_EXPORT}),
}


def allows(role: Role, permission: Permission) -> bool:
    """True when `role` is granted `permission`. An unknown role allows nothing."""
    return permission in GRANTS.get(role, frozenset())
