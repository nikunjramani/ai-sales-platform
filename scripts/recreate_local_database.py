"""Recreate the local development database from scratch (development only, deletes local data).

  uv run python scripts/recreate_local_database.py
      [--remove-old-project quotation-local]   also stop the container of the old Compose
                                               project (before ADR 0026 step 4)
      [--delete-volume NAME]                   delete an old data volume by its exact name,
                                               e.g. quotation-local_quotation_pgdata

Steps: stop and remove this project's database container and volume, start a fresh one (the
init script creates the sales_owner / sales_app / sales_worker roles and sales_test), apply
the migrations to sales_dev and seed the demo businesses. All local data is synthetic; run
scripts/check_database.py afterwards to migrate and test sales_test.

Refused when SALES_ENVIRONMENT is not development.
"""

import argparse
import subprocess
import sys

from sales_platform.config import Settings


def run(command: list[str]) -> None:
    print("$", " ".join(command), flush=True)
    subprocess.run(command, check=True)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--remove-old-project", metavar="NAME")
    parser.add_argument("--delete-volume", metavar="NAME")
    arguments = parser.parse_args()
    if Settings().environment != "development":
        raise SystemExit("Only for a development environment (SALES_ENVIRONMENT=development)")
    if arguments.remove_old_project:
        # Containers are found by the project label; volumes must be named explicitly, since
        # "down --volumes" would only remove the volumes of the current compose file.
        run(["docker", "compose", "-p", arguments.remove_old_project, "down"])
    if arguments.delete_volume:
        run(["docker", "volume", "rm", arguments.delete_volume])
    run(["docker", "compose", "down", "--volumes"])
    run(["docker", "compose", "up", "-d", "--wait"])
    run([sys.executable, "-m", "alembic", "upgrade", "head"])
    run([sys.executable, "-m", "sales_platform.persistence.seed"])
    print("Fresh database ready with demo data. Next: uv run python scripts/check_database.py")


if __name__ == "__main__":
    main()
