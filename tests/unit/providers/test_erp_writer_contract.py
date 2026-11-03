"""OrderWriter contract (phase 7): every writer, simulated or real, must pass these.

The real ERPNext sandbox joins with the integration marker when its address and key are in the
environment (SALES_TEST_ERPNEXT_URL, _KEY, _SECRET, _CUSTOMER; see
.docs/operations/provider-setup.md#erpnext). Never a production ERP: it creates orders.
"""

import os
from collections.abc import Callable
from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest

from sales_platform.catalogue.ports.erp import (
    ErpError,
    ErpErrorCode,
    OrderLineRequest,
    OrderWriter,
    SalesOrderRequest,
)
from sales_platform.orders.adapters.erp.simulated_orders import SimulatedOrderWriter
from sales_platform.platform.domain.tenancy import Role, TenantContext

SKU_ENV = "SALES_TEST_ERPNEXT_SKU"


def _simulated() -> OrderWriter:
    return SimulatedOrderWriter()


def _sandbox() -> OrderWriter:
    from sales_platform.providers.erpnext.client import FrappeClient
    from sales_platform.providers.erpnext.config import ErpnextForm, ErpnextSettings
    from sales_platform.providers.erpnext.orders import ErpnextOrderWriter

    url = os.environ.get("SALES_TEST_ERPNEXT_URL")
    key = os.environ.get("SALES_TEST_ERPNEXT_KEY")
    secret = os.environ.get("SALES_TEST_ERPNEXT_SECRET")
    if not (url and key and secret):
        pytest.skip("No ERPNext sandbox configured")
    form = ErpnextForm.model_validate(
        {
            "base_url": url,
            "api_key": key,
            "api_secret": secret,
            "price_list": "Standard Selling",
            "warehouses": os.environ.get("SALES_TEST_ERPNEXT_WAREHOUSE", "Stores - PS"),
            "write_orders": True,
            "default_customer": os.environ.get("SALES_TEST_ERPNEXT_CUSTOMER", "Sandbox Customer"),
        }
    )
    return ErpnextOrderWriter(FrappeClient(url, key, secret), ErpnextSettings.from_form(form))


WRITERS = [
    pytest.param(_simulated, id="simulated"),
    pytest.param(_sandbox, id="erpnext-sandbox", marks=pytest.mark.integration),
]


@pytest.fixture(params=WRITERS)
def writer(request: pytest.FixtureRequest) -> OrderWriter:
    factory: Callable[[], OrderWriter] = request.param
    return factory()


def _context() -> TenantContext:
    return TenantContext(uuid4(), "contract", frozenset({Role.VIEWER}))


def _order(reference: str) -> SalesOrderRequest:
    sku = os.environ.get(SKU_ENV, "PUMP-100")
    return SalesOrderRequest(
        reference,
        "O-CONTRACT",
        None,
        "INR",
        (OrderLineRequest(sku, Decimal("1"), "each", Decimal("100.00")),),
        date.today(),
    )


def test_a_created_order_is_found_by_its_reference(writer: OrderWriter) -> None:
    context = _context()
    reference = f"QO-{uuid4().hex}"
    created = writer.create_sales_order(context, _order(reference))
    found = writer.get_order_by_external_reference(context, reference)
    assert found is not None and found.erp_order_id == created.erp_order_id
    assert found.reference == reference
    assert (
        writer.get_order_status(context, created.erp_order_id).erp_order_id == created.erp_order_id
    )


def test_the_same_reference_never_makes_a_second_order(writer: OrderWriter) -> None:
    context = _context()
    reference = f"QO-{uuid4().hex}"
    first = writer.create_sales_order(context, _order(reference))
    try:
        again = writer.create_sales_order(context, _order(reference))
    except ErpError as error:
        assert error.code is ErpErrorCode.CONFLICT  # the other allowed answer
    else:
        assert again.erp_order_id == first.erp_order_id


def test_an_unknown_reference_is_absent_not_invented(writer: OrderWriter) -> None:
    assert writer.get_order_by_external_reference(_context(), f"QO-{uuid4().hex}") is None


def test_an_unknown_order_status_is_not_found(writer: OrderWriter) -> None:
    with pytest.raises(ErpError) as raised:
        writer.get_order_status(_context(), f"MISSING-{uuid4().hex[:8]}")
    assert raised.value.code is ErpErrorCode.NOT_FOUND


def test_simulated_orders_stay_with_their_business() -> None:
    writer = SimulatedOrderWriter()
    one, other = _context(), _context()
    created = writer.create_sales_order(one, _order("QO-shared"))
    assert writer.get_order_by_external_reference(other, "QO-shared") is None
    with pytest.raises(ErpError):
        writer.get_order_status(other, created.erp_order_id)


def test_a_timeout_after_commit_leaves_exactly_one_order_to_find() -> None:
    writer = SimulatedOrderWriter(mode="timeout_after_commit")
    context = _context()
    with pytest.raises(ErpError) as raised:
        writer.create_sales_order(context, _order("QO-chaos"))
    assert raised.value.code is ErpErrorCode.UNKNOWN_OUTCOME
    assert writer.get_order_by_external_reference(context, "QO-chaos") is not None
    assert writer.orders_created == 1
