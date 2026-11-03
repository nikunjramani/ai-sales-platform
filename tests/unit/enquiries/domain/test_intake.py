from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from sales_platform.enquiries.domain.intake import (
    MAX_ATTACHMENT_BYTES,
    MAX_ATTACHMENTS,
    MAX_TEXT_CHARACTERS,
    AttachmentUpload,
    EnquirySubmission,
    InboundEnvelope,
    validate_idempotency_key,
)
from sales_platform.integrations.domain.events import (
    ConnectionUpdate,
    ConsentChange,
    CustomerAction,
    CustomerMessage,
    DeliveryStatusUpdate,
    InboundEvent,
    InboundEventType,
    InboundFile,
    ManagerAction,
    ManagerReply,
    OrderStatusRequest,
    event_from_json,
    event_to_json,
    event_type,
)


def upload(name: str = "spec.txt", data: bytes = b"50 fans") -> AttachmentUpload:
    return AttachmentUpload(name, "text/plain", data)


def test_fingerprint_is_stable_and_detects_any_change() -> None:
    base = EnquirySubmission("Quote 50 fans", "CUST-1", (upload(),))
    assert (
        base.fingerprint()
        == EnquirySubmission("Quote 50 fans", "CUST-1", (upload(),)).fingerprint()
    )
    changed = [
        EnquirySubmission("Quote 51 fans", "CUST-1", (upload(),)),
        EnquirySubmission("Quote 50 fans", "CUST-2", (upload(),)),
        EnquirySubmission("Quote 50 fans", "CUST-1", (upload(data=b"other"),)),
        EnquirySubmission("Quote 50 fans", "CUST-1", ()),
    ]
    assert all(item.fingerprint() != base.fingerprint() for item in changed)


@pytest.mark.parametrize(
    "build",
    [
        lambda: EnquirySubmission("   ", None),
        lambda: EnquirySubmission("x" * (MAX_TEXT_CHARACTERS + 1), None),
        lambda: EnquirySubmission("ok", None, tuple(upload() for _ in range(MAX_ATTACHMENTS + 1))),
        lambda: EnquirySubmission("ok", ""),
        lambda: upload(data=b""),
        lambda: upload(data=b"x" * (MAX_ATTACHMENT_BYTES + 1)),
        lambda: AttachmentUpload("", "text/plain", b"x"),
    ],
)
def test_limits_are_enforced(build: object) -> None:
    with pytest.raises(ValueError):
        build()  # type: ignore[operator]


def test_total_attachment_size_is_limited() -> None:
    big = b"x" * MAX_ATTACHMENT_BYTES
    with pytest.raises(ValueError, match="limited"):
        EnquirySubmission(
            "ok", None, (upload(data=big), upload(data=big), upload(data=big), upload(data=b"x"))
        )


@pytest.mark.parametrize("key", ["short", "x" * 201, "has\nnewline"])
def test_idempotency_key_rules(key: str) -> None:
    with pytest.raises(ValueError, match="Idempotency-Key"):
        validate_idempotency_key(key)
    assert validate_idempotency_key("order-2026-0001") == "order-2026-0001"


def test_envelope_serializes_without_attachment_bytes() -> None:
    envelope = InboundEnvelope(
        event_id=uuid4(),
        tenant_id=uuid4(),
        channel="api",
        connection_id="api-key:1",
        connection_ref=UUID("00000000-0000-4000-8000-000000000001"),
        reply_to="api-key:1",
        provider_event_id="request-0001",
        sender_external_id="CUST-1",
        received_at=datetime(2026, 10, 22, tzinfo=UTC),
        text="Quote 50 fans",
        attachments=(),
        correlation_id=uuid4(),
    )
    data = envelope.to_json()
    assert data["schema_version"] == 2
    assert (data["connection_ref"], data["reply_to"]) == (
        "00000000-0000-4000-8000-000000000001",
        "api-key:1",
    )
    assert data["channel"] == "api"
    assert data["received_at"] == "2026-10-22T00:00:00+00:00"
    with pytest.raises(ValueError, match="timezone"):
        InboundEnvelope(**{**envelope.__dict__, "received_at": datetime(2026, 10, 22)})


def test_every_inbound_event_has_a_type() -> None:
    events: list[InboundEvent] = [
        CustomerMessage("evt-1", "+911234", "2 fans"),
        ManagerAction("evt-2", "manager-phone", "decision", action="approve"),
        DeliveryStatusUpdate("evt-3", "msg-1", "delivered"),
        ConsentChange("evt-4", "+911234", opted_in=False),
        ManagerReply("evt-5", "manager-phone", "ok", "msg-9"),
        CustomerAction("evt-6", "+911234", "ref-1", "accept"),
        ConnectionUpdate("evt-7", "link", {"owner": "42"}),
        OrderStatusRequest("evt-8", "+911234", "O-1A2B3C4D"),
    ]
    assert sorted(event_type(event) for event in events) == sorted(InboundEventType)


def test_events_survive_their_stored_form() -> None:
    events: list[InboundEvent] = [
        CustomerMessage(
            "evt-1",
            "42",
            "see the list",
            occurred_at=datetime(2026, 10, 29, tzinfo=UTC),
            files=(InboundFile("file-1", "list.pdf", "application/pdf", 1200),),
            skipped=("voice message",),
            from_business=True,
        ),
        ManagerReply("evt-2", "7", "approved", "m-1", datetime(2026, 10, 29, tzinfo=UTC)),
        CustomerAction("evt-3", "42", "ref-1", "decline"),
        ConnectionUpdate("evt-4", "link", {"owner": "42"}),
        ManagerAction("evt-5", "7", "confirm", reference="tok", action="yes"),
    ]
    for event in events:
        assert event_from_json(event_to_json(event)) == event
