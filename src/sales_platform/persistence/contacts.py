"""Staff reads of conversations and contacts, and the manual merge (API role, phase 4)."""

from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import Engine, text

from sales_platform.persistence.tenancy import tenant_transaction
from sales_platform.ports.contacts import (
    ContactDetail,
    ContactEnquiry,
    ContactSummary,
    ConversationSummary,
    IdentityView,
    ThreadEntry,
)

_SUMMARY = """
SELECT c.id AS conversation_id, c.contact_id, ct.display_name AS contact_name,
       cn.provider_key, cn.display_name AS connection_name,
       COALESCE(last.text, '') AS last_text,
       COALESCE(last.direction, 'inbound') AS last_direction,
       COALESCE(last.at, c.last_message_at) AS last_at,
       e.id AS enquiry_id, e.status AS enquiry_status,
       q.id AS quotation_id, q.status AS quotation_status
FROM conversations c
JOIN contacts ct ON ct.id = c.contact_id
JOIN connections cn ON cn.id = c.connection_ref
LEFT JOIN LATERAL (
    SELECT m.text, m.direction, m.at FROM conversation_messages m
    WHERE m.conversation_id = c.id ORDER BY m.seq DESC LIMIT 1
) last ON true
LEFT JOIN LATERAL (
    SELECT id, status FROM enquiries WHERE conversation_id = c.id
    ORDER BY received_at DESC LIMIT 1
) e ON true
LEFT JOIN quotations q ON q.enquiry_id = e.id
"""

_THREAD = """
SELECT m.seq, m.direction, m.author, m.text, m.at, m.enquiry_id, m.template_key,
       o.status AS outbound_status, o.last_error AS outbound_error,
       m.outbound_message_id, o.purpose AS outbound_purpose,
       EXISTS (SELECT 1 FROM outbound_messages r WHERE r.resent_from_id = o.id) AS resent
FROM conversation_messages m
LEFT JOIN outbound_messages o ON o.id = m.outbound_message_id
"""

_CONTACT = """
SELECT ct.id AS contact_id, ct.display_name AS name, ct.company, ct.email, ct.phone,
       ct.email_verified_at IS NOT NULL AS email_verified,
       ct.phone_verified_at IS NOT NULL AS phone_verified,
       (SELECT count(*) FROM contact_identities i WHERE i.contact_id = ct.id) AS identities,
       (SELECT max(c.last_message_at) FROM conversations c WHERE c.contact_id = ct.id)
           AS last_activity
FROM contacts ct
"""

_IDENTITY = """
SELECT i.id AS identity_id, cn.provider_key, cn.display_name AS connection_name,
       i.external_id, i.verified, i.consent_status AS consent
FROM contact_identities i JOIN connections cn ON cn.id = i.connection_ref
"""


def _summary(row: Any) -> ConversationSummary:
    return ConversationSummary(**row._mapping)


def _entry(row: Any) -> ThreadEntry:
    return ThreadEntry(**row._mapping)


def _contact(row: Any) -> ContactSummary:
    return ContactSummary(**row._mapping)


def _identity(row: Any) -> IdentityView:
    return IdentityView(**row._mapping)


class SqlContactReader:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def conversations(self, tenant_id: UUID, limit: int) -> list[ConversationSummary]:
        with tenant_transaction(self._engine, tenant_id) as connection:
            rows = connection.execute(
                # A chat started without any message yet is not shown.
                text(
                    _SUMMARY + " WHERE last.at IS NOT NULL "
                    "ORDER BY c.last_message_at DESC, c.id LIMIT :limit"
                ),
                {"limit": limit},
            ).all()
        return [_summary(row) for row in rows]

    def conversation(
        self, tenant_id: UUID, conversation_id: UUID
    ) -> tuple[ConversationSummary, IdentityView, list[ThreadEntry]] | None:
        with tenant_transaction(self._engine, tenant_id) as connection:
            head = connection.execute(
                text(_SUMMARY + " WHERE c.id = :id"), {"id": conversation_id}
            ).one_or_none()
            if head is None:
                return None
            identity = connection.execute(
                text(
                    _IDENTITY + " WHERE i.id = (SELECT identity_id FROM conversations "
                    "WHERE id = :id)"
                ),
                {"id": conversation_id},
            ).one()
            entries = connection.execute(
                text(_THREAD + " WHERE m.conversation_id = :id ORDER BY m.seq"),
                {"id": conversation_id},
            ).all()
        return _summary(head), _identity(identity), [_entry(row) for row in entries]

    def enquiry_thread(self, tenant_id: UUID, enquiry_id: UUID) -> list[ThreadEntry]:
        with tenant_transaction(self._engine, tenant_id) as connection:
            rows = connection.execute(
                text(_THREAD + " WHERE m.enquiry_id = :id ORDER BY m.seq"), {"id": enquiry_id}
            ).all()
        return [_entry(row) for row in rows]

    def contacts(self, tenant_id: UUID, query: str | None, limit: int) -> list[ContactSummary]:
        where = " WHERE ct.merged_into_id IS NULL"
        values: dict[str, object] = {"limit": limit}
        if query:
            where += (
                " AND (ct.display_name ILIKE :q OR ct.email ILIKE :q OR ct.phone ILIKE :q "
                "OR ct.company ILIKE :q)"
            )
            escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            values["q"] = f"%{escaped}%"
        with tenant_transaction(self._engine, tenant_id) as connection:
            rows = connection.execute(
                text(
                    _CONTACT + where + " ORDER BY last_activity DESC NULLS LAST, ct.id LIMIT :limit"
                ),
                values,
            ).all()
        return [_contact(row) for row in rows]

    def contact(self, tenant_id: UUID, contact_id: UUID) -> ContactDetail | None:
        with tenant_transaction(self._engine, tenant_id) as connection:
            head = connection.execute(
                text(_CONTACT + " WHERE ct.id = :id AND ct.merged_into_id IS NULL"),
                {"id": contact_id},
            ).one_or_none()
            if head is None:
                return None
            identities = connection.execute(
                text(_IDENTITY + " WHERE i.contact_id = :id ORDER BY i.first_seen_at"),
                {"id": contact_id},
            ).all()
            conversations = connection.execute(
                text(
                    "SELECT id FROM conversations WHERE contact_id = :id "
                    "ORDER BY last_message_at DESC"
                ),
                {"id": contact_id},
            ).scalars()
            enquiries = connection.execute(
                text(
                    "SELECT e.id AS enquiry_id, e.status, e.received_at, q.id AS quotation_id, "
                    "q.status AS quotation_status FROM enquiries e "
                    "LEFT JOIN quotations q ON q.enquiry_id = e.id "
                    "WHERE e.contact_id = :id ORDER BY e.received_at DESC"
                ),
                {"id": contact_id},
            ).all()
            # D13: suggest only on a *verified* email or phone shared with another contact.
            suggestions = connection.execute(
                text(
                    _CONTACT + " WHERE ct.id <> :id AND ct.merged_into_id IS NULL AND ("
                    "(ct.email_verified_at IS NOT NULL AND :email_verified "
                    " AND lower(ct.email) = lower(:email)) OR "
                    "(ct.phone_verified_at IS NOT NULL AND :phone_verified "
                    " AND ct.phone = :phone)) LIMIT 10"
                ),
                {
                    "id": contact_id,
                    "email": head.email or "",
                    "phone": head.phone or "",
                    "email_verified": head.email_verified,
                    "phone_verified": head.phone_verified,
                },
            ).all()
            conversation_ids = tuple(conversations)
        return ContactDetail(
            _contact(head),
            tuple(_identity(row) for row in identities),
            conversation_ids,
            tuple(ContactEnquiry(**row._mapping) for row in enquiries),
            tuple(_contact(row) for row in suggestions),
        )


class SqlContactMerger:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def merge(self, tenant_id: UUID, source_id: UUID, target_id: UUID, actor: str) -> bool:
        if source_id == target_id:
            return False
        with tenant_transaction(self._engine, tenant_id) as connection:
            found = connection.execute(
                text(
                    "SELECT id FROM contacts WHERE id IN (:s, :t) AND merged_into_id IS NULL "
                    "ORDER BY id FOR UPDATE"
                ),
                {"s": source_id, "t": target_id},
            ).all()
            if len(found) != 2:
                return False
            values = {"s": source_id, "t": target_id}
            for table in ("contact_identities", "conversations", "enquiries"):
                connection.execute(
                    text(f"UPDATE {table} SET contact_id = :t WHERE contact_id = :s"), values
                )
            connection.execute(
                text(
                    "UPDATE contacts SET merged_into_id = :t, merged_at = now(), "
                    "merged_by = :a, updated_at = now() WHERE id = :s"
                ),
                {**values, "a": actor},
            )
            connection.execute(
                text(
                    "INSERT INTO admin_events (id, tenant_id, actor_subject, action, target, "
                    "details) VALUES (:id, :tenant, :a, 'contacts_merged', :s, "
                    "jsonb_build_object('into', CAST(:t_text AS text)))"
                ),
                {
                    "id": uuid4(),
                    "tenant": tenant_id,
                    "a": actor,
                    "s": str(source_id),
                    "t_text": str(target_id),
                },
            )
        return True


class SqlCustomerMessageSettings:
    """tenants.acknowledge_enquiries (D14), changed by administrators with an audit row."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def acknowledge_enquiries(self, tenant_id: UUID) -> bool:
        with tenant_transaction(self._engine, tenant_id) as connection:
            return bool(
                connection.execute(
                    text("SELECT acknowledge_enquiries FROM tenants WHERE id = :t"),
                    {"t": tenant_id},
                ).scalar_one()
            )

    def set_acknowledge_enquiries(self, tenant_id: UUID, value: bool, actor: str) -> None:
        with tenant_transaction(self._engine, tenant_id) as connection:
            connection.execute(
                text("UPDATE tenants SET acknowledge_enquiries = :v WHERE id = :t"),
                {"v": value, "t": tenant_id},
            )
            connection.execute(
                text(
                    "INSERT INTO admin_events (id, tenant_id, actor_subject, action, details) "
                    "VALUES (:id, :t, :a, 'customer_messages_changed', "
                    "jsonb_build_object('acknowledge_enquiries', CAST(:v AS boolean)))"
                ),
                {"id": uuid4(), "t": tenant_id, "a": actor, "v": value},
            )
