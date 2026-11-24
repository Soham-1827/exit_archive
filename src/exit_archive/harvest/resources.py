"""Registry of Harvest resources to export, company feature gating and streaming fetchers.

Nothing here writes files or holds a whole account in memory: records are yielded one at a
time as pages arrive. ``invoice_payments`` is not a registry entry; it is fetched per invoice.
"""

from collections.abc import AsyncIterator, Callable, Iterable
from dataclasses import dataclass
from typing import Any

from exit_archive.errors import ExportError, ForbiddenError, NotFoundError
from exit_archive.harvest.http import HarvestClient

COMPANY_PATH = "/v2/company"
FEATURE_FLAGS = ("invoice_feature", "expense_feature", "estimate_feature")


@dataclass(frozen=True)
class Resource:
    name: str
    path: str
    key: str
    feature: str | None = None


def _resource(name: str, feature: str | None = None) -> Resource:
    return Resource(name=name, path=f"/v2/{name}", key=name, feature=feature)


REGISTRY: tuple[Resource, ...] = (
    _resource("clients"),
    _resource("contacts"),
    _resource("projects"),
    _resource("tasks"),
    _resource("users"),
    _resource("user_assignments"),
    _resource("task_assignments"),
    _resource("time_entries"),
    _resource("expense_categories", "expense_feature"),
    _resource("expenses", "expense_feature"),
    _resource("invoice_item_categories", "invoice_feature"),
    _resource("invoices", "invoice_feature"),
    _resource("estimate_item_categories", "estimate_feature"),
    _resource("estimates", "estimate_feature"),
)

# Fetched per invoice (GET /v2/invoices/{id}/payments); gated by the invoice feature.
INVOICE_PAYMENTS = Resource(
    name="invoice_payments",
    path="/v2/invoices/{id}/payments",
    key="invoice_payments",
    feature="invoice_feature",
)


@dataclass(frozen=True)
class Company:
    base_uri: str
    name: str
    invoice_feature: bool
    expense_feature: bool
    estimate_feature: bool


async def fetch_company(client: HarvestClient) -> Company:
    """GET /v2/company and validate the fields the export depends on."""
    data = await client.get(COMPANY_PATH, resource="company")
    base_uri, name = data.get("base_uri"), data.get("name")
    flags = {flag: data.get(flag) for flag in FEATURE_FLAGS}
    if (
        not isinstance(base_uri, str)
        or not isinstance(name, str)
        or not all(isinstance(v, bool) for v in flags.values())
    ):
        raise ExportError("Harvest company response had an unexpected shape")
    return Company(
        base_uri=base_uri,
        name=name,
        invoice_feature=bool(flags["invoice_feature"]),
        expense_feature=bool(flags["expense_feature"]),
        estimate_feature=bool(flags["estimate_feature"]),
    )


def _enabled(resource: Resource, company: Company) -> bool:
    return resource.feature is None or bool(getattr(company, resource.feature))


def skipped_resources(company: Company) -> list[str]:
    """Names of resources (including ``invoice_payments``) disabled by the company's flags."""
    names = [r.name for r in REGISTRY if not _enabled(r, company)]
    if not _enabled(INVOICE_PAYMENTS, company):
        names.append(INVOICE_PAYMENTS.name)
    return names


def iter_resource(client: HarvestClient, resource: Resource) -> AsyncIterator[dict[str, Any]]:
    """Stream every record of one registry resource."""
    return client.paginate(resource.path, resource.key)


def _check_invoice_id(invoice_id: object) -> int:
    if isinstance(invoice_id, bool) or not isinstance(invoice_id, int):
        raise ExportError("Harvest invoice has no integer id")
    if invoice_id < 1:
        raise ExportError("Harvest invoice has an invalid id")
    return invoice_id


async def iter_invoice_payments(
    client: HarvestClient,
    invoice_ids: Iterable[int],
    on_missing: Callable[[dict[str, Any]], None] | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Stream payments for each invoice id, tagging each (as a new dict) with ``invoice_id``.

    Every id is validated before the first request. With ``on_missing``, a 404/403 on one
    invoice's payments is reported as ``{type, id, reason}`` and the stream continues; without
    it the error propagates. Auth, rate-limit and outage errors always propagate.
    """
    ids = [_check_invoice_id(i) for i in invoice_ids]
    for invoice_id in ids:
        path = f"/v2/invoices/{invoice_id}/payments"
        try:
            async for payment in client.paginate(path, INVOICE_PAYMENTS.key):
                if not isinstance(payment, dict):
                    raise ExportError("Harvest returned an unexpected invoice payment shape")
                yield {**payment, "invoice_id": invoice_id}
        except (NotFoundError, ForbiddenError) as exc:
            if on_missing is None:
                raise
            reason = "not_found" if isinstance(exc, NotFoundError) else "forbidden"
            on_missing({"type": INVOICE_PAYMENTS.name, "id": invoice_id, "reason": reason})


async def iter_all(
    client: HarvestClient,
    company: Company,
    on_missing: Callable[[dict[str, Any]], None] | None = None,
) -> AsyncIterator[tuple[str, dict[str, Any]]]:
    """Yield ``(resource_name, record)`` for every enabled resource, in registry order.

    Payments for an invoice are yielded right after that invoice, so only one record is
    held at a time. Disabled resources make no request; see ``skipped_resources``.
    ``on_missing`` receives ``missing[]`` entries for invoices whose payments are gone.
    """
    for resource in REGISTRY:
        if not _enabled(resource, company):
            continue
        async for record in iter_resource(client, resource):
            yield resource.name, record
            if resource.name == "invoices":
                invoice_id: Any = record.get("id")
                async for payment in iter_invoice_payments(client, [invoice_id], on_missing):
                    yield INVOICE_PAYMENTS.name, payment
