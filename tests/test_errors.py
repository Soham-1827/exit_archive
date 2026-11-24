import pytest

from exit_archive.errors import (
    AuthError,
    ExitArchiveError,
    ExportError,
    ForbiddenError,
    HarvestUnavailableError,
    NotFoundError,
    RateLimitedError,
)

SUBCLASSES = [
    AuthError,
    ForbiddenError,
    NotFoundError,
    RateLimitedError,
    HarvestUnavailableError,
    ExportError,
]


@pytest.mark.parametrize("cls", SUBCLASSES)
def test_hierarchy(cls: type[ExitArchiveError]) -> None:
    assert issubclass(cls, ExitArchiveError)
    assert issubclass(ExitArchiveError, Exception)


@pytest.mark.parametrize(
    ("cls", "status"),
    [
        (AuthError, 401),
        (ForbiddenError, 403),
        (NotFoundError, 404),
        (RateLimitedError, 429),
        (HarvestUnavailableError, 503),
    ],
)
def test_http_errors_use_safe_fields(cls: type[ExitArchiveError], status: int) -> None:
    err = cls(resource="time_entries")  # type: ignore[call-arg]
    msg = str(err)
    assert str(status) in msg
    assert "time_entries" in msg
    assert err.status_code == status  # type: ignore[attr-defined]
    assert err.resource == "time_entries"  # type: ignore[attr-defined]


@pytest.mark.parametrize("cls", [AuthError, ForbiddenError, NotFoundError, RateLimitedError])
def test_fixed_status_classes_reject_status_code(cls: type[ExitArchiveError]) -> None:
    with pytest.raises(TypeError):
        cls(resource="x", status_code=500)  # type: ignore[call-arg]


@pytest.mark.parametrize("bad", [200, 404, 499, 600, 0])
def test_unavailable_rejects_non_5xx(bad: int) -> None:
    with pytest.raises(ValueError):
        HarvestUnavailableError(resource="x", status_code=bad)


def test_status_override_for_unavailable() -> None:
    assert "502" in str(HarvestUnavailableError(resource="clients", status_code=502))


def test_auth_error_without_resource() -> None:
    assert "401" in str(AuthError())


def test_export_error_fixed_text() -> None:
    assert str(ExportError("Multiple accounts; choose one")) == "Multiple accounts; choose one"


def test_errors_do_not_carry_arbitrary_input() -> None:
    err = AuthError(resource="clients")
    assert "Bearer" not in str(err)
    assert err.args == (str(err),)
