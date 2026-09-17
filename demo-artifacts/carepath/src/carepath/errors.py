"""Domain errors, mapped to HTTP status codes at the edge.

Services raise these; only `carepath.app` knows what an HTTP status code is. The
message on a `NotFound` is deliberately incurious - a caller who is not entitled
to a record is told the record does not exist, never that it exists and is
withheld (REG-03).
"""


class CarePathError(Exception):
    """Base class for every error this service raises deliberately."""


class NotFound(CarePathError):
    """The record does not exist, or the caller may not know that it does."""


class Conflict(CarePathError):
    """The request contradicts something already recorded."""


class Invalid(CarePathError):
    """The request is well-formed but not acceptable."""


class ConsentWithheld(CarePathError):
    """The patient has not consented to this use of their record. BR-05."""


class RiskEngineUnavailable(CarePathError):
    """The scoring model could not be reached. NFR-10 - degrade, do not fail."""
