"""Whether a product is still needed by quotations (ADR 0026; formerly inside the catalogue
store): its SKU appears in any quotation version or prepared enquiry (phase 3, Q12)."""

from uuid import UUID

from sqlalchemy import Connection, exists, func, select

from sales_platform.enquiries.persistence.tables import WorkflowRun
from sales_platform.quotations.persistence.tables import QuotationVersion


def sku_was_quoted(connection: Connection, tenant_id: UUID, sku: str) -> bool:
    """For SqlCatalogueStore's delete guard (catalogue.persistence.catalogue.ProductInUse)."""
    line = func.jsonb_build_array(func.jsonb_build_object("sku", sku))
    return bool(
        connection.execute(
            select(
                exists().where(
                    QuotationVersion.tenant_id == tenant_id,
                    QuotationVersion.content["lines"].op("@>")(line),
                )
                | exists().where(
                    WorkflowRun.tenant_id == tenant_id,
                    WorkflowRun.outcome["lines"].op("@>")(line),
                )
            )
        ).scalar_one()
    )
