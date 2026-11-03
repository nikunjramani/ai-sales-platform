"""Bounded PostgreSQL connections and readiness checks."""

from sqlalchemy import Engine, create_engine, text

# Readiness requires exactly this migration head; update it with every new migration.
EXPECTED_REVISION = "0015_orders"


def build_engine(url: str) -> Engine:
    return create_engine(
        url,
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=0,
        pool_timeout=3,
        hide_parameters=True,
        connect_args={"connect_timeout": 3, "options": "-c statement_timeout=3000"},
    )


def database_ready(engine: Engine) -> bool:
    """Check schema revision and reject a privileged runtime connection."""
    with engine.connect() as connection:
        revisions = set(
            connection.execute(text("SELECT version_num FROM alembic_version")).scalars()
        )
        role = connection.execute(
            text(
                "SELECT rolsuper, rolbypassrls, "
                "pg_has_role(current_user, 'quotation_owner', 'MEMBER') "
                "FROM pg_roles WHERE rolname = current_user"
            )
        ).one()
        return revisions == {EXPECTED_REVISION} and not any(role)
