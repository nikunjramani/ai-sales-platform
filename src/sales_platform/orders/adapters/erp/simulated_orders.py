"""An in-memory ERP that takes sales orders, for tests and chaos drills (phase 7).

Faults a test can inject (`mode`), the ones that matter for "never two orders":
- "refuse_before_send": nothing reaches the ERP (temporary_unavailable, retryable);
- "timeout_after_commit": the order IS created, but the caller hears unknown_outcome;
- "reject": the ERP refuses for good (invalid_request);
- "lookup_down": reference look-ups fail (temporary_unavailable).
Orders are kept per tenant and reference, so a second create with the same reference answers
the existing order, like the ERPNext writer. `orders_created` counts real creations.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID

from sales_platform.catalogue.ports.erp import (
    ErpError,
    ErpErrorCode,
    ErpOrder,
    ErpOrderState,
    SalesOrderRequest,
)
from sales_platform.platform.domain.tenancy import TenantContext


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass
class SimulatedOrderWriter:
    clock: Callable[[], datetime] = _utc_now
    mode: str | None = None
    orders: dict[tuple[UUID, str], ErpOrder] = field(default_factory=dict)
    requests: list[SalesOrderRequest] = field(default_factory=list)
    initial_state: ErpOrderState = ErpOrderState.DRAFT

    @property
    def orders_created(self) -> int:
        return len(self.orders)

    def create_sales_order(self, context: TenantContext, order: SalesOrderRequest) -> ErpOrder:
        key = (context.tenant_id, order.reference)
        if key in self.orders:
            return self.orders[key]
        if self.mode == "refuse_before_send":
            raise ErpError(ErpErrorCode.TEMPORARY_UNAVAILABLE, "Connection refused")
        if self.mode == "reject":
            raise ErpError(ErpErrorCode.INVALID_REQUEST, "Customer not found")
        self.requests.append(order)
        created = ErpOrder(
            f"SO-{len(self.orders) + 1:05d}", order.reference, self.initial_state, self.clock()
        )
        self.orders[key] = created
        if self.mode == "timeout_after_commit":
            raise ErpError(ErpErrorCode.UNKNOWN_OUTCOME, "Read timeout after sending")
        return created

    def get_order_by_external_reference(
        self, context: TenantContext, reference: str
    ) -> ErpOrder | None:
        if self.mode == "lookup_down":
            raise ErpError(ErpErrorCode.TEMPORARY_UNAVAILABLE, "ERP unavailable")
        return self.orders.get((context.tenant_id, reference))

    def get_order_status(self, context: TenantContext, erp_order_id: str) -> ErpOrder:
        for (tenant_id, _), order in self.orders.items():
            if tenant_id == context.tenant_id and order.erp_order_id == erp_order_id:
                return order
        raise ErpError(ErpErrorCode.NOT_FOUND, "No such order")

    def set_state(self, tenant_id: UUID, reference: str, state: ErpOrderState) -> None:
        """The business changed the order in its ERP (tests of the status refresh)."""
        order = self.orders[(tenant_id, reference)]
        self.orders[(tenant_id, reference)] = ErpOrder(
            order.erp_order_id, order.reference, state, self.clock()
        )
