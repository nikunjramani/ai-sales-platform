"""Our own product catalogue (phase 3, ADR 0020).

- catalogue_categories, catalogue_products: edited in place with a row_version; products keep
  their search words (domain.catalogue_search) in search_terms, indexed with GIN. SKUs are
  unique per business ignoring case and never change (Q2).
- catalogue_prices, catalogue_stock: history only (reject_change trigger, no UPDATE/DELETE
  grant). A price change or a new count is a new row.
- catalogue_changes: append-only audit of product and category edits.
- pg_trgm indexes on SKU and name for the "contains" search box in the portal.
- Numbers are unconstrained numeric with scale() checks, so 4250.00 stays 4250.00 (quotation
  evidence and the acceptance comparison store prices as text).
- Forced tenant RLS on all five tables. The API role reads and writes (column-level UPDATE on
  products and categories only); the worker only reads.
- create_business gains the default catalogue source (SALES_DEFAULT_CATALOGUE_PROVIDER),
  created with the business. Businesses without an active catalogue connection get one to the
  local catalogue (user answer, 2026-10-28); the demo businesses keep the simulated ERP.

Downgrade loses all catalogue data and removes every local catalogue connection.
"""

from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0011_catalogue"
down_revision = "0010_accounts"
branch_labels = None
depends_on = None

_TENANT_FILTER = "tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid"
_SUBJECT = "NULLIF(current_setting('app.subject', true), '')"
_NOW = sa.text("now()")
_LOCAL_PROVIDER = "local_catalogue"
_LOCAL_NAME = "Our product list"
_MIGRATION_ACTOR = "migration:0011_catalogue"
_TABLES = (
    "catalogue_categories",
    "catalogue_products",
    "catalogue_prices",
    "catalogue_stock",
    "catalogue_changes",
)
_BACKFILL_TABLES = ("tenants", "connections", "connection_events")
_OLD_SIGNATURE = "create_business(uuid, text, text, text, text, numeric, integer)"
_NEW_SIGNATURE = "create_business(uuid, text, text, text, text, numeric, integer, text, text)"


def _audit_columns(*, updated: bool) -> list[sa.Column[Any]]:
    columns: list[sa.Column[Any]] = [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.Column("created_by", sa.String(200), nullable=False),
    ]
    if updated:
        columns += [
            sa.Column(
                "updated_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False
            ),
            sa.Column("updated_by", sa.String(200), nullable=False),
        ]
    return columns


def _scale(column: str, places: int) -> str:
    return f"scale({column}) <= {places}"


def _business_function(signature_extra: str, catalogue: str) -> str:
    """create_business as in 0010, plus `catalogue` (the default product source part)."""
    return f"""
CREATE FUNCTION create_business(
    p_user_id uuid, p_name text, p_country text, p_time_zone text,
    p_currency text, p_tax_rate numeric, p_quote_validity_days integer{signature_extra}
) RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE
    v_subject text := {_SUBJECT};
    v_previous text := current_setting('app.tenant_id', true);
    v_tenant uuid := gen_random_uuid();  -- never a parameter: cannot target an existing business
    v_name text := btrim(p_name);
    v_connection uuid := gen_random_uuid();
BEGIN
    IF v_subject IS NULL OR NOT EXISTS (
        SELECT 1 FROM users WHERE id = p_user_id AND subject = v_subject
        AND email_verified_at IS NOT NULL AND disabled_at IS NULL
    ) THEN
        RAISE EXCEPTION 'not allowed' USING ERRCODE = '42501';
    END IF;
    IF v_name IS NULL OR length(v_name) NOT BETWEEN 1 AND 200 THEN
        RAISE EXCEPTION 'invalid business name' USING ERRCODE = '22023';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_timezone_names WHERE name = p_time_zone) THEN
        RAISE EXCEPTION 'unknown time zone' USING ERRCODE = '22023';
    END IF;
    PERFORM set_config('app.tenant_id', v_tenant::text, true);
    INSERT INTO tenants (id, name, status, country, time_zone, created_by)
    VALUES (v_tenant, v_name, 'setting_up', p_country, p_time_zone, v_subject);
    INSERT INTO memberships (id, tenant_id, subject, role) VALUES
        (gen_random_uuid(), v_tenant, v_subject, 'administrator'),
        (gen_random_uuid(), v_tenant, v_subject, 'approver');
    INSERT INTO tenant_pricing_policies
        (tenant_id, version, currency, tax_rate, quote_validity_days, updated_by)
    VALUES (v_tenant, 1, p_currency, p_tax_rate, p_quote_validity_days, v_subject);
    INSERT INTO tenant_profiles (tenant_id) VALUES (v_tenant);
    INSERT INTO admin_events (id, tenant_id, actor_subject, action, details)
    VALUES (gen_random_uuid(), v_tenant, v_subject, 'business_created',
            jsonb_build_object('name', v_name));{catalogue}
    PERFORM set_config('app.tenant_id', COALESCE(v_previous, ''), true);
    RETURN v_tenant;
END $$
"""


# The provider key and name come from the API's settings; no provider is named here.
_CATALOGUE_PART = """
    IF p_catalogue_provider IS NOT NULL THEN
        IF p_catalogue_provider !~ '^[a-z][a-z0-9_]{1,31}$' THEN
            RAISE EXCEPTION 'invalid provider key' USING ERRCODE = '22023';
        END IF;
        INSERT INTO connections
            (id, tenant_id, provider_key, display_name, purposes, status, created_by)
        VALUES (v_connection, v_tenant, p_catalogue_provider,
                left(COALESCE(NULLIF(btrim(p_catalogue_name), ''), p_catalogue_provider), 100),
                ARRAY['catalogue'], 'active', v_subject);
        INSERT INTO connection_events (id, tenant_id, connection_id, kind, actor, detail)
        VALUES (gen_random_uuid(), v_tenant, v_connection, 'created', v_subject,
                jsonb_build_object('reason', 'default product source'));
    END IF;"""


def _grant_function(signature: str) -> None:
    op.execute(f"ALTER FUNCTION {signature} OWNER TO quotation_owner")
    op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO quotation_app")


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    op.create_table(
        "catalogue_categories",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("row_version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        *_audit_columns(updated=True),
        sa.PrimaryKeyConstraint("id", name="pk_catalogue_categories"),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name="fk_catalogue_categories_tenant_id_tenants"
        ),
        sa.UniqueConstraint("tenant_id", "id", name="uq_catalogue_categories_tenant_id"),
        sa.CheckConstraint("btrim(name) <> ''", name="ck_catalogue_categories_name"),
        sa.CheckConstraint("row_version >= 1", name="ck_catalogue_categories_row_version"),
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_catalogue_categories_name "
        "ON catalogue_categories (tenant_id, lower(name))"
    )

    op.create_table(
        "catalogue_products",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("sku", sa.String(64), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("description", sa.String(2000), nullable=True),
        sa.Column("category_id", sa.Uuid(), nullable=True),
        sa.Column("unit", sa.String(32), nullable=False),
        sa.Column("minimum_quantity", sa.Numeric(), nullable=False),
        sa.Column("quantity_step", sa.Numeric(), nullable=False),
        sa.Column("search_terms", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("source", sa.String(16), server_default=sa.text("'manual'"), nullable=False),
        # For later import/sync (D18); unused while products are typed in.
        sa.Column("external_id", sa.String(200), nullable=True),
        sa.Column("synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("archived_by", sa.String(200), nullable=True),
        sa.Column("row_version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        *_audit_columns(updated=True),
        sa.PrimaryKeyConstraint("id", name="pk_catalogue_products"),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name="fk_catalogue_products_tenant_id_tenants"
        ),
        # A NULL category skips the check; a set one must be this business's.
        sa.ForeignKeyConstraint(
            ["tenant_id", "category_id"],
            ["catalogue_categories.tenant_id", "catalogue_categories.id"],
            name="fk_catalogue_products_category",
        ),
        sa.UniqueConstraint("tenant_id", "id", name="uq_catalogue_products_tenant_id"),
        sa.CheckConstraint(
            "sku ~ '^[A-Za-z0-9][A-Za-z0-9._/-]{0,63}$'", name="ck_catalogue_products_sku"
        ),
        sa.CheckConstraint("btrim(name) <> ''", name="ck_catalogue_products_name"),
        # The list of units lives in the domain (Q3); the database only refuses blanks.
        sa.CheckConstraint("btrim(unit) <> ''", name="ck_catalogue_products_unit"),
        sa.CheckConstraint(
            "minimum_quantity > 0 AND minimum_quantity < 1e9 AND " + _scale("minimum_quantity", 3),
            name="ck_catalogue_products_minimum_quantity",
        ),
        sa.CheckConstraint(
            "quantity_step > 0 AND quantity_step < 1e9 AND " + _scale("quantity_step", 3),
            name="ck_catalogue_products_quantity_step",
        ),
        sa.CheckConstraint("source IN ('manual')", name="ck_catalogue_products_source"),
        sa.CheckConstraint("row_version >= 1", name="ck_catalogue_products_row_version"),
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_catalogue_products_sku "
        "ON catalogue_products (tenant_id, lower(sku))"
    )
    op.create_index(
        "ix_catalogue_products_category", "catalogue_products", ["tenant_id", "category_id"]
    )
    op.create_index(
        "ix_catalogue_products_terms",
        "catalogue_products",
        ["search_terms"],
        postgresql_using="gin",
    )
    op.execute(
        "CREATE INDEX ix_catalogue_products_sku_trgm ON catalogue_products "
        "USING gin (lower(sku) gin_trgm_ops)"
    )
    op.execute(
        "CREATE INDEX ix_catalogue_products_name_trgm ON catalogue_products "
        "USING gin (lower(name) gin_trgm_ops)"
    )

    op.create_table(
        "catalogue_prices",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("product_id", sa.Uuid(), nullable=False),
        sa.Column(
            "price_list", sa.String(64), server_default=sa.text("'standard'"), nullable=False
        ),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("unit", sa.String(32), nullable=False),
        sa.Column("unit_price", sa.Numeric(), nullable=False),
        sa.Column("effective_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source", sa.String(16), server_default=sa.text("'manual'"), nullable=False),
        sa.Column("note", sa.String(500), nullable=True),
        sa.Column("command_id", sa.Uuid(), nullable=False),
        *_audit_columns(updated=False),
        sa.PrimaryKeyConstraint("id", name="pk_catalogue_prices"),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name="fk_catalogue_prices_tenant_id_tenants"
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "product_id"],
            ["catalogue_products.tenant_id", "catalogue_products.id"],
            name="fk_catalogue_prices_product",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "product_id",
            "price_list",
            "currency",
            "revision",
            name="uq_catalogue_prices_revision",
        ),
        sa.UniqueConstraint("tenant_id", "command_id", name="uq_catalogue_prices_command"),
        sa.CheckConstraint("revision >= 1", name="ck_catalogue_prices_revision"),
        sa.CheckConstraint("currency ~ '^[A-Z]{3}$'", name="ck_catalogue_prices_currency"),
        sa.CheckConstraint(
            "unit_price > 0 AND unit_price < 1e12 AND " + _scale("unit_price", 6),
            name="ck_catalogue_prices_unit_price",
        ),
        sa.CheckConstraint("source IN ('manual')", name="ck_catalogue_prices_source"),
    )
    op.execute(
        "CREATE INDEX ix_catalogue_prices_lookup ON catalogue_prices "
        "(tenant_id, product_id, price_list, currency, effective_from DESC, revision DESC)"
    )

    op.create_table(
        "catalogue_stock",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("product_id", sa.Uuid(), nullable=False),
        sa.Column("location_code", sa.String(32), server_default=sa.text("'MAIN'"), nullable=False),
        sa.Column("quantity", sa.Numeric(), nullable=True),  # NULL: "not tracked"
        sa.Column("unit", sa.String(32), nullable=False),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source", sa.String(16), server_default=sa.text("'manual'"), nullable=False),
        sa.Column("note", sa.String(500), nullable=True),
        sa.Column("command_id", sa.Uuid(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.Column("recorded_by", sa.String(200), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_catalogue_stock"),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name="fk_catalogue_stock_tenant_id_tenants"
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "product_id"],
            ["catalogue_products.tenant_id", "catalogue_products.id"],
            name="fk_catalogue_stock_product",
        ),
        sa.UniqueConstraint("tenant_id", "command_id", name="uq_catalogue_stock_command"),
        sa.CheckConstraint(
            "quantity IS NULL OR (quantity >= 0 AND quantity < 1e12 AND "
            + _scale("quantity", 3)
            + ")",
            name="ck_catalogue_stock_quantity",
        ),
        sa.CheckConstraint(
            "as_of <= recorded_at + interval '5 minutes'", name="ck_catalogue_stock_as_of"
        ),
        sa.CheckConstraint("source IN ('manual')", name="ck_catalogue_stock_source"),
    )
    op.execute(
        "CREATE INDEX ix_catalogue_stock_current ON catalogue_stock "
        "(tenant_id, product_id, location_code, as_of DESC, recorded_at DESC)"
    )

    op.create_table(
        "catalogue_changes",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("entity", sa.String(16), nullable=False),
        # No foreign key: the record outlives the deletion of an unused product.
        sa.Column("entity_id", sa.Uuid(), nullable=False),
        sa.Column("action", sa.String(16), nullable=False),
        sa.Column("changes", postgresql.JSONB(), nullable=False),
        sa.Column("actor", sa.String(200), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_catalogue_changes"),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name="fk_catalogue_changes_tenant_id_tenants"
        ),
        sa.CheckConstraint("entity IN ('product', 'category')", name="ck_catalogue_changes_entity"),
        sa.CheckConstraint(
            "action IN ('created', 'updated', 'archived', 'restored', 'deleted')",
            name="ck_catalogue_changes_action",
        ),
    )
    op.create_index(
        "ix_catalogue_changes_entity",
        "catalogue_changes",
        ["tenant_id", "entity_id", "created_at"],
    )

    for table in _TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tenant_isolation ON {table} "
            f"USING ({_TENANT_FILTER}) WITH CHECK ({_TENANT_FILTER})"
        )
    for table in ("catalogue_prices", "catalogue_stock", "catalogue_changes"):
        op.execute(
            f"CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION reject_change()"
        )

    op.execute(f"GRANT SELECT, INSERT ON {', '.join(_TABLES)} TO quotation_app")
    op.execute(
        "GRANT UPDATE (name, archived_at, row_version, updated_at, updated_by) "
        "ON catalogue_categories TO quotation_app"
    )
    op.execute(
        "GRANT UPDATE (name, description, category_id, unit, minimum_quantity, quantity_step, "
        "search_terms, archived_at, archived_by, row_version, updated_at, updated_by) "
        "ON catalogue_products TO quotation_app"
    )
    # Deleting is allowed only for unused products (the service checks, Q12).
    op.execute("GRANT DELETE ON catalogue_products TO quotation_app")
    op.execute(
        "GRANT SELECT ON catalogue_categories, catalogue_products, catalogue_prices, "
        "catalogue_stock TO quotation_worker"
    )

    op.execute(f"DROP FUNCTION {_OLD_SIGNATURE}")
    op.execute(
        _business_function(
            ",\n    p_catalogue_provider text DEFAULT NULL, p_catalogue_name text DEFAULT NULL",
            _CATALOGUE_PART,
        )
    )
    _grant_function(_NEW_SIGNATURE)

    # Every business without an active product source gets our product list. FORCE RLS also
    # applies to the owner, so it is lifted for the backfill and restored right after.
    for table in _BACKFILL_TABLES:
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
    op.execute(
        f"""
        WITH missing AS (
            SELECT t.id AS tenant_id, gen_random_uuid() AS connection_id FROM tenants t
            WHERE NOT EXISTS (
                SELECT 1 FROM connections c WHERE c.tenant_id = t.id AND c.status = 'active'
                AND c.purposes @> ARRAY['catalogue']::text[]
            )
        ), created AS (
            INSERT INTO connections
                (id, tenant_id, provider_key, display_name, purposes, status, created_by)
            SELECT connection_id, tenant_id, '{_LOCAL_PROVIDER}', '{_LOCAL_NAME}',
                   ARRAY['catalogue'], 'active', '{_MIGRATION_ACTOR}'
            FROM missing
            RETURNING id, tenant_id
        )
        INSERT INTO connection_events (id, tenant_id, connection_id, kind, actor, detail)
        SELECT gen_random_uuid(), tenant_id, id, 'migrated', '{_MIGRATION_ACTOR}',
               jsonb_build_object('reason', 'default product source')
        FROM created
        """
    )
    for table in _BACKFILL_TABLES:
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")


def downgrade() -> None:
    op.execute(f"DROP FUNCTION {_NEW_SIGNATURE}")
    op.execute(_business_function("", ""))
    _grant_function(_OLD_SIGNATURE)

    op.execute("ALTER TABLE connections NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE connection_events NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE connection_events DISABLE TRIGGER connection_events_immutable")
    op.execute(
        "DELETE FROM connection_events WHERE connection_id IN "
        f"(SELECT id FROM connections WHERE provider_key = '{_LOCAL_PROVIDER}')"
    )
    op.execute(f"DELETE FROM connections WHERE provider_key = '{_LOCAL_PROVIDER}'")
    op.execute("ALTER TABLE connection_events ENABLE TRIGGER connection_events_immutable")
    op.execute("ALTER TABLE connection_events FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE connections FORCE ROW LEVEL SECURITY")

    for table in reversed(_TABLES):
        op.drop_table(table)
    # RESTRICT: fails loudly if something else started using the extension.
    op.execute("DROP EXTENSION IF EXISTS pg_trgm")
