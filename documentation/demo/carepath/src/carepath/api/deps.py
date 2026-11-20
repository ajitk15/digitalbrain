"""Shared FastAPI dependencies: the connection, the principal, the permission.

`require` returns a dependency rather than being one, so a route declares the
permission it needs in its own signature and the check cannot be forgotten in a
handler body where a reviewer would have to go looking for it.
"""

from collections.abc import Callable, Iterator

from fastapi import Depends, Header, HTTPException, status

from ..db import transaction
from ..security.auth import Principal, principal_for
from ..security.rbac import Permission

UNAUTHENTICATED = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="A bearer token is required.",
    headers={"WWW-Authenticate": "Bearer"},
)


def unit_of_work() -> Iterator:
    """Yield a connection whose transaction commits when the request succeeds."""
    with transaction() as connection:
        yield connection


def caller(authorization: str = Header(default="")) -> Principal:
    """Resolve the bearer token on the request, or refuse it. NFR-05."""
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise UNAUTHENTICATED
    principal = principal_for(token.strip())
    if principal is None:
        raise UNAUTHENTICATED
    return principal


def require(permission: Permission) -> Callable[[Principal], Principal]:
    """Build a dependency that admits only a principal holding `permission`."""

    def check(principal: Principal = Depends(caller)) -> Principal:
        if not principal.may(permission):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"This role may not {permission}.",
            )
        return principal

    return check
