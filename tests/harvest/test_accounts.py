"""Account discovery and selection tests (respx mocks only)."""

import pytest
import respx

from exit_archive.errors import AuthError, ExportError
from exit_archive.harvest.accounts import Account, choose_account, discover_accounts
from exit_archive.secrets import HarvestToken

SECRET = "fake-token-abcdef-SECRET"  # noqa: S105 (fake test value)
URL = "https://id.getharvest.com/api/v2/accounts"

H1 = Account(id=111, name="Acme Co", product="harvest")
H2 = Account(id=222, name="Beta LLC", product="harvest")


@respx.mock
async def test_discover_filters_forecast() -> None:
    route = respx.get(URL).respond(
        200,
        json={
            "user": {"id": 1},
            "accounts": [
                {"id": 111, "name": "Acme Co", "product": "harvest"},
                {"id": 333, "name": "Acme Forecast", "product": "forecast"},
            ],
        },
    )
    accounts = await discover_accounts(HarvestToken(SECRET))
    assert accounts == [H1]
    req = route.calls[0].request
    assert req.headers["Authorization"] == f"Bearer {SECRET}"
    assert req.headers["User-Agent"].startswith("ExitArchive/")


@respx.mock
async def test_discover_forecast_only_gives_empty() -> None:
    respx.get(URL).respond(200, json={"accounts": [{"id": 3, "name": "F", "product": "forecast"}]})
    assert await discover_accounts(HarvestToken(SECRET)) == []


@respx.mock
async def test_discover_auth_error_has_no_token(caplog: pytest.LogCaptureFixture) -> None:
    respx.get(URL).respond(401)
    with pytest.raises(AuthError) as info:
        await discover_accounts(HarvestToken(SECRET))
    assert SECRET not in str(info.value)
    assert SECRET not in caplog.text


@respx.mock
async def test_discover_malformed_accounts_is_export_error() -> None:
    respx.get(URL).respond(200, json={"accounts": [{"name": "no id", "product": "harvest"}]})
    with pytest.raises(ExportError):
        await discover_accounts(HarvestToken(SECRET))


@respx.mock
async def test_discover_missing_accounts_key_is_export_error() -> None:
    respx.get(URL).respond(200, json={"user": {}})
    with pytest.raises(ExportError):
        await discover_accounts(HarvestToken(SECRET))


def test_choose_single_account() -> None:
    assert choose_account([H1], None) == H1


def test_choose_single_account_with_matching_id() -> None:
    assert choose_account([H1], 111) == H1


def test_choose_several_without_id_lists_ids_and_names() -> None:
    with pytest.raises(ExportError) as info:
        choose_account([H1, H2], None)
    msg = str(info.value)
    for text in ("111", "Acme Co", "222", "Beta LLC"):
        assert text in msg


def test_choose_several_with_id() -> None:
    assert choose_account([H1, H2], 222) == H2


def test_choose_unknown_id() -> None:
    with pytest.raises(ExportError) as info:
        choose_account([H1, H2], 999)
    assert "999" in str(info.value)


def test_choose_none_available() -> None:
    with pytest.raises(ExportError):
        choose_account([], None)
