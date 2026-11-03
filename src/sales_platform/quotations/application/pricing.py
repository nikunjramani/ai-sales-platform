"""Price requested lines for one tenant using live ERP facts and the tenant's policy."""

from collections.abc import Callable, Sequence
from datetime import UTC, datetime

from sales_platform.catalogue.ports.erp import ProductSource
from sales_platform.enquiries.domain.requirements import RequestedLine
from sales_platform.platform.domain.tenancy import Permission, TenantContext
from sales_platform.quotations.domain.pricing import PricingOutcome, calculate_quotation
from sales_platform.quotations.ports.pricing_policy import PricingPolicyReader


def utc_now() -> datetime:
    return datetime.now(UTC)


class QuotationPricingService:
    """Read-only use case: it calculates a draft and stores or sends nothing.

    Raises PermissionDenied, PricingPolicyNotConfigured or ErpError. ERP failures propagate
    so a caller can retry or escalate; no fallback values are substituted.
    """

    def __init__(
        self,
        erp: ProductSource,
        policies: PricingPolicyReader,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._erp = erp
        self._policies = policies
        self._clock = clock

    def price(self, context: TenantContext, lines: Sequence[RequestedLine]) -> PricingOutcome:
        context.require(Permission.DRAFT_QUOTATIONS)
        policy = self._policies.get(context)
        if policy.tenant_id != context.tenant_id:
            raise RuntimeError("Policy reader returned another tenant's policy")
        now = self._clock()
        skus = list(dict.fromkeys(line.sku for line in lines))
        products = self._erp.get_products(context, skus)
        prices = self._erp.get_prices(context, skus, policy.currency, now)
        stock = self._erp.get_availability(context, skus)
        return calculate_quotation(
            policy=policy, lines=lines, products=products, prices=prices, stock=stock, now=now
        )
