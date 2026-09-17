"""Audit endpoints. REG-01.

Read-only by construction: there is no route here that writes, updates or
deletes an audit event, because an audit trail an application can edit is not
one.
"""

from fastapi import APIRouter, Depends

from ..schemas import AuditEventOut
from ..security.audit import events_for
from ..security.auth import Principal
from ..security.rbac import Permission
from .deps import require, unit_of_work

router = APIRouter(prefix="/audit", tags=["audit"])


@router.get("/{resource_type}/{resource_id}", response_model=list[AuditEventOut])
def resource_history(
    resource_type: str,
    resource_id: str,
    connection=Depends(unit_of_work),
    principal: Principal = Depends(require(Permission.AUDIT_READ)),
) -> list[dict]:
    """Every recorded access to one resource, oldest first."""
    return events_for(connection, resource_type, resource_id)
