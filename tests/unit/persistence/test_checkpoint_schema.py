"""The baseline's LangGraph checkpoint schema must match the installed library.

The baseline migration (ADR 0026 step 4) creates LangGraph's checkpoint tables and records
which of the library's own schema migrations they contain (checkpoint_migrations rows 0..n).
The worker role may not change the schema, so the library must never find a migration missing.
If this fails after upgrading langgraph-checkpoint-postgres, add a new Alembic migration with
the library's new statements and their checkpoint_migrations rows (do not edit the baseline).
"""

import re
from pathlib import Path

from langgraph.checkpoint.postgres.base import BasePostgresSaver

BASELINE = Path(__file__).resolve().parents[3] / "migrations" / "versions" / "0001_baseline.sql"


def test_baseline_records_every_checkpoint_migration_of_the_library() -> None:
    recorded = [
        int(version)
        for version in re.findall(
            r"INSERT INTO public\.checkpoint_migrations VALUES \((\d+)\);",
            BASELINE.read_text(encoding="utf-8"),
        )
    ]
    assert recorded == list(range(len(BasePostgresSaver.MIGRATIONS)))


def test_baseline_creates_the_checkpoint_tables() -> None:
    sql = BASELINE.read_text(encoding="utf-8")
    for table in ("checkpoint_migrations", "checkpoints", "checkpoint_blobs", "checkpoint_writes"):
        assert f"CREATE TABLE public.{table} (" in sql
