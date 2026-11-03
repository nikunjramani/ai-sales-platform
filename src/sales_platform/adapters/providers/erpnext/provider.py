"""Provider `erpnext` (phase 6/7): a business's ERPNext as product source and order destination.

Enable (one click) shows one form: the ERPNext address, an API user's key and secret, the
price list, warehouses and the other choices in config.ErpnextForm. Sending it checks the key
and the names with harmless reads; then the connection is active, becomes the business's
product source (replacing the previous one) and the first catalogue copy is queued.

- catalogue_reader: our copy for search, ERPNext live for prices and stock (catalogue.py);
- fetch_catalogue: the whole catalogue for the sync job;
- order_writer: only when the business turned "Write orders to ERPNext" on (orders.py).
The API key and secret are kept in the SecretStore; the connection holds only references.
To change a setting, Disable and Enable again (the form asks for everything once more).
"""

import ipaddress
import socket
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

import httpx
from pydantic import ValidationError

from sales_platform.adapters.catalogue.local import LocalCatalogue
from sales_platform.adapters.providers.erpnext.catalogue import ErpnextCatalogue, build_feed
from sales_platform.adapters.providers.erpnext.client import FrappeClient
from sales_platform.adapters.providers.erpnext.config import ErpnextForm, ErpnextSettings
from sales_platform.adapters.providers.erpnext.orders import ErpnextOrderWriter
from sales_platform.domain.catalogue_sync import CatalogueFeed
from sales_platform.domain.connections import (
    Capabilities,
    CatalogueCapabilities,
    Connection,
    OrderCapabilities,
    Purpose,
)
from sales_platform.domain.intake import InboundEvent
from sales_platform.ports.catalogue_admin import CatalogueReadStore
from sales_platform.ports.erp import ErpError, ErpErrorCode, ErpReader, ErpWriter
from sales_platform.ports.providers import (
    CostNote,
    InboundRequest,
    OperationUnsupported,
    PendingAction,
    PendingActionKind,
    ProviderDefaults,
    ProviderEnvironment,
    ProvisionResult,
    ProvisionStatus,
    RenderedMessage,
    SendReceipt,
    TestResult,
)
from sales_platform.ports.secrets import SecretStore

PROVIDER_KEY = "erpnext"
SETTINGS_ACTION = "settings"
SECRET_KEY = "api_key"
SECRET_SECRET = "api_secret"


def _utc_now() -> datetime:
    return datetime.now(UTC)


def form_schema() -> dict[str, object]:
    """The form's JSON schema with enums inlined (the portal reads `enum` on each field)."""
    schema = ErpnextForm.model_json_schema()
    definitions: Mapping[str, Any] = schema.pop("$defs", {})
    properties: dict[str, Any] = {}
    for name, field in schema["properties"].items():
        reference = field.pop("$ref", None) or next(
            (item.get("$ref") for item in field.pop("allOf", []) if "$ref" in item), None
        )
        if reference:
            field = {**definitions[reference.rsplit("/", 1)[-1]], **field}
        properties[name] = field
    return {**schema, "properties": properties}


def _short(error: ValidationError) -> str:
    """The first problem per field, in plain words (shown on the form)."""
    parts = []
    for item in error.errors()[:5]:
        where = ".".join(str(piece) for piece in item.get("loc", ()) if piece != "__root__")
        message = str(item.get("msg", "invalid")).removeprefix("Value error, ")
        parts.append(f"{where}: {message}" if where else message)
    return "; ".join(parts) or "Please check the form"


def _public_host(url: str) -> None:
    """Production: only HTTPS addresses on the public internet (no internal hosts)."""
    parts = urlsplit(url)
    if parts.scheme != "https":
        raise ValueError("The ERPNext address must start with https://")
    try:
        addresses = {info[4][0] for info in socket.getaddrinfo(parts.hostname, parts.port or 443)}
    except (OSError, UnicodeError):
        raise ValueError("The ERPNext address could not be found") from None
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if not ip.is_global:
            raise ValueError("The ERPNext address must be reachable on the internet")


class ErpnextProvider(ProviderDefaults):
    key = PROVIDER_KEY
    display_name = "ERPNext"
    purposes = frozenset({Purpose.CATALOGUE, Purpose.ORDERS})
    capabilities = Capabilities(
        webhooks=False,
        text=False,
        outbound_initiation=False,
        manager_enrollment="none",
        self_service=True,
        catalogue=CatalogueCapabilities(
            sync=True, live_prices=True, live_stock=True, warehouses=True
        ),
        orders=OrderCapabilities(create_order=True, reference_lookup=True, order_status=True),
    )
    cost = CostNote(free=True, text="Free from us; your ERPNext plan's API limits apply")

    def __init__(
        self,
        environment: str,
        store: CatalogueReadStore | None,
        http: httpx.Client | None = None,
        clock: Callable[[], datetime] = _utc_now,
        check_host: Callable[[str], None] | None = None,
    ) -> None:
        self._environment = environment
        self._store = store
        self._http = http
        self._clock = clock
        if check_host is None and environment == "production":
            check_host = _public_host
        self._check_host = check_host

    # --- one-click setup -------------------------------------------------------------------
    def provision(self, conn: Connection, secrets: SecretStore) -> ProvisionResult:
        return ProvisionResult(
            status=ProvisionStatus.NEEDS_ACTION,
            pending=PendingAction(
                action_id=SETTINGS_ACTION,
                kind=PendingActionKind.FILL_FORM,
                title="Connect your ERPNext",
                steps=(
                    "In ERPNext, open the user that should connect (for example a dedicated "
                    "API user with Sales and Stock read access, plus Sales Order if orders "
                    "are written), then API Access > Generate Keys.",
                ),
                form_schema=form_schema(),
            ),
        )

    def resume(
        self,
        conn: Connection,
        secrets: SecretStore,
        action_id: str,
        submission: Mapping[str, object],
    ) -> ProvisionResult:
        if action_id != SETTINGS_ACTION:
            raise OperationUnsupported("ERPNext has no such setup step")
        try:
            form = ErpnextForm.model_validate(dict(submission))
        except ValidationError as error:
            raise ValueError(_short(error)) from None
        if self._check_host is not None:
            self._check_host(form.base_url)
        settings = ErpnextSettings.from_form(form)
        client = self._client(
            settings, form.api_key.get_secret_value(), form.api_secret.get_secret_value()
        )
        try:
            _check(client, settings)
        except ErpError as error:
            raise ValueError(_plain(error)) from None
        host = urlsplit(settings.base_url).hostname
        return ProvisionResult(
            status=ProvisionStatus.READY,
            detail=f"Products from {host}, copied every {settings.sync_minutes} minutes"
            + ("; orders are written to ERPNext" if settings.write_orders else ""),
            config=settings.as_config(),
            secrets={
                SECRET_KEY: form.api_key.get_secret_value(),
                SECRET_SECRET: form.api_secret.get_secret_value(),
            },
        )

    def deprovision(self, conn: Connection, secrets: SecretStore) -> tuple[str, ...]:
        return ("If nobody else uses it, remove the API key of the connected ERPNext user.",)

    def test(self, conn: Connection, secrets: SecretStore) -> TestResult:
        try:
            settings, client = self._open(conn, secrets)
            _check(client, settings)
        except OperationUnsupported as error:
            return TestResult(ok=False, detail=str(error))
        except ErpError as error:
            return TestResult(ok=False, detail=_plain(error))
        return TestResult(ok=True, detail="ERPNext answered and the price list was found")

    # --- data ------------------------------------------------------------------------------
    def catalogue_reader(self, conn: Connection, secrets: SecretStore) -> ErpReader:
        if Purpose.CATALOGUE not in conn.purposes or self._store is None:
            raise OperationUnsupported("This connection is not a product source")
        settings, client = self._open(conn, secrets)
        return ErpnextCatalogue(client, settings, LocalCatalogue(self._store), self._clock)

    def fetch_catalogue(self, conn: Connection, secrets: SecretStore) -> CatalogueFeed:
        settings, client = self._open(conn, secrets)
        return build_feed(client, settings, self._clock())

    def order_writer(self, conn: Connection, secrets: SecretStore) -> ErpWriter:
        settings, client = self._open(conn, secrets)
        if Purpose.ORDERS not in conn.purposes or not settings.write_orders:
            raise OperationUnsupported("Orders are not written to this ERPNext")
        return ErpnextOrderWriter(client, settings, self._clock)

    # --- no messages -----------------------------------------------------------------------
    def verify_inbound(
        self, conn: Connection, secrets: SecretStore, request: InboundRequest
    ) -> None:
        raise OperationUnsupported("ERPNext sends no callbacks here")

    def parse_inbound(self, conn: Connection, request: InboundRequest) -> Sequence[InboundEvent]:
        raise OperationUnsupported("ERPNext sends no callbacks here")

    def send(self, conn: Connection, secrets: SecretStore, message: RenderedMessage) -> SendReceipt:
        raise OperationUnsupported("ERPNext sends no messages")

    # --- internals -------------------------------------------------------------------------
    def _open(self, conn: Connection, secrets: SecretStore) -> tuple[ErpnextSettings, FrappeClient]:
        raw = conn.config.get("erpnext")
        refs = conn.secret_refs
        if not isinstance(raw, dict) or SECRET_KEY not in refs or SECRET_SECRET not in refs:
            raise OperationUnsupported("This ERPNext connection is not set up")
        settings = ErpnextSettings.model_validate(raw)
        key = secrets.get(conn.tenant_id, refs[SECRET_KEY])
        secret = secrets.get(conn.tenant_id, refs[SECRET_SECRET])
        return settings, self._client(settings, key, secret)

    def _client(self, settings: ErpnextSettings, key: str, secret: str) -> FrappeClient:
        return FrappeClient(settings.base_url, key, secret, self._http)


def _check(client: FrappeClient, settings: ErpnextSettings) -> None:
    """Harmless reads: the key works, the price list sells, the warehouses exist."""
    client.logged_user()
    price_list = client.get_doc("Price List", settings.price_list)
    if not price_list.get("selling"):
        raise ErpError(
            ErpErrorCode.INVALID_REQUEST, f"'{settings.price_list}' is not a selling price list"
        )
    found = {
        str(row.get("name"))
        for row in client.get_list_in("Warehouse", ("name",), "name", list(settings.warehouses))
    }
    missing = [name for name in settings.warehouses if name not in found]
    if missing:
        raise ErpError(ErpErrorCode.NOT_FOUND, f"Warehouse not found: {', '.join(missing)}")
    if settings.write_orders:
        client.get_doc("Customer", settings.default_customer)


def _plain(error: ErpError) -> str:
    if error.code is ErpErrorCode.NOT_FOUND:
        return f"ERPNext does not have that: {error}"
    if error.code is ErpErrorCode.TEMPORARY_UNAVAILABLE:
        return f"ERPNext could not be reached: {error}"
    return str(error)


def build(environment: ProviderEnvironment) -> ErpnextProvider:
    """Entry point `erpnext` in group sales_platform.providers."""
    return ErpnextProvider(environment.environment, environment.catalogue_store)
