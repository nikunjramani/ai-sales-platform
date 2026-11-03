"""ERPNext as the place a business's orders are written (phase 7, ADR 0024).

One Sales Order per order. Our stable reference (QO-<order id>) goes in the Sales Order's
"Customer's Purchase Order" field (po_no), which ERPNext lists and filters by, so an uncertain
create can be looked up (reference lookup). Prices are the accepted ones, with ERPNext's
pricing rules switched off for the document, so ERPNext never re-prices the order.

Orders arrive as drafts unless the business chose "Submit orders"; staff then submit them in
ERPNext, and the order's status follows ERPNext (get_order_status).

ERPNext refuses a second Sales Order with the same po_no for the same customer unless the
business allows it in Selling Settings, so that refusal cannot be relied on: before creating,
the writer always looks the reference up, and after a refusal it looks again. A second create
with the same reference therefore answers the existing order, never a second one.
"""

from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sales_platform.catalogue.ports.erp import (
    ErpError,
    ErpErrorCode,
    ErpOrder,
    ErpOrderState,
    SalesOrderRequest,
)
from sales_platform.platform.domain.tenancy import TenantContext
from sales_platform.providers.erpnext.client import FrappeClient
from sales_platform.providers.erpnext.config import ErpnextSettings

_ORDER_FIELDS = ("name", "po_no", "docstatus", "status", "per_delivered")


def _utc_now() -> datetime:
    return datetime.now(UTC)


def order_state(row: Mapping[str, Any]) -> ErpOrderState:
    """ERPNext's docstatus, status and delivered percentage in our words."""
    docstatus = int(row.get("docstatus") or 0)
    if docstatus == 2 or row.get("status") == "Cancelled":
        return ErpOrderState.CANCELLED
    if docstatus == 0:
        return ErpOrderState.DRAFT
    if row.get("status") == "On Hold":
        return ErpOrderState.ON_HOLD
    delivered = Decimal(str(row.get("per_delivered") or 0))
    if delivered >= 100:
        return ErpOrderState.DELIVERED
    if delivered > 0:
        return ErpOrderState.IN_PROGRESS
    return ErpOrderState.CONFIRMED


class ErpnextOrderWriter:
    def __init__(
        self,
        client: FrappeClient,
        settings: ErpnextSettings,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._client = client
        self._settings = settings
        self._clock = clock

    def create_sales_order(self, context: TenantContext, order: SalesOrderRequest) -> ErpOrder:
        existing = self.get_order_by_external_reference(context, order.reference)
        if existing is not None:
            return existing
        delivery = (order.ordered_on + timedelta(days=self._settings.delivery_days)).isoformat()
        document = {
            "customer": order.customer or self._settings.default_customer,
            "po_no": order.reference,
            "po_date": order.ordered_on.isoformat(),
            "transaction_date": order.ordered_on.isoformat(),
            "delivery_date": delivery,
            "currency": order.currency,
            "selling_price_list": self._settings.price_list,
            "ignore_pricing_rule": 1,
            "docstatus": 1 if self._settings.submit_orders else 0,
            "items": [
                {
                    "item_code": line.sku,
                    "qty": str(line.quantity),
                    "rate": str(line.unit_price),
                    "delivery_date": delivery,
                    "description": f"{order.order_number}: {line.sku}",
                }
                for line in order.lines
            ],
        }
        try:
            created = self._client.insert("Sales Order", document)
        except ErpError as error:
            if error.code in {ErpErrorCode.INVALID_REQUEST, ErpErrorCode.CONFLICT}:
                # Perhaps a racing create used the reference first: then that order is ours.
                again = self.get_order_by_external_reference(context, order.reference)
                if again is not None:
                    return again
            raise
        return self._order(created)

    def get_order_by_external_reference(
        self, context: TenantContext, reference: str
    ) -> ErpOrder | None:
        rows = self._client.get_list(
            "Sales Order", _ORDER_FIELDS, [["po_no", "=", reference]], order_by="creation asc"
        )
        if not rows:
            return None
        # A cancelled copy and a live one: the live one is the order.
        live = [row for row in rows if int(row.get("docstatus") or 0) != 2]
        return self._order((live or rows)[0])

    def get_order_status(self, context: TenantContext, erp_order_id: str) -> ErpOrder:
        return self._order(self._client.get_doc("Sales Order", erp_order_id))

    def _order(self, row: Mapping[str, Any]) -> ErpOrder:
        name = str(row.get("name") or "")
        if not name:
            raise ErpError(ErpErrorCode.UNKNOWN_OUTCOME, "ERPNext answered without an order")
        return ErpOrder(
            erp_order_id=name,
            reference=str(row.get("po_no") or ""),
            state=order_state(row),
            fetched_at=self._clock(),
        )
