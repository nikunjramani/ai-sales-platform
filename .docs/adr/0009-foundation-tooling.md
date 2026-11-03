# 0009: Foundation tooling and database roles
Date: 2026-10-22
Status: implemented and verified (local PostgreSQL and GitHub Actions)

## Context
M0 needs reproducible dependencies, observable startup and real migrations without prematurely building the workflow. Python 3.13.14, uv 0.13.1 and Git are available; Docker/PostgreSQL/WSL are not.

## Decision
Target Python 3.13 and lock dependencies with uv. Use a FastAPI factory with health endpoints only, synchronous SQLAlchemy 2/psycopg 3 for bounded DB checks, and Alembic for explicit migrations.
Describe PostgreSQL 17.11-bookworm in Compose, loopback port 55432. Separate bootstrap, owner and runtime roles; force row security on the initial tenant registry.
Use offline tests with fake dependencies plus opt-in real PostgreSQL tests. CI describes migration/schema checks and upgrade/downgrade/re-upgrade against a disposable DB.
Use the installed Starlette-compatible httpx2 test transport. Exact versions are in the lockfile.

## Alternatives
SQLite fallback, privileged runtime credentials, automatic startup schema creation, installing future AI/provider libraries now, or automatically changing Windows virtualization settings.

## Consequences
Docker setup is a real M0 gate. Initial database filtering is not authenticated tenant context; M1/M4 must supply that before business operations.
Synchronous access is sufficient for initial bounded health checks; revisit before workflow concurrency if evidence warrants.
No public business-data routes exist. Model libraries arrive at their planned milestones.

## Verification
Locked install, Ruff, mypy, offline pytest, offline migration generation and in-process health smoke checks pass. Update 2026-10-22: live local PostgreSQL migrations, downgrade/re-upgrade, seed, integration tests and API readiness passed after Docker Desktop was enabled. Remote GitHub Actions [run 39734937763](https://github.com/nikunjramani/ai-sales-platform/actions/runs/39734937763) passed the same checks from a fresh environment. See setup/handoff for results.
