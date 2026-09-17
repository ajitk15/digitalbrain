"""The FastAPI application: routers, error mapping and the request middleware.

Domain errors are translated here and nowhere else, so a service can be called
from a background job without a web framework in scope.
"""

import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import __version__, logging_setup
from .api import (
    routes_audit,
    routes_consent,
    routes_encounters,
    routes_fhir,
    routes_health,
    routes_patients,
    routes_referrals,
)
from .db import connect
from .errors import (
    CarePathError,
    Conflict,
    ConsentWithheld,
    Invalid,
    NotFound,
    RiskEngineUnavailable,
)

log = logging.getLogger(__name__)

#: Domain error to HTTP status. One table, so the mapping is reviewable.
#: Ordered most specific first, because the lookup walks it in order.
STATUS_FOR: dict[type[CarePathError], int] = {
    NotFound: 404,
    Conflict: 409,
    Invalid: 422,
    ConsentWithheld: 403,
    RiskEngineUnavailable: 503,
}

DESCRIPTION = """
Care coordination and referral service for a regional health network.

**Demonstration software.** It holds synthetic records only and is not a
medical device. See the notice in demo-artifacts/README.md.
"""


def create_app() -> FastAPI:
    """Build the application. Called by the server and by the test fixtures."""
    logging_setup.configure()
    application = FastAPI(
        title="CarePath",
        version=__version__,
        description=DESCRIPTION,
        openapi_tags=[
            {"name": "patients", "description": "Registration, retrieval and risk."},
            {"name": "encounters", "description": "Admissions, attendances and observations."},
            {"name": "referrals", "description": "Referrals between providers."},
            {"name": "consent", "description": "What the patient has agreed to."},
            {"name": "fhir", "description": "FHIR R4 export for partner systems."},
            {"name": "audit", "description": "Who read what, and why."},
            {"name": "health", "description": "Liveness and readiness."},
        ],
    )

    @application.middleware("http")
    async def correlate(request: Request, call_next):
        """Give every request an identifier and return it on the response. NFR-08."""
        incoming = request.headers.get("x-correlation-id")
        identifier = incoming or logging_setup.new_correlation_id()
        logging_setup.correlation_id.set(identifier)
        response = await call_next(request)
        response.headers["x-correlation-id"] = identifier
        return response

    @application.exception_handler(CarePathError)
    async def domain_errors(request: Request, error: CarePathError) -> JSONResponse:
        """Map a domain error to its status code.

        Registered against the base class rather than each subclass: Starlette
        walks the exception's ancestry looking for a handler, so one
        registration covers every error the services raise and a new error
        class cannot be added without a status.

        Anything that is *not* a `CarePathError` is left alone deliberately. It
        reaches the server as an unhandled exception, which is a plain 500 with
        no body describing it, and the correlation identifier on the response
        is how an operator finds the line that does.
        """
        for kind, code in STATUS_FOR.items():
            if isinstance(error, kind):
                return JSONResponse(status_code=code, content={"detail": str(error)})
        log.warning("Unmapped domain error serving %s", request.url.path)
        return JSONResponse(status_code=500, content={"detail": "Internal error."})

    for module in (
        routes_health,
        routes_patients,
        routes_encounters,
        routes_referrals,
        routes_consent,
        routes_fhir,
        routes_audit,
    ):
        application.include_router(module.router)

    connect()
    return application


app = create_app()
