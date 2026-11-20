"""Liveness and readiness. FR-12, NFR-01.

Two endpoints rather than one. Liveness answers "is this process running"; the
orchestrator restarts the container when it stops answering. Readiness answers
"can this process serve traffic", which needs the database, and a failing
readiness check takes the instance out of the load balancer without restarting
it. Collapsing them turns a database blip into a restart loop.
"""

from fastapi import APIRouter, Depends, Response, status

from .. import __version__
from ..config import settings
from ..schemas import HealthOut
from .deps import unit_of_work

router = APIRouter(tags=["health"])


@router.get("/health/live", response_model=HealthOut)
def live() -> dict:
    """The process is up. No dependency is consulted."""
    return {"status": "live", "version": __version__, "environment": settings.environment}


@router.get("/health/ready", response_model=HealthOut)
def ready(response: Response, connection=Depends(unit_of_work)) -> dict:
    """The process can serve traffic, which means the database answers."""
    try:
        connection.execute("SELECT 1").fetchone()
    except Exception:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "unready", "version": __version__, "environment": settings.environment}
    return {"status": "ready", "version": __version__, "environment": settings.environment}
