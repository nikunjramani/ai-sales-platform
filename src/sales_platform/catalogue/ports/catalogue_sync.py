"""Storing a product source's catalogue copy and the record of each sync run (phase 6).

Rules for implementations:
- apply stores a whole feed in one transaction: products created, updated, restored or
  archived (no longer offered), prices and stock counts added only when they changed, a change
  log row per product change, and the run marked succeeded. Either all of it or nothing.
- Prices are copied only in the business currency; other currencies are an issue, never a
  conversion.
- finish and apply queue the next scheduled run in the same transaction when next_run_at is
  given, so a crash can never end the schedule.
- Every call is scoped to one business.
"""

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol
from uuid import UUID

from sales_platform.catalogue.domain.catalogue_sync import (
    CatalogueFeed,
    FeedIssue,
    SyncCounts,
    SyncRun,
    SyncTrigger,
)


class CatalogueSyncStore(Protocol):
    def start(self, tenant_id: UUID, connection_id: UUID, trigger: SyncTrigger) -> UUID:
        """Record a running run (an older run left running is marked interrupted)."""
        ...

    def apply(
        self,
        tenant_id: UUID,
        run_id: UUID,
        feed: CatalogueFeed,
        actor: str,
        next_run_at: datetime | None,
    ) -> SyncCounts: ...

    def fail(
        self,
        tenant_id: UUID,
        run_id: UUID,
        error: str,
        issues: Sequence[FeedIssue],
        next_run_at: datetime | None,
    ) -> None:
        """The run failed; the last good copy stays as it is."""
        ...

    def latest(self, tenant_id: UUID, connection_id: UUID) -> SyncRun | None: ...

    def request(self, tenant_id: UUID, connection_id: UUID, actor: str) -> bool:
        """Queue one manual run now; False when one is already waiting."""
        ...
