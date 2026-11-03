"""Alembic entry point: use explicit migration credentials, never log the URL."""

from typing import Any

from alembic import context
from sqlalchemy import create_engine, pool

from sales_platform.config import Settings
from sales_platform.persistence.models import Base

target_metadata = Base.metadata
url = Settings().migration_url()

# LangGraph checkpoint tables come from the library's own schema (in the baseline) and
# are not described by our models, so schema comparison (alembic check) must skip them.
CHECKPOINT_TABLES = frozenset(
    {"checkpoint_migrations", "checkpoints", "checkpoint_blobs", "checkpoint_writes"}
)


def include_object(
    obj: object, name: str | None, type_: str, reflected: bool, compare_to: object
) -> bool:
    table = getattr(obj, "table", None)
    table_name = name if type_ == "table" else getattr(table, "name", None)
    return table_name not in CHECKPOINT_TABLES


options: dict[str, Any] = {"target_metadata": target_metadata, "include_object": include_object}

if context.is_offline_mode():
    context.configure(url=url, literal_binds=True, dialect_opts={"paramstyle": "named"}, **options)
    with context.begin_transaction():
        context.run_migrations()
else:
    engine = create_engine(url, poolclass=pool.NullPool, hide_parameters=True)
    with engine.connect() as connection:
        context.configure(connection=connection, **options)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()
