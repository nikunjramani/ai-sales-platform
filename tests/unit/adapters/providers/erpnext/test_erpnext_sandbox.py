"""The ERPNext package against the real local sandbox (integration marker; skipped unless
SALES_TEST_ERPNEXT_URL, _KEY and _SECRET are set). Seed it first with
scripts/seed_erpnext_sandbox.py; see .docs/operations/provider-setup.md#erpnext.
"""

import os
from datetime import UTC, datetime

import pytest

from sales_platform.adapters.providers.erpnext.catalogue import build_feed
from sales_platform.adapters.providers.erpnext.client import FrappeClient
from sales_platform.adapters.providers.erpnext.config import ErpnextForm, ErpnextSettings
from sales_platform.adapters.providers.erpnext.provider import ErpnextProvider
from sales_platform.ports.providers import ProvisionStatus

pytestmark = pytest.mark.integration


@pytest.fixture
def form() -> dict[str, object]:
    url = os.environ.get("SALES_TEST_ERPNEXT_URL")
    key = os.environ.get("SALES_TEST_ERPNEXT_KEY")
    secret = os.environ.get("SALES_TEST_ERPNEXT_SECRET")
    if not (url and key and secret):
        pytest.skip("No ERPNext sandbox configured")
    return {
        "base_url": url,
        "api_key": key,
        "api_secret": secret,
        "price_list": "Standard Selling",
        "warehouses": os.environ.get("SALES_TEST_ERPNEXT_WAREHOUSE", "Stores - PS"),
    }


def test_enabling_against_the_sandbox_succeeds(form: dict[str, object]) -> None:
    result = ErpnextProvider("test", None).resume(None, None, "settings", form)  # type: ignore[arg-type]
    assert result.status is ProvisionStatus.READY


def test_the_seeded_catalogue_is_copied_with_prices_and_stock(form: dict[str, object]) -> None:
    settings = ErpnextSettings.from_form(ErpnextForm.model_validate(form))
    client = FrappeClient(settings.base_url, str(form["api_key"]), str(form["api_secret"]))
    feed = build_feed(client, settings, datetime.now(UTC))
    products = {product.sku: product.unit for product in feed.products}
    assert {"PUMP-100": "each", "CABLE-2C": "metre", "FAN-400": "each"}.items() <= products.items()
    prices = {price.sku: price for price in feed.prices}
    assert prices["PUMP-100"].currency == "INR" and prices["PUMP-100"].unit_price > 0
    stock = {item.sku: item.quantity for item in feed.stock}
    assert stock["FAN-400"] == 0 and stock["PUMP-100"] is not None
