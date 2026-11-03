"""A product source's catalogue copied into our catalogue tables (phase 6, ADR 0023).

SqlCatalogueSync runs as the worker role. apply() stores one whole feed in one transaction,
under the business's catalogue lock (the same advisory lock the product screens take for the
active-product limit), so a sync and a staff edit never interleave:
- a product is matched by SKU, ignoring case. New: created with source 'sync'. Changed (name,
  unit, category, quantities) or archived or typed in before: updated, restored or adopted,
  with a change record. Offered no more: archived, with a change record;
- a category is matched by name, ignoring case, and created or restored when needed;
- a price is added (revision + 1, effective now) only when the price in force differs, and
  only in the business currency; a stock count only when the number or unit differs;
- the run is marked succeeded with its counts and issues, and the next scheduled run queued.
"""

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import Connection, Engine, func, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError

from sales_platform.domain.catalogue_admin import (
    LOCAL_PRICE_LIST,
    EntrySource,
    PriceEntry,
    effective_price,
    field_changes,
)
from sales_platform.domain.catalogue_search import product_terms
from sales_platform.domain.catalogue_sync import (
    CatalogueFeed,
    FeedIssue,
    FeedProduct,
    SyncCounts,
    SyncRun,
    SyncStatus,
    SyncTrigger,
    keep_issues,
)
from sales_platform.domain.jobs import JobKind
from sales_platform.persistence.jobs import queue_job
from sales_platform.persistence.models import (
    CatalogueCategory,
    CatalogueChange,
    CataloguePriceRow,
    CatalogueProductRow,
    CatalogueStockRow,
    CatalogueSyncRun,
    TenantPricingPolicy,
)
from sales_platform.persistence.tenancy import tenant_transaction

_P = CatalogueProductRow
_C = CatalogueCategory
_PRICE = CataloguePriceRow
_STOCK = CatalogueStockRow
_RUN = CatalogueSyncRun
_SYNC = EntrySource.SYNC.value


def sync_payload(connection_id: UUID, trigger: SyncTrigger) -> dict[str, str]:
    return {"connection_id": str(connection_id), "trigger": trigger.value}


def _issues_json(issues: Sequence[FeedIssue]) -> list[dict[str, object]]:
    return [
        {"code": item.code, "message": item.message[:300], "sku": item.sku}
        for item in keep_issues(issues)
    ]


def _counts_json(counts: SyncCounts) -> dict[str, object]:
    return dict(vars(counts))


class SqlCatalogueSync:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def start(self, tenant_id: UUID, connection_id: UUID, trigger: SyncTrigger) -> UUID:
        run_id = uuid4()
        with tenant_transaction(self._engine, tenant_id) as connection:
            # A worker that died mid-run left its run "running": it will never finish.
            connection.execute(
                update(_RUN)
                .where(
                    _RUN.tenant_id == tenant_id,
                    _RUN.connection_id == connection_id,
                    _RUN.status == SyncStatus.RUNNING.value,
                )
                .values(
                    status=SyncStatus.FAILED.value,
                    finished_at=func.now(),
                    error="Interrupted (the worker stopped during the run)",
                )
            )
            connection.execute(
                insert(_RUN).values(
                    id=run_id,
                    tenant_id=tenant_id,
                    connection_id=connection_id,
                    trigger=trigger.value,
                    status=SyncStatus.RUNNING.value,
                )
            )
        return run_id

    def apply(
        self,
        tenant_id: UUID,
        run_id: UUID,
        feed: CatalogueFeed,
        actor: str,
        next_run_at: datetime | None,
    ) -> SyncCounts:
        issues = list(feed.issues)
        with tenant_transaction(self._engine, tenant_id) as connection:
            connection.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": f"catalogue-limit:{tenant_id}"},
            )
            now = connection.execute(select(func.now())).scalar_one()
            currency = connection.execute(
                select(TenantPricingPolicy.currency).where(
                    TenantPricingPolicy.tenant_id == tenant_id
                )
            ).scalar_one_or_none()
            writer = _Writer(connection, tenant_id, actor, now)
            ids = writer.products(feed.products)
            prices_added = 0
            mismatched = sorted({p.currency for p in feed.prices if p.currency != currency})
            if mismatched:
                issues.append(
                    FeedIssue(
                        "currency_mismatch",
                        f"Prices in {', '.join(mismatched)} are not copied: the business "
                        f"currency is {currency or 'not set yet'}",
                    )
                )
            for price in feed.prices:
                if price.currency == currency and price.sku.casefold() in ids:
                    prices_added += writer.price(
                        ids[price.sku.casefold()], price.unit_price, price.currency, price.unit
                    )
            stock_added = sum(
                writer.stock(
                    ids[item.sku.casefold()], item.quantity, item.unit, item.as_of, feed.location
                )
                for item in feed.stock
                if item.sku.casefold() in ids
            )
            counts = SyncCounts(
                created=writer.created,
                updated=writer.updated,
                archived=writer.archived,
                unchanged=writer.unchanged,
                prices_added=prices_added,
                stock_added=stock_added,
                issues=len(issues),
            )
            self._finish(
                connection,
                tenant_id,
                run_id,
                SyncStatus.SUCCEEDED,
                counts,
                issues,
                None,
                next_run_at,
            )
        return counts

    def fail(
        self,
        tenant_id: UUID,
        run_id: UUID,
        error: str,
        issues: Sequence[FeedIssue],
        next_run_at: datetime | None,
    ) -> None:
        with tenant_transaction(self._engine, tenant_id) as connection:
            self._finish(
                connection, tenant_id, run_id, SyncStatus.FAILED, None, issues, error, next_run_at
            )

    def latest(self, tenant_id: UUID, connection_id: UUID) -> SyncRun | None:
        with tenant_transaction(self._engine, tenant_id) as connection:
            row = connection.execute(
                select(_RUN)
                .where(_RUN.tenant_id == tenant_id, _RUN.connection_id == connection_id)
                .order_by(_RUN.started_at.desc())
                .limit(1)
            ).one_or_none()
        if row is None:
            return None
        counts = row.counts
        return SyncRun(
            run_id=row.id,
            connection_id=row.connection_id,
            trigger=SyncTrigger(row.trigger),
            status=SyncStatus(row.status),
            started_at=row.started_at,
            finished_at=row.finished_at,
            counts=None if counts is None else SyncCounts(**{k: int(v) for k, v in counts.items()}),
            issues=tuple(
                FeedIssue(str(item["code"]), str(item["message"]), item.get("sku"))
                for item in row.issues
            ),
            error=row.error,
            next_run_at=row.next_run_at,
        )

    def request(self, tenant_id: UUID, connection_id: UUID, actor: str) -> bool:
        """The API role only inserts jobs; uq_jobs_manual_sync refuses a second waiting one."""
        payload = sync_payload(connection_id, SyncTrigger.MANUAL)
        try:
            with tenant_transaction(self._engine, tenant_id) as connection:
                queue_job(connection, tenant_id, JobKind.SYNC_CATALOGUE, payload, uuid4())
        except IntegrityError as error:
            if "uq_jobs_manual_sync" not in str(error.orig):
                raise
            return False
        return True

    @staticmethod
    def _finish(
        connection: Connection,
        tenant_id: UUID,
        run_id: UUID,
        status: SyncStatus,
        counts: SyncCounts | None,
        issues: Sequence[FeedIssue],
        error: str | None,
        next_run_at: datetime | None,
    ) -> None:
        connection.execute(
            update(_RUN)
            .where(_RUN.tenant_id == tenant_id, _RUN.id == run_id)
            .values(
                status=status.value,
                finished_at=func.now(),
                counts=None if counts is None else _counts_json(counts),
                issues=_issues_json(issues),
                error=None if error is None else error[:500],
                next_run_at=next_run_at,
            )
        )
        if next_run_at is not None:
            connection_id = connection.execute(
                select(_RUN.connection_id).where(_RUN.tenant_id == tenant_id, _RUN.id == run_id)
            ).scalar_one()
            queue_job(
                connection,
                tenant_id,
                JobKind.SYNC_CATALOGUE,
                sync_payload(connection_id, SyncTrigger.SCHEDULE),
                uuid4(),
                run_at=next_run_at,
            )


class _Writer:
    """The row changes of one apply(), all in the caller's transaction."""

    def __init__(self, connection: Connection, tenant_id: UUID, actor: str, now: datetime) -> None:
        self._db = connection
        self._tenant = tenant_id
        self._actor = actor
        self._now = now
        self.created = self.updated = self.archived = self.unchanged = 0
        self._categories: dict[str, tuple[UUID, datetime | None]] = {
            row.name.casefold(): (row.id, row.archived_at)
            for row in connection.execute(
                select(_C.id, _C.name, _C.archived_at).where(_C.tenant_id == tenant_id)
            )
        }

    def products(self, offered: Sequence[FeedProduct]) -> dict[str, UUID]:
        """Create, update, restore or archive; returns product IDs by SKU (lower case)."""
        existing = {
            row.sku.casefold(): row
            for row in self._db.execute(select(_P).where(_P.tenant_id == self._tenant))
        }
        ids: dict[str, UUID] = {}
        for product in offered:
            key = product.sku.casefold()
            category_id = self._category(product.category)
            row = existing.get(key)
            if row is None:
                ids[key] = self._create(product, category_id)
            else:
                ids[key] = row.id
                self._update(row, product, category_id)
        for key, row in existing.items():
            if key not in ids and row.archived_at is None:
                self._archive(row)
        return ids

    def price(self, product_id: UUID, amount: Any, currency: str, unit: str) -> int:
        rows = self._db.execute(
            select(_PRICE).where(
                _PRICE.tenant_id == self._tenant,
                _PRICE.product_id == product_id,
                _PRICE.price_list == LOCAL_PRICE_LIST,
                _PRICE.currency == currency,
            )
        ).all()
        entries = [
            PriceEntry(
                row.id,
                row.product_id,
                row.price_list,
                row.revision,
                row.currency,
                row.unit,
                row.unit_price,
                row.effective_from,
                EntrySource(row.source),
                row.note,
                row.created_at,
                row.created_by,
            )
            for row in rows
        ]
        current = effective_price(entries, self._now)
        if current is not None and (current.entry.unit_price, current.entry.unit) == (amount, unit):
            return 0
        self._db.execute(
            insert(_PRICE).values(
                id=uuid4(),
                tenant_id=self._tenant,
                product_id=product_id,
                price_list=LOCAL_PRICE_LIST,
                revision=max((entry.revision for entry in entries), default=0) + 1,
                currency=currency,
                unit=unit,
                unit_price=amount,
                effective_from=self._now,
                source=_SYNC,
                note="Copied from the product source",
                command_id=uuid4(),
                created_by=self._actor,
            )
        )
        return 1

    def stock(
        self, product_id: UUID, quantity: Any, unit: str, as_of: datetime, location: str
    ) -> int:
        current = self._db.execute(
            select(_STOCK.quantity, _STOCK.unit)
            .where(
                _STOCK.tenant_id == self._tenant,
                _STOCK.product_id == product_id,
                _STOCK.location_code == location,
            )
            .order_by(_STOCK.as_of.desc(), _STOCK.recorded_at.desc())
            .limit(1)
        ).one_or_none()
        if current is not None and (current.quantity, current.unit) == (quantity, unit):
            return 0
        self._db.execute(
            insert(_STOCK).values(
                id=uuid4(),
                tenant_id=self._tenant,
                product_id=product_id,
                location_code=location,
                quantity=quantity,
                unit=unit,
                as_of=min(as_of, self._now),
                source=_SYNC,
                note="Copied from the product source",
                command_id=uuid4(),
                recorded_by=self._actor,
            )
        )
        return 1

    # --- internals -------------------------------------------------------------------------
    def _category(self, name: str | None) -> UUID | None:
        if name is None:
            return None
        found = self._categories.get(name.casefold())
        if found is None:
            category_id = uuid4()
            self._db.execute(
                insert(_C).values(
                    id=category_id,
                    tenant_id=self._tenant,
                    name=name,
                    created_by=self._actor,
                    updated_by=self._actor,
                )
            )
            self._change("category", category_id, "created", {}, {"name": name})
        else:
            category_id, archived_at = found
            if archived_at is not None:
                self._db.execute(
                    update(_C)
                    .where(_C.tenant_id == self._tenant, _C.id == category_id)
                    .values(
                        archived_at=None,
                        row_version=_C.row_version + 1,
                        updated_at=func.now(),
                        updated_by=self._actor,
                    )
                )
                self._change("category", category_id, "restored", {}, {})
        self._categories[name.casefold()] = (category_id, None)
        return category_id

    def _values(self, product: FeedProduct, category_id: UUID | None) -> dict[str, Any]:
        return {
            "name": product.name,
            "unit": product.unit,
            "category_id": category_id,
            "minimum_quantity": product.minimum_quantity,
            "quantity_step": product.quantity_step,
            "search_terms": sorted(product_terms(product.sku, product.name, product.category)),
        }

    def _create(self, product: FeedProduct, category_id: UUID | None) -> UUID:
        product_id = uuid4()
        values = self._values(product, category_id)
        self._db.execute(
            insert(_P).values(
                id=product_id,
                tenant_id=self._tenant,
                sku=product.sku,
                source=_SYNC,
                external_id=product.external_id,
                synced_at=self._now,
                created_by=self._actor,
                updated_by=self._actor,
                **values,
            )
        )
        self._change("product", product_id, "created", {}, _shown(values))
        self.created += 1
        return product_id

    def _update(self, row: Any, product: FeedProduct, category_id: UUID | None) -> None:
        values = self._values(product, category_id)
        before = {name: getattr(row, name) for name in values}
        restored = row.archived_at is not None
        adopted = row.source != _SYNC or row.external_id != product.external_id
        if before == values and not restored and not adopted:
            self._db.execute(
                update(_P)
                .where(_P.tenant_id == self._tenant, _P.id == row.id)
                .values(synced_at=self._now)
            )
            self.unchanged += 1
            return
        self._db.execute(
            update(_P)
            .where(_P.tenant_id == self._tenant, _P.id == row.id)
            .values(
                source=_SYNC,
                external_id=product.external_id,
                synced_at=self._now,
                archived_at=None,
                archived_by=None,
                row_version=_P.row_version + 1,
                updated_at=func.now(),
                updated_by=self._actor,
                **values,
            )
        )
        self._change(
            "product", row.id, "restored" if restored else "updated", _shown(before), _shown(values)
        )
        self.updated += 1

    def _archive(self, row: Any) -> None:
        self._db.execute(
            update(_P)
            .where(_P.tenant_id == self._tenant, _P.id == row.id)
            .values(
                archived_at=func.now(),
                archived_by=self._actor,
                row_version=_P.row_version + 1,
                updated_at=func.now(),
                updated_by=self._actor,
            )
        )
        self._change("product", row.id, "archived", {}, {})
        self.archived += 1

    def _change(
        self,
        entity: str,
        entity_id: UUID,
        action: str,
        before: Mapping[str, object],
        after: Mapping[str, object],
    ) -> None:
        self._db.execute(
            insert(CatalogueChange).values(
                id=uuid4(),
                tenant_id=self._tenant,
                entity=entity,
                entity_id=entity_id,
                action=action,
                changes=field_changes(before, after),
                actor=self._actor,
            )
        )


def _shown(values: Mapping[str, Any]) -> dict[str, object]:
    """The change-log view of product values (search words are not a change staff made)."""
    return {name: value for name, value in values.items() if name != "search_terms"}
