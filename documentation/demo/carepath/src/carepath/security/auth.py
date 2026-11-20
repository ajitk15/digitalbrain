"""Bearer tokens and the principal behind one. NFR-05, REG-02.

Tokens are compared as SHA-256 digests with `hmac.compare_digest`, so the stored
form is not the usable form and the comparison does not leak length through
timing. REG-02 wants a unique identifier per user: the principal carries the
staff identifier, and that identifier is what lands in the audit trail, never
the token.

The seeded tokens exist because this is a demonstration service with no identity
provider attached. A deployment replaces `TOKENS` with the network's own
directory - see ADR-0002.
"""

import hashlib
import hmac
from dataclasses import dataclass

from .rbac import Permission, Role, allows


@dataclass(frozen=True)
class Principal:
    """Who is making this request, and in what capacity."""

    subject: str
    role: Role
    organisation: str

    def may(self, permission: Permission) -> bool:
        return allows(self.role, permission)


def digest(token: str) -> str:
    """The stored form of a token."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


#: Demonstration directory. Digest to principal, never token to principal.
TOKENS: dict[str, Principal] = {
    digest("demo-clinician-token"): Principal(
        "dr.okafor@riverside.example", Role.CLINICIAN, "Riverside General"
    ),
    digest("demo-coordinator-token"): Principal(
        "j.mbeki@riverside.example", Role.COORDINATOR, "Riverside General"
    ),
    digest("demo-auditor-token"): Principal(
        "compliance@riverside.example", Role.AUDITOR, "Riverside General"
    ),
    digest("demo-partner-token"): Principal(
        "exchange@northvale.example", Role.PARTNER, "Northvale Clinic"
    ),
}


def principal_for(token: str) -> Principal | None:
    """Resolve a bearer token to a principal, or None.

    Every candidate is compared even after a match, so the time taken does not
    depend on where in the directory the token sits.
    """
    presented = digest(token or "")
    found: Principal | None = None
    for stored, principal in TOKENS.items():
        if hmac.compare_digest(stored, presented):
            found = principal
    return found
