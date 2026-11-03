"""Public PDF link rows (phase 5): issued by the worker, found by token hash by the API."""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import Engine, select, text
from sqlalchemy.dialects.postgresql import insert

from sales_platform.persistence.models import MediaLink
from sales_platform.persistence.tenancy import tenant_transaction
from sales_platform.ports.media import MediaLinkTarget


class SqlMediaLinks:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def issue(
        self,
        tenant_id: UUID,
        document_id: UUID,
        content_sha256: str,
        outbound_message_id: UUID,
        token_sha256: str,
        expires_at: datetime,
    ) -> None:
        with tenant_transaction(self._engine, tenant_id) as connection:
            connection.execute(
                insert(MediaLink).values(
                    id=uuid4(),
                    tenant_id=tenant_id,
                    token_sha256=token_sha256,
                    document_id=document_id,
                    content_sha256=content_sha256,
                    outbound_message_id=outbound_message_id,
                    expires_at=expires_at,
                )
            )

    def find(self, token_sha256: str) -> MediaLinkTarget | None:
        with self._engine.begin() as connection:
            connection.execute(
                text("SELECT set_config('app.media_token_sha256', :h, true)"), {"h": token_sha256}
            )
            row = connection.execute(
                select(
                    MediaLink.tenant_id,
                    MediaLink.document_id,
                    MediaLink.content_sha256,
                    MediaLink.expires_at,
                ).where(MediaLink.token_sha256 == token_sha256)
            ).one_or_none()
        if row is None:
            return None
        return MediaLinkTarget(row.tenant_id, row.document_id, row.content_sha256, row.expires_at)
