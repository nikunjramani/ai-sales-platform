"""The ERPNext provider over a fake Frappe REST API (recorded-style answers, no network).

The fake understands the requests the package sends: lists with fields and filters ("=", "in",
"<", "is not set", "descendants of (inclusive)"), single documents, inserts and the logged-user
method. Faults are injected per path. The same package also runs against a real sandbox in
test_erpnext_sandbox.py (integration marker).
"""

import json
from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from urllib.parse import unquote
from uuid import uuid4

import httpx
import pytest

from sales_platform.adapters.catalogue.local import LocalCatalogue
from sales_platform.adapters.erp.simulated import SimulatedCatalogue
from sales_platform.adapters.providers.erpnext.catalogue import ErpnextCatalogue, build_feed
from sales_platform.adapters.providers.erpnext.client import FrappeClient
from sales_platform.adapters.providers.erpnext.config import ErpnextForm, ErpnextSettings
from sales_platform.adapters.providers.erpnext.orders import ErpnextOrderWriter, order_state
from sales_platform.adapters.providers.erpnext.provider import ErpnextProvider, form_schema
from sales_platform.domain.catalogue import Product
from sales_platform.domain.connections import Purpose
from sales_platform.domain.tenancy import Role, TenantContext
from sales_platform.ports.erp import (
    ErpError,
    ErpErrorCode,
    ErpOrderState,
    OrderLineRequest,
    SalesOrderRequest,
)
from sales_platform.ports.providers import OperationUnsupported, ProvisionStatus
from tests.support.catalogue_store import InMemoryCatalogueStore

NOW = datetime(2026, 10, 29, 10, tzinfo=UTC)
BASE = "https://erp.example"
TENANT = uuid4()
CONTEXT = TenantContext(TENANT, "test", frozenset({Role.VIEWER}))


def _items() -> list[dict[str, Any]]:
    return [
        {"item_code": "PUMP-100", "item_name": "Pump", "item_group": "Pumps", "stock_uom": "Nos",
         "is_stock_item": 1, "disabled": 0, "is_sales_item": 1, "has_variants": 0},
        {"item_code": "CABLE-2C", "item_name": "Cable", "item_group": "Cables",
         "stock_uom": "Meter", "is_stock_item": 1, "disabled": 0, "is_sales_item": 1,
         "has_variants": 0},
        {"item_code": "SERVICE-1", "item_name": "Installation", "item_group": "Pumps",
         "stock_uom": "Nos", "is_stock_item": 0, "disabled": 0, "is_sales_item": 1,
         "has_variants": 0},
        {"item_code": "OIL-5", "item_name": "Oil", "item_group": "Pumps", "stock_uom": "Litre",
         "is_stock_item": 1, "disabled": 0, "is_sales_item": 1, "has_variants": 0},
        {"item_code": "BAD CODE", "item_name": "Spaces", "item_group": "Pumps",
         "stock_uom": "Nos", "is_stock_item": 1, "disabled": 0, "is_sales_item": 1,
         "has_variants": 0},
        {"item_code": "OLD-1", "item_name": "Old", "item_group": "Pumps", "stock_uom": "Nos",
         "is_stock_item": 1, "disabled": 1, "is_sales_item": 1, "has_variants": 0},
    ]  # fmt: skip


class FakeFrappe:
    def __init__(self) -> None:
        self.docs: dict[str, list[dict[str, Any]]] = {
            "Item": _items(),
            "UOM": [{"name": "Nos", "must_be_whole_number": 1}, {"name": "Meter"}],
            "Price List": [{"name": "Standard Selling", "currency": "INR", "selling": 1}],
            "Item Price": [
                {
                    "name": "p1",
                    "item_code": "PUMP-100",
                    "price_list": "Standard Selling",
                    "price_list_rate": 4250.0,
                    "currency": "INR",
                    "uom": "",
                    "selling": 1,
                    "customer": None,
                    "valid_from": None,
                    "valid_upto": None,
                    "modified": "m1",
                },
                {
                    "name": "p2",
                    "item_code": "PUMP-100",
                    "price_list": "Standard Selling",
                    "price_list_rate": 4400.0,
                    "currency": "INR",
                    "uom": "Nos",
                    "selling": 1,
                    "customer": None,
                    "valid_from": "2026-11-01",
                    "valid_upto": None,
                    "modified": "m2",
                },
                {
                    "name": "p3",
                    "item_code": "PUMP-100",
                    "price_list": "Standard Selling",
                    "price_list_rate": 3900.0,
                    "currency": "INR",
                    "uom": "",
                    "selling": 1,
                    "customer": "Big Buyer",
                    "valid_from": None,
                    "valid_upto": None,
                    "modified": "m3",
                },
                {
                    "name": "p4",
                    "item_code": "CABLE-2C",
                    "price_list": "Standard Selling",
                    "price_list_rate": 38.5,
                    "currency": "INR",
                    "uom": "Box",
                    "selling": 1,
                    "customer": None,
                    "valid_from": None,
                    "valid_upto": None,
                    "modified": "m4",
                },
            ],
            "Bin": [
                {
                    "item_code": "PUMP-100",
                    "warehouse": "Stores - EX",
                    "actual_qty": 12.0,
                    "reserved_qty": 3.0,
                },
                {
                    "item_code": "PUMP-100",
                    "warehouse": "Transit - EX",
                    "actual_qty": 50.0,
                    "reserved_qty": 0.0,
                },
            ],
            "Warehouse": [{"name": "Stores - EX"}, {"name": "Transit - EX"}],
            "Customer": [{"name": "Sandbox Customer"}],
            "Sales Order": [],
        }
        self.groups = {"Pumps": {"Pumps"}, "All Item Groups": {"Pumps", "Cables"}}
        self.faults: dict[str, Callable[[httpx.Request], httpx.Response]] = {}
        self.inserts: list[dict[str, Any]] = []
        self.auth: list[str] = []

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handle))

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.auth.append(request.headers.get("authorization", ""))
        path = unquote(request.url.path)
        for prefix, fault in self.faults.items():
            if path.startswith(prefix):
                return fault(request)
        return self.answer(request)

    def answer(self, request: httpx.Request) -> httpx.Response:
        """The normal answer, without faults (fault handlers fall back to it)."""
        path = unquote(request.url.path)
        if path == "/api/method/frappe.auth.get_logged_user":
            return httpx.Response(200, json={"message": "api@erp.example"})
        doctype, _, name = path.removeprefix("/api/resource/").partition("/")
        rows = self.docs.get(doctype)
        if rows is None:
            return httpx.Response(404, json={"exc_type": "DoesNotExistError"})
        if request.method == "POST":
            document = json.loads(request.content)
            customer = document.get("customer")
            if doctype == "Sales Order" and customer not in {"Sandbox Customer", "Big Buyer"}:
                return _error(417, "LinkValidationError", f"Could not find Customer: {customer}")
            created = {**document, "name": f"SAL-ORD-{len(rows) + 1:05d}", "status": "Draft",
                       "per_delivered": 0}  # fmt: skip
            rows.append(created)
            self.inserts.append(created)
            return httpx.Response(200, json={"data": created})
        if name:
            found = [row for row in rows if row.get("name") == name]
            if not found:
                return _error(404, "DoesNotExistError", f"{doctype} {name} not found")
            return httpx.Response(200, json={"data": found[0]})
        filters = json.loads(request.url.params.get("filters", "[]"))
        fields = json.loads(request.url.params.get("fields", "[]"))
        start = int(request.url.params.get("limit_start", "0"))
        page = int(request.url.params.get("limit_page_length", "20"))
        matched = [row for row in rows if all(self._match(row, item) for item in filters)]
        data = [{field: row.get(field) for field in fields} for row in matched]
        return httpx.Response(200, json={"data": data[start : start + page]})

    def _match(self, row: dict[str, Any], condition: list[Any]) -> bool:
        field, operator, value = condition
        current = row.get(field)
        if operator == "=":
            return bool(current == value)
        if operator == "<":
            return bool(current is not None and current < value)
        if operator == "in":
            return current in value
        if operator == "is":
            return not current if value == "not set" else bool(current)
        if operator == "descendants of (inclusive)":
            return current in self.groups.get(value, {value})
        raise AssertionError(f"unexpected operator {operator}")


def _error(status: int, kind: str, message: str) -> httpx.Response:
    messages = json.dumps([json.dumps({"message": message})])
    return httpx.Response(status, json={"exc_type": kind, "_server_messages": messages})


def _form(**changes: Any) -> dict[str, Any]:
    return {
        "base_url": f"{BASE}/",
        "api_key": "key-1",
        "api_secret": "secret-1",
        "price_list": "Standard Selling",
        "warehouses": "Stores - EX",
        **changes,
    }


def _settings(**changes: Any) -> ErpnextSettings:
    return ErpnextSettings.from_form(ErpnextForm.model_validate(_form(**changes)))


@pytest.fixture
def erp() -> FakeFrappe:
    return FakeFrappe()


def _client(erp: FakeFrappe) -> FrappeClient:
    return FrappeClient(BASE, "key-1", "secret-1", erp.client())


# --- setup -----------------------------------------------------------------------------------


def test_the_form_schema_inlines_choices_and_marks_secrets() -> None:
    schema = form_schema()
    properties: dict[str, Any] = schema["properties"]  # type: ignore[assignment]
    assert properties["stock_meaning"]["enum"] == ["available", "on_hand"]
    assert properties["stock_meaning"]["default"] == "available"
    assert properties["api_secret"]["writeOnly"] is True
    assert "$defs" not in schema
    required: list[str] = schema["required"]  # type: ignore[assignment]
    assert set(required) == {"base_url", "api_key", "api_secret", "price_list", "warehouses"}


def test_enabling_checks_the_key_and_names_then_keeps_secrets_apart(erp: FakeFrappe) -> None:
    provider = ErpnextProvider("development", None, erp.client())
    pending = provider.provision(None, None)  # type: ignore[arg-type]
    assert pending.status is ProvisionStatus.NEEDS_ACTION and pending.pending is not None
    result = provider.resume(None, None, "settings", _form(sync_minutes=30))  # type: ignore[arg-type]
    assert result.status is ProvisionStatus.READY
    assert result.secrets == {"api_key": "key-1", "api_secret": "secret-1"}
    assert result.config["sync_minutes"] == 30
    stored = json.dumps(result.config)
    assert "secret-1" not in stored and "key-1" not in stored
    assert erp.auth[-1] == "token key-1:secret-1"


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"warehouses": " , "}, "warehouse"),
        ({"write_orders": True}, "customer for orders"),
        ({"base_url": "ftp://erp.example"}, "https://"),
        ({"unit_map": "Nos=pieces"}, "CODE=unit"),
        ({"sync_minutes": 1}, "sync_minutes"),
        ({"surprise": 1}, "surprise"),
    ],
)
def test_bad_forms_are_refused_with_a_short_message(
    erp: FakeFrappe, changes: dict[str, Any], message: str
) -> None:
    provider = ErpnextProvider("development", None, erp.client())
    with pytest.raises(ValueError, match=message):
        provider.resume(None, None, "settings", _form(**changes))  # type: ignore[arg-type]


def test_enabling_refuses_unknown_names_and_bad_keys(erp: FakeFrappe) -> None:
    provider = ErpnextProvider("development", None, erp.client())
    with pytest.raises(ValueError, match="Warehouse not found: Nowhere"):
        provider.resume(None, None, "settings", _form(warehouses="Nowhere"))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="does not have that"):
        provider.resume(None, None, "settings", _form(price_list="Missing"))  # type: ignore[arg-type]
    erp.faults["/api/method"] = lambda request: _error(401, "AuthenticationError", "bad key")
    with pytest.raises(ValueError, match="refused the API key"):
        provider.resume(None, None, "settings", _form())  # type: ignore[arg-type]


def test_production_accepts_only_public_https_addresses(erp: FakeFrappe) -> None:
    provider = ErpnextProvider("production", None, erp.client())
    with pytest.raises(ValueError, match="https://"):
        provider.resume(None, None, "settings", _form(base_url="http://erp.example"))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="internet"):
        provider.resume(None, None, "settings", _form(base_url="https://127.0.0.1"))  # type: ignore[arg-type]


def test_orders_are_written_only_when_the_business_turned_it_on(erp: FakeFrappe) -> None:
    from sales_platform.domain.connections import Connection, ConnectionStatus

    provider = ErpnextProvider("development", None, erp.client())

    class Secrets:
        def get(self, tenant_id: Any, reference: str) -> str:
            return reference

    def connection(write: bool) -> Connection:
        config = _settings(write_orders=write, default_customer="Sandbox Customer").as_config()
        return Connection(
            uuid4(), TENANT, "erpnext", "ERPNext", frozenset({Purpose.CATALOGUE, Purpose.ORDERS}),
            config, {"api_key": "k", "api_secret": "s"}, None, ConnectionStatus.ACTIVE, None,
            "t", NOW, NOW,
        )  # fmt: skip

    with pytest.raises(OperationUnsupported):
        provider.order_writer(connection(False), Secrets())  # type: ignore[arg-type]
    assert isinstance(provider.order_writer(connection(True), Secrets()), ErpnextOrderWriter)  # type: ignore[arg-type]


# --- catalogue -------------------------------------------------------------------------------


def test_the_feed_maps_units_groups_and_reports_what_it_skips(erp: FakeFrappe) -> None:
    feed = build_feed(_client(erp), _settings(), NOW)
    assert {p.sku: (p.unit, str(p.quantity_step)) for p in feed.products} == {
        "PUMP-100": ("each", "1"),
        "CABLE-2C": ("metre", "0.001"),
        "SERVICE-1": ("each", "1"),
    }
    issues = {(issue.code, issue.sku) for issue in feed.issues}
    assert issues == {("unmapped_unit", "OIL-5"), ("invalid_product", "BAD CODE")}
    # A customer-specific price, a future price and a price in another unit are never used.
    assert [(p.sku, p.unit_price) for p in feed.prices] == [("PUMP-100", Decimal("4250.0"))]
    stock = {item.sku: item.quantity for item in feed.stock}
    assert stock == {"PUMP-100": Decimal("9"), "CABLE-2C": Decimal("0"), "SERVICE-1": None}


def test_the_feed_follows_the_chosen_groups_and_the_unit_map(erp: FakeFrappe) -> None:
    feed = build_feed(_client(erp), _settings(item_groups="Cables", unit_map="Litre=kg"), NOW)
    assert [product.sku for product in feed.products] == ["CABLE-2C"]
    feed = build_feed(_client(erp), _settings(unit_map="Litre=kg"), NOW)
    assert "OIL-5" in {product.sku for product in feed.products}


def test_tax_inclusive_prices_are_never_copied(erp: FakeFrappe) -> None:
    feed = build_feed(_client(erp), _settings(prices_include_tax=True), NOW)
    assert feed.prices == () and "tax_included" in {issue.code for issue in feed.issues}


def test_on_hand_stock_ignores_reservations_and_other_warehouses(erp: FakeFrappe) -> None:
    feed = build_feed(_client(erp), _settings(stock_meaning="on_hand"), NOW)
    assert {item.sku: item.quantity for item in feed.stock}["PUMP-100"] == 12
    both = build_feed(_client(erp), _settings(warehouses="Stores - EX, Transit - EX"), NOW)
    assert {item.sku: item.quantity for item in both.stock}["PUMP-100"] == 59


def _reader(erp: FakeFrappe, **changes: Any) -> ErpnextCatalogue:
    # Our synced copy of the three sellable items (what the sync job would have stored).
    copy = SimulatedCatalogue(
        {
            sku: Product(sku, sku, unit, Decimal(1), Decimal(1), "Pumps")
            for sku, unit in (("PUMP-100", "each"), ("CABLE-2C", "metre"), ("SERVICE-1", "each"))
        }
    )
    store = InMemoryCatalogueStore.from_simulated({TENANT: copy})
    return ErpnextCatalogue(_client(erp), _settings(**changes), LocalCatalogue(store), lambda: NOW)


def test_live_prices_come_from_erpnext_in_the_business_currency_only(erp: FakeFrappe) -> None:
    reader = _reader(erp)
    skus = ["PUMP-100", "CABLE-2C", "MISSING"]
    prices = reader.get_prices(CONTEXT, skus, "INR", NOW)
    assert set(prices) == {"PUMP-100"}  # the cable's only price is per box: never converted
    quote = prices["PUMP-100"]
    assert (quote.unit_price.amount, quote.source, quote.price_list_id) == (
        Decimal("4250.0"),
        "erpnext",
        "Standard Selling",
    )
    later = reader.get_prices(CONTEXT, skus, "INR", datetime(2026, 11, 2, tzinfo=UTC))
    assert later["PUMP-100"].unit_price.amount == Decimal("4400.0")
    assert reader.get_prices(CONTEXT, skus, "EUR", NOW) == {}
    assert _reader(erp, prices_include_tax=True).get_prices(CONTEXT, skus, "INR", NOW) == {}


def test_live_stock_says_unknown_for_services_and_honours_the_warehouse(erp: FakeFrappe) -> None:
    reader = _reader(erp)
    levels = reader.get_availability(CONTEXT, ["PUMP-100", "CABLE-2C", "SERVICE-1", "NONE"])
    assert {sku: level.available_quantity for sku, level in levels.items()} == {
        "PUMP-100": Decimal("9"),
        "CABLE-2C": Decimal("0"),  # counted, no stock recorded
        "SERVICE-1": None,  # not counted by ERPNext
    }
    assert levels["PUMP-100"].fetched_at == NOW and levels["PUMP-100"].source == "erpnext"
    assert reader.get_availability(CONTEXT, ["PUMP-100"], warehouse="Elsewhere") == {}


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (401, ErpErrorCode.UNAUTHENTICATED),
        (403, ErpErrorCode.FORBIDDEN),
        (417, ErpErrorCode.INVALID_REQUEST),
        (429, ErpErrorCode.RATE_LIMITED),
        (502, ErpErrorCode.TEMPORARY_UNAVAILABLE),
    ],
)
def test_read_errors_are_normalised(erp: FakeFrappe, status: int, code: ErpErrorCode) -> None:
    erp.faults["/api/resource/Item Price"] = lambda request: _error(status, "Err", "nope")
    with pytest.raises(ErpError) as raised:
        _reader(erp).get_prices(CONTEXT, ["PUMP-100"], "INR", NOW)
    assert raised.value.code is code


def test_an_unreachable_erpnext_is_temporary_for_reads(erp: FakeFrappe) -> None:
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    erp.faults["/api/resource/Bin"] = down
    with pytest.raises(ErpError) as raised:
        _reader(erp).get_availability(CONTEXT, ["PUMP-100"])
    assert raised.value.retryable


# --- orders ----------------------------------------------------------------------------------


def _request(reference: str, customer: str | None = None) -> SalesOrderRequest:
    return SalesOrderRequest(
        reference,
        "O-1A2B3C4D",
        customer,
        "INR",
        (OrderLineRequest("PUMP-100", Decimal("2"), "each", Decimal("4100.00")),),
        date(2026, 10, 29),
    )


def _writer(erp: FakeFrappe, **changes: Any) -> ErpnextOrderWriter:
    settings = _settings(write_orders=True, default_customer="Sandbox Customer", **changes)
    return ErpnextOrderWriter(_client(erp), settings, lambda: NOW)


def test_an_order_carries_our_reference_the_accepted_price_and_a_draft(erp: FakeFrappe) -> None:
    order = _writer(erp).create_sales_order(CONTEXT, _request("QO-1"))
    [sent] = erp.inserts
    assert (sent["po_no"], sent["customer"], sent["docstatus"]) == ("QO-1", "Sandbox Customer", 0)
    assert sent["ignore_pricing_rule"] == 1 and sent["items"][0]["rate"] == "4100.00"
    assert sent["delivery_date"] == "2026-11-05"
    assert order.state is ErpOrderState.DRAFT and order.reference == "QO-1"


def test_a_second_create_with_the_same_reference_returns_the_same_order(erp: FakeFrappe) -> None:
    writer = _writer(erp)
    first = writer.create_sales_order(CONTEXT, _request("QO-2"))
    again = writer.create_sales_order(CONTEXT, _request("QO-2", "Big Buyer"))
    assert again.erp_order_id == first.erp_order_id and len(erp.inserts) == 1


def test_refusals_and_uncertain_writes_are_told_apart(erp: FakeFrappe) -> None:
    writer = _writer(erp)
    with pytest.raises(ErpError) as refused:
        writer.create_sales_order(CONTEXT, _request("QO-3", "Nobody"))
    assert refused.value.code is ErpErrorCode.INVALID_REQUEST

    def timeout(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            raise httpx.ReadTimeout("no answer", request=request)
        return erp.answer(request)

    erp.faults["/api/resource/Sales Order"] = timeout
    with pytest.raises(ErpError) as unknown:
        writer.create_sales_order(CONTEXT, _request("QO-4"))
    assert unknown.value.code is ErpErrorCode.UNKNOWN_OUTCOME

    def refused_connection(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            raise httpx.ConnectError("refused", request=request)
        return erp.answer(request)

    erp.faults["/api/resource/Sales Order"] = refused_connection
    with pytest.raises(ErpError) as before:
        writer.create_sales_order(CONTEXT, _request("QO-5"))
    assert before.value.retryable  # nothing reached ERPNext


def test_a_server_error_while_writing_is_unknown(erp: FakeFrappe) -> None:
    def broken(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return _error(500, "InternalError", "boom")
        return erp.answer(request)

    erp.faults["/api/resource/Sales Order"] = broken
    with pytest.raises(ErpError) as raised:
        _writer(erp).create_sales_order(CONTEXT, _request("QO-6"))
    assert raised.value.code is ErpErrorCode.UNKNOWN_OUTCOME


def test_submitted_orders_and_erpnext_statuses_map_to_our_states(erp: FakeFrappe) -> None:
    writer = _writer(erp, submit_orders=True)
    created = writer.create_sales_order(CONTEXT, _request("QO-7"))
    assert erp.inserts[0]["docstatus"] == 1
    row = erp.docs["Sales Order"][0]
    row.update({"docstatus": 1, "status": "To Deliver and Bill", "per_delivered": 40})
    assert writer.get_order_status(CONTEXT, created.erp_order_id).state is ErpOrderState.IN_PROGRESS
    assert writer.get_order_by_external_reference(CONTEXT, "QO-none") is None
    with pytest.raises(ErpError) as missing:
        writer.get_order_status(CONTEXT, "SAL-ORD-99999")
    assert missing.value.code is ErpErrorCode.NOT_FOUND
    cases: list[tuple[dict[str, Any], ErpOrderState]] = [
        ({"docstatus": 0}, ErpOrderState.DRAFT),
        ({"docstatus": 1, "per_delivered": 0}, ErpOrderState.CONFIRMED),
        ({"docstatus": 1, "status": "On Hold"}, ErpOrderState.ON_HOLD),
        ({"docstatus": 1, "per_delivered": 100, "status": "Completed"}, ErpOrderState.DELIVERED),
        ({"docstatus": 2}, ErpOrderState.CANCELLED),
    ]
    for document, state in cases:
        assert order_state(document) is state
