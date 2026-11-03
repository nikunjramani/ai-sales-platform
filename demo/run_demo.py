"""Start the API, the worker and the demo page together; Ctrl+C stops all of them.

    docker compose up -d --wait          # database, once
    uv run python demo/run_demo.py       # then open http://127.0.0.1:8001

If the API (port 8000) is already running, it is reused. Pass --no-worker if you already run a
worker yourself.
"""

import argparse
import os
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def api_running() -> bool:
    try:
        with urllib.request.urlopen("http://127.0.0.1:8000/health/live", timeout=2):
            return True
    except OSError:
        return False


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-worker", action="store_true")
    parser.add_argument("--no-browser", action="store_true")
    arguments = parser.parse_args()

    python = sys.executable
    print("Applying migrations and seed data...")
    subprocess.run([python, "-m", "alembic", "upgrade", "head"], cwd=ROOT, check=True)
    subprocess.run([python, "-m", "sales_platform.persistence.seed"], cwd=ROOT, check=True)

    processes = []
    if not api_running():
        processes.append(
            subprocess.Popen(
                [
                    python,
                    "-m",
                    "uvicorn",
                    "sales_platform.api.app:create_app",
                    "--factory",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    "8000",
                ],
                cwd=ROOT,
                # Serves the built portal and the website chat widget (npm run build in portal/).
                env={**os.environ, "SALES_PORTAL_DIST_DIR": str(ROOT / "portal" / "dist")},
            )
        )
    if not arguments.no_worker:
        processes.append(subprocess.Popen([python, "-m", "sales_platform.worker"], cwd=ROOT))
    processes.append(
        subprocess.Popen(
            [
                python,
                "-m",
                "uvicorn",
                "server:app",
                "--host",
                "127.0.0.1",
                "--port",
                "8001",
                "--app-dir",
                str(ROOT / "demo"),
            ],
            cwd=ROOT,
        )
    )
    for _ in range(30):
        if api_running():
            break
        time.sleep(1)
    print("\nDemo: http://127.0.0.1:8001   (API docs: http://127.0.0.1:8000/docs)")
    print("Press Ctrl+C to stop.\n")
    if not arguments.no_browser:
        webbrowser.open("http://127.0.0.1:8001")
    try:
        while all(process.poll() is None for process in processes):
            time.sleep(1)
        print("A process stopped; shutting down the others.")
    except KeyboardInterrupt:
        pass
    finally:
        for process in processes:
            process.terminate()


if __name__ == "__main__":
    main()
