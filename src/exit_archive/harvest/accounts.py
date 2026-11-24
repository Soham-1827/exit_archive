"""Discover and choose the Harvest account to export."""

from dataclasses import dataclass

from exit_archive.errors import ExportError
from exit_archive.harvest.http import HarvestClient
from exit_archive.secrets import HarvestToken

ID_BASE_URL = "https://id.getharvest.com"
ACCOUNTS_PATH = "/api/v2/accounts"


@dataclass(frozen=True)
class Account:
    id: int
    name: str
    product: str


async def discover_accounts(token: HarvestToken) -> list[Account]:
    """List the Harvest (not Forecast) accounts the token can see."""
    async with HarvestClient(token, None, base_url=ID_BASE_URL) as client:
        data = await client.get(ACCOUNTS_PATH, resource="accounts")
    raw = data.get("accounts")
    if not isinstance(raw, list):
        raise ExportError("Harvest account list had an unexpected shape")
    accounts: list[Account] = []
    for item in raw:
        if not isinstance(item, dict) or item.get("product") != "harvest":
            continue
        account_id, name = item.get("id"), item.get("name")
        if not isinstance(account_id, int) or not isinstance(name, str):
            raise ExportError("Harvest account list had an unexpected shape")
        accounts.append(Account(id=account_id, name=name, product="harvest"))
    return accounts


def choose_account(accounts: list[Account], account_id: int | None) -> Account:
    """Pick the account to export, or fail with a message the user can act on."""
    if not accounts:
        raise ExportError("This token has no Harvest accounts")
    if account_id is not None:
        for account in accounts:
            if account.id == account_id:
                return account
        raise ExportError(f"Account id {account_id} is not available to this token")
    if len(accounts) == 1:
        return accounts[0]
    listing = ", ".join(f"{a.id} ({a.name})" for a in accounts)
    raise ExportError(f"Several Harvest accounts found; choose one by id: {listing}")
