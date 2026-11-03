"""Migrate and check the local databases, then run all tests against sales_test.

sales_dev keeps your development data; integration tests only ever use sales_test,
because they clear intake and job tables between tests. Use --cycle to also downgrade the test
database to empty, upgrade again and repeat the tests (CI does this).
"""

import argparse
import os
import subprocess
import sys

from sqlalchemy.engine import make_url

from sales_platform.platform.config import Settings

DEV_DATABASE = "sales_dev"
TEST_DATABASE = "sales_test"


def _with_database(url: str, database: str) -> str:
    return make_url(url).set(database=database).render_as_string(hide_password=False)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cycle", action="store_true", help="downgrade/upgrade test DB and retest")
    arguments = parser.parse_args()

    settings = Settings()
    dev_urls = {
        "runtime": settings.database_url.get_secret_value(),
        "owner": settings.migration_url(),
        "worker": settings.worker_url(),
    }
    if settings.environment != "development" or any(
        make_url(url).host not in {"127.0.0.1", "localhost"}
        or make_url(url).database != DEV_DATABASE
        for url in dev_urls.values()
    ):
        raise SystemExit("This helper only targets the local sales_dev/sales_test databases.")
    test_urls = {role: _with_database(url, TEST_DATABASE) for role, url in dev_urls.items()}

    def run(command: list[str], **overrides: str) -> None:
        # Credentials travel only in the child process environment and are never printed.
        environment = {**os.environ, **overrides}
        subprocess.run([sys.executable, "-m", *command], check=True, env=environment)

    on_test_db = {"SALES_MIGRATION_DATABASE_URL": test_urls["owner"]}
    test_env = {
        "TEST_DATABASE_URL": test_urls["runtime"],
        "TEST_MIGRATION_DATABASE_URL": test_urls["owner"],
        "TEST_WORKER_DATABASE_URL": test_urls["worker"],
    }
    print(f"== {DEV_DATABASE}: migrate and check ==", flush=True)
    run(["alembic", "upgrade", "head"])
    run(["alembic", "check"])
    print(f"== {TEST_DATABASE}: migrate, check and test ==", flush=True)
    run(["alembic", "upgrade", "head"], **on_test_db)
    run(["alembic", "check"], **on_test_db)
    run(["pytest", "--run-integration"], **test_env)
    if arguments.cycle:
        print(f"== {TEST_DATABASE}: downgrade to empty, upgrade, retest ==", flush=True)
        run(["alembic", "downgrade", "base"], **on_test_db)
        run(["alembic", "upgrade", "head"], **on_test_db)
        run(["pytest", "--run-integration"], **test_env)


if __name__ == "__main__":
    main()
