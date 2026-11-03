"""Baseline: the whole schema as of the former 0015_orders (ADR 0026 step 4, user 2026-11-03).

The 15 earlier migrations (0001_tenants ... 0015_orders) were replaced by this one file: it
runs 0001_baseline.sql, generated once from `pg_dump --schema-only` of a database at
0015_orders, with the roles renamed to sales_owner / sales_app / sales_worker. It creates every
table, index, row-level security policy, trigger, function and grant, plus LangGraph's
checkpoint schema rows. Databases made with the old migrations are not upgraded: they are
recreated (scripts/recreate_local_database.py), as all local data is synthetic.

Runs as the owner role, so the owner owns everything it creates. The SQL goes to the driver
unchanged (no bind-parameter parsing), because it contains ":=" and "%" in function bodies.

Downgrade drops everything this migration created and leaves an empty schema (Alembic's own
alembic_version table stays).
"""

from pathlib import Path

from alembic import op

revision = "0001_baseline"
down_revision = None
branch_labels = None
depends_on = None

_SQL = Path(__file__).with_suffix(".sql")

_DROP_ALL = """
DO $$
DECLARE item record;
BEGIN
    FOR item IN
        SELECT tablename FROM pg_tables
        WHERE schemaname = 'public' AND tablename <> 'alembic_version'
    LOOP
        EXECUTE format('DROP TABLE IF EXISTS public.%I CASCADE', item.tablename);
    END LOOP;
    -- Our functions only: the pg_trgm functions belong to the extension, dropped below.
    FOR item IN
        SELECT p.oid::regprocedure AS signature FROM pg_proc p
        JOIN pg_namespace n ON n.oid = p.pronamespace
        WHERE n.nspname = 'public' AND NOT EXISTS (
            SELECT 1 FROM pg_depend d WHERE d.objid = p.oid AND d.deptype = 'e'
        )
    LOOP
        EXECUTE format('DROP FUNCTION IF EXISTS %s CASCADE', item.signature);
    END LOOP;
END $$;
DROP EXTENSION IF EXISTS pg_trgm;
REVOKE ALL ON alembic_version FROM sales_app;
"""


def _run(sql: str) -> None:
    # The DB-API cursor with no parameters: psycopg sends the script as is (several statements).
    cursor = op.get_bind().connection.cursor()
    try:
        cursor.execute(sql)
    finally:
        cursor.close()


def upgrade() -> None:
    _run(_SQL.read_text(encoding="utf-8"))


def downgrade() -> None:
    _run(_DROP_ALL)
