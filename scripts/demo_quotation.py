"""Print an M1 pricing trace using the local database and the simulated ERP.

Requires a migrated, seeded local database. Reads only; nothing is stored or sent.
"""

from decimal import Decimal

from sales_platform.catalogue.adapters.erp.simulated import SimulatedErp, demo_catalogues
from sales_platform.enquiries.domain.requirements import RequestedLine
from sales_platform.platform.application.tenancy import TenantContextResolver
from sales_platform.platform.config import Settings
from sales_platform.platform.domain.tenancy import AccessDenied, PermissionDenied
from sales_platform.platform.persistence.database import build_engine
from sales_platform.platform.persistence.tenancy import SqlMembershipReader
from sales_platform.quotations.application.pricing import QuotationPricingService
from sales_platform.quotations.domain.pricing import PricedQuotation
from sales_platform.quotations.persistence.pricing_policy import SqlPricingPolicyReader
from sales_platform.seed import INDUSTRIAL_TENANT, OFFICE_TENANT


def main() -> None:
    engine = build_engine(Settings().database_url.get_secret_value())
    try:
        resolver = TenantContextResolver(SqlMembershipReader(engine))
        service = QuotationPricingService(
            SimulatedErp(demo_catalogues(INDUSTRIAL_TENANT, OFFICE_TENANT)),
            SqlPricingPolicyReader(engine),
        )
        sales = resolver.resolve("sales@industrial.example", INDUSTRIAL_TENANT)
        requests = {
            "Valid request": [
                RequestedLine("IND-FAN-450", Decimal(2), "each"),
                RequestedLine("IND-BOLT-M8", Decimal(150), "each"),
                RequestedLine("IND-CABLE-2C", Decimal("12.5"), "metre"),
            ],
            "Problem request": [
                RequestedLine("IND-PUMP-2HP", Decimal(1), "each"),
                RequestedLine("IND-BOLT-M8", Decimal(120), "each"),
                RequestedLine("OFF-DESK-160", Decimal(1), "each"),
            ],
        }
        for title, lines in requests.items():
            print(f"\n== {title} ({sales.subject}) ==")
            outcome = service.price(sales, lines)
            if isinstance(outcome, PricedQuotation):
                print("\n".join(outcome.explain()))
                print(f"Stock review required: {outcome.stock_review_required}")
            else:
                for issue in outcome.issues:
                    print(f"line {issue.line_index} {issue.sku}: {issue.code} - {issue.message}")

        print("\n== Access checks ==")
        try:
            resolver.resolve("sales@industrial.example", OFFICE_TENANT)
        except AccessDenied as error:
            print(f"Industrial user asking for office tenant: {error}")
        viewer = resolver.resolve("viewer@industrial.example", INDUSTRIAL_TENANT)
        try:
            service.price(viewer, requests["Valid request"])
        except PermissionDenied as error:
            print(f"Viewer trying to draft a quote: {error}")
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
