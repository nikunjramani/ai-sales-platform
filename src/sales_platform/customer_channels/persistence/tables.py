"""Customer_channels tables, split from platform/persistence/models.py (ADR 0026).

They register on the one Base.metadata that Alembic reads (migrations/env.py imports them).
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    String,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from sales_platform.platform.persistence.models import Base


class CustomerSession(Base):
    """A website chat visitor's session: one connection, identity and conversation."""

    __tablename__ = "customer_sessions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "connection_ref"],
            ["connections.tenant_id", "connections.id"],
            name="fk_customer_sessions_connection",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "identity_id"],
            ["contact_identities.tenant_id", "contact_identities.id"],
            name="fk_customer_sessions_identity",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "conversation_id"],
            ["conversations.tenant_id", "conversations.id"],
            name="fk_customer_sessions_conversation",
        ),
        Index("ix_customer_sessions_connection", "tenant_id", "connection_ref"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"))
    connection_ref: Mapped[UUID] = mapped_column()
    identity_id: Mapped[UUID] = mapped_column()
    conversation_id: Mapped[UUID] = mapped_column()
    secret_sha256: Mapped[str] = mapped_column(String(64))
    ip_hmac: Mapped[str] = mapped_column(String(64))
    origin: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
