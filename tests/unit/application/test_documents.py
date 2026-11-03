"""Document access: previews for staff, approved files, and what customers can see."""

from datetime import timedelta
from io import BytesIO
from typing import Any
from uuid import UUID, uuid4

import pytest
from pypdf import PdfReader

from sales_platform.domain.documents import DocumentIntegrityError, DocumentNotAvailable
from sales_platform.domain.quotations import QuotationNotFound
from sales_platform.domain.tenancy import PermissionDenied, Role, TenantContext

FAN = ("IND-FAN-450", "2", "each")
MANAGER = "manager@industrial.example"
VIEWER = "viewer@industrial.example"


def intake_client(harness: Any, tenant_id: UUID | None = None) -> TenantContext:
    """The API key context a customer system uses (intake_service role)."""
    return TenantContext(
        tenant_id or harness.tenant_id,
        "api-key:00000000-0000-4000-8000-0000000000c1",
        frozenset({Role.INTAKE_SERVICE}),
    )


def drafted(harness: Any) -> tuple[UUID, UUID]:
    enquiry = harness.enquiry(FAN)
    harness.run_jobs()
    return enquiry, harness.quotation(enquiry).quotation_id


def text_of(pdf: bytes) -> str:
    return "".join(page.extract_text() for page in PdfReader(BytesIO(pdf)).pages)


def test_staff_can_preview_any_version_marked_as_not_an_offer(harness: Any) -> None:
    _, quotation = drafted(harness)
    file = harness.documents().preview(harness.context(VIEWER), quotation, 1)
    assert file.filename.endswith("-v1-preview.pdf")
    assert "PREVIEW - NOT AN OFFER" in text_of(file.data)
    with pytest.raises(QuotationNotFound):
        harness.documents().preview(harness.context(VIEWER), quotation, 2)
    with pytest.raises(PermissionDenied):  # API clients never see drafts
        harness.documents().preview(intake_client(harness), quotation, 1)


def test_nothing_is_offered_before_approval_and_delivery(harness: Any) -> None:
    enquiry, quotation = drafted(harness)
    documents = harness.documents()
    with pytest.raises(DocumentNotAvailable):
        documents.approved(harness.context(MANAGER), quotation)
    with pytest.raises(DocumentNotAvailable):
        documents.offered(intake_client(harness), enquiry, "api")
    overview = documents.overview(harness.context(MANAGER), quotation)
    assert overview.document is None and overview.deliveries == []


def test_after_delivery_the_customer_gets_the_approved_file_and_downloads_are_tracked(
    harness: Any,
) -> None:
    enquiry, quotation = drafted(harness)
    harness.approvals.decide(MANAGER, harness.command(harness.pending_task(enquiry)))
    harness.run_jobs()  # resume -> queue delivery -> render and make available

    documents = harness.documents()
    staff = documents.approved(harness.context(VIEWER), quotation)
    assert "PREVIEW" not in text_of(staff.data)
    offer = documents.offered(intake_client(harness), enquiry, "api")
    approved_version = harness.db.versions[-1].view
    assert offer.document.version == 1 and offer.content == approved_version.content
    assert offer.number == f"Q-{quotation.hex[:8].upper()}"

    first = documents.download(intake_client(harness), enquiry, "api")
    assert first.data == staff.data and first.filename == offer.filename
    first_seen = harness.db.delivery_rows[offer.delivery.delivery_id][1].first_downloaded_at
    assert first_seen == harness.clock()
    later = harness.documents()
    later._clock = lambda: harness.clock() + timedelta(hours=1)
    later.download(intake_client(harness), enquiry, "api")
    assert harness.db.delivery_rows[offer.delivery.delivery_id][1].first_downloaded_at == first_seen

    overview = documents.overview(harness.context(MANAGER), quotation)
    assert overview.document is not None and len(overview.deliveries) == 1


def test_other_tenants_and_channels_see_nothing(harness: Any, office_tenant: UUID) -> None:
    enquiry, quotation = drafted(harness)
    harness.approvals.decide(MANAGER, harness.command(harness.pending_task(enquiry)))
    harness.run_jobs()
    documents = harness.documents()
    with pytest.raises(DocumentNotAvailable):
        documents.offered(intake_client(harness, office_tenant), enquiry, "api")
    with pytest.raises(DocumentNotAvailable):
        documents.offered(intake_client(harness), enquiry, "email")
    with pytest.raises(DocumentNotAvailable):
        documents.offered(intake_client(harness), uuid4(), "api")


def test_a_changed_stored_file_is_never_served(harness: Any) -> None:
    enquiry, quotation = drafted(harness)
    harness.approvals.decide(MANAGER, harness.command(harness.pending_task(enquiry)))
    harness.run_jobs()
    [(_, document)] = harness.db.documents.values()
    harness.storage.put(document.storage_key, b"%PDF-1.4 swapped")
    with pytest.raises(DocumentIntegrityError):
        harness.documents().approved(harness.context(MANAGER), quotation)
    with pytest.raises(DocumentIntegrityError):
        harness.documents().download(intake_client(harness), enquiry, "api")


def test_the_logo_is_added_only_when_stored_and_unchanged(
    memory_storage: Any, make_image: Any
) -> None:
    import hashlib
    from dataclasses import replace as copy

    from sales_platform.application.documents import with_logo
    from sales_platform.domain.documents import DocumentSource, SellerDetails

    png = make_image("PNG", 40, 20)
    digest = hashlib.sha256(png).hexdigest()
    source = DocumentSource(
        "Demo", uuid4(), 1, {}, "c" * 64, None, None,
        seller=SellerDetails("Demo Pvt Ltd", ("1 Road",)),
        seller_logo_ref=("tenants/t/profile/logo.png", digest),
    )  # fmt: skip
    assert with_logo(memory_storage, source).seller.logo_png is None  # type: ignore[union-attr]
    memory_storage.put("tenants/t/profile/logo.png", png)
    assert with_logo(memory_storage, source).seller.logo_png == png  # type: ignore[union-attr]
    changed = copy(source, seller_logo_ref=("tenants/t/profile/logo.png", "0" * 64))
    assert with_logo(memory_storage, changed).seller.logo_png is None  # type: ignore[union-attr]
    plain = copy(source, seller=None)
    assert with_logo(memory_storage, plain) is plain
