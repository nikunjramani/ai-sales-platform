"""Moved here by ADR 0026 (module order)."""

from typing import Protocol

from sales_platform.platform.domain.tenancy import TenantContext
from sales_platform.quotations.domain.pricing import PricingPolicy


class PricingPolicyNotConfigured(Exception):
    """The tenant has no pricing policy; quotations cannot be calculated safely."""


class PricingPolicyReader(Protocol):
    def get(self, context: TenantContext) -> PricingPolicy: ...
