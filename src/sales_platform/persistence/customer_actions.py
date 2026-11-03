"""customer_action_refs rows (phase 4). The worker issues them; the API reads and consumes."""

from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import Engine, Row, select, text, update
from sqlalchemy.dialects.postgresql import insert

from sales_platform.persistence.models import CustomerActionRef as RefRow
from sales_platform.persistence.tenancy import tenant_transaction
from sales_platform.ports.customer_actions import CustomerActionRef, NewActionRef

_COLUMNS = (
    RefRow.id,
    RefRow.tenant_id,
    RefRow.quotation_id,
    RefRow.version,
    RefRow.content_sha256,
    RefRow.identity_id,
    RefRow.conversation_id,
    RefRow.expires_at,
    RefRow.consumed_at,
)


def _ref(row: Row[Any] | None) -> CustomerActionRef | None:
    return None if row is None else CustomerActionRef(*row)


class SqlCustomerActionStore:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def issue(self, new: NewActionRef) -> UUID:
        with tenant_transaction(self._engine, new.tenant_id) as connection:
            connection.execute(
                insert(RefRow)
                .values(
                    id=uuid4(),
                    tenant_id=new.tenant_id,
                    quotation_id=new.quotation_id,
                    version=new.version,
                    content_sha256=new.content_sha256,
                    identity_id=new.identity_id,
                    conversation_id=new.conversation_id,
                    outbound_message_id=new.outbound_message_id,
                    token_sha256=new.token_sha256,
                    expires_at=new.expires_at,
                )
                .on_conflict_do_nothing(constraint="uq_customer_action_refs_offer")
            )
            ref_id: UUID = connection.execute(
                select(RefRow.id).where(
                    RefRow.tenant_id == new.tenant_id,
                    RefRow.quotation_id == new.quotation_id,
                    RefRow.version == new.version,
                    RefRow.identity_id == new.identity_id,
                )
            ).scalar_one()
            connection.execute(
                update(RefRow)
                .where(RefRow.tenant_id == new.tenant_id, RefRow.id == ref_id)
                .values(token_sha256=new.token_sha256, outbound_message_id=new.outbound_message_id)
            )
        return ref_id

    def get(self, tenant_id: UUID, ref_id: UUID) -> CustomerActionRef | None:
        with tenant_transaction(self._engine, tenant_id) as connection:
            return _ref(
                connection.execute(
                    select(*_COLUMNS).where(RefRow.tenant_id == tenant_id, RefRow.id == ref_id)
                ).one_or_none()
            )

    def find_by_token(self, token_sha256: str) -> CustomerActionRef | None:
        with self._engine.begin() as connection:
            # The customer_action_lookup policy shows only the row with this token hash.
            connection.execute(
                text("SELECT set_config('app.action_token_sha256', :t, true)"),
                {"t": token_sha256},
            )
            return _ref(
                connection.execute(
                    select(*_COLUMNS).where(RefRow.token_sha256 == token_sha256)
                ).one_or_none()
            )

    def for_message(self, tenant_id: UUID, outbound_message_id: UUID) -> CustomerActionRef | None:
        with tenant_transaction(self._engine, tenant_id) as connection:
            return _ref(
                connection.execute(
                    select(*_COLUMNS).where(
                        RefRow.tenant_id == tenant_id,
                        RefRow.outbound_message_id == outbound_message_id,
                    )
                ).one_or_none()
            )
