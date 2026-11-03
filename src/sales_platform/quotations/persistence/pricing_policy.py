"""Moved here by ADR 0026 (module order)."""

from datetime import timedelta

from sqlalchemy import Engine, select

from sales_platform.accounts.persistence.tables import TenantPricingPolicy
from sales_platform.platform.domain.money import RoundingMode
from sales_platform.platform.domain.tenancy import TenantContext
from sales_platform.platform.persistence.tenancy import tenant_transaction
from sales_platform.quotations.domain.pricing import PricingPolicy
from sales_platform.quotations.ports.pricing_policy import PricingPolicyNotConfigured


class SqlPricingPolicyReader:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def get(self, context: TenantContext) -> PricingPolicy:
        table = TenantPricingPolicy
        with tenant_transaction(self._engine, context.tenant_id) as connection:
            row = connection.execute(
                select(
                    table.tenant_id,
                    table.version,
                    table.currency,
                    table.minor_units,
                    table.rounding_mode,
                    table.tax_rate,
                    table.max_price_age_seconds,
                    table.max_stock_age_seconds,
                ).where(table.tenant_id == context.tenant_id)
            ).one_or_none()
        if row is None:
            raise PricingPolicyNotConfigured("No pricing policy is configured for this tenant")
        if (
            row.currency is None
            or row.minor_units is None
            or row.rounding_mode is None
            or row.tax_rate is None
            or row.max_price_age_seconds is None
            or row.max_stock_age_seconds is None
        ):
            # "Set later" (phase 2): never price with values nobody has confirmed.
            raise PricingPolicyNotConfigured("The pricing settings are not complete yet")
        return PricingPolicy(
            tenant_id=row.tenant_id,
            version=row.version,
            currency=row.currency,
            minor_units=row.minor_units,
            rounding=RoundingMode(row.rounding_mode),
            tax_rate=row.tax_rate,
            max_price_age=timedelta(seconds=row.max_price_age_seconds),
            max_stock_age=timedelta(seconds=row.max_stock_age_seconds),
        )
