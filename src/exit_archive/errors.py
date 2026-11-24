"""Typed errors. Messages are built only from safe fields (status, resource, fixed text)."""


class ExitArchiveError(Exception):
    """Base class for all expected export failures."""


class _HarvestHttpError(ExitArchiveError):
    default_status: int
    reason: str

    def __init__(self, resource: str | None = None) -> None:
        self._init(resource, self.default_status)

    def _init(self, resource: str | None, status_code: int) -> None:
        self.status_code = status_code
        self.resource = resource
        where = f" for {resource}" if resource else ""
        super().__init__(f"Harvest returned {status_code}{where}: {self.reason}")


class AuthError(_HarvestHttpError):
    default_status = 401
    reason = "the token was rejected"


class ForbiddenError(_HarvestHttpError):
    default_status = 403
    reason = "access to this resource is not allowed"


class NotFoundError(_HarvestHttpError):
    default_status = 404
    reason = "resource not found"


class RateLimitedError(_HarvestHttpError):
    default_status = 429
    reason = "rate limit retries exhausted"


class HarvestUnavailableError(_HarvestHttpError):
    default_status = 503
    reason = "Harvest is unavailable after retries"

    def __init__(self, resource: str | None = None, status_code: int = 503) -> None:
        if not 500 <= status_code <= 599:
            raise ValueError("status_code must be in 500-599")
        self._init(resource, status_code)


class ExportError(ExitArchiveError):
    """Export-level failure with a fixed, caller-written safe message."""
