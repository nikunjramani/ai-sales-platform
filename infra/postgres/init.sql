-- Creates the application roles. Idempotent: it runs automatically when the development
-- volume is first initialized, and can be re-run on an existing volume to add new roles:
--   docker compose exec -T postgres psql -U postgres -d sales_dev -v ON_ERROR_STOP=1 \
--     -f /docker-entrypoint-initdb.d/01-roles.sql
-- psql quotes variables as SQL literals; secrets are read from the container environment.
\getenv owner_password POSTGRES_OWNER_PASSWORD
\getenv app_password POSTGRES_APP_PASSWORD
\getenv worker_password POSTGRES_WORKER_PASSWORD

-- sales_owner: runs migrations and seeds. sales_app: API runtime.
-- sales_worker: background jobs; may see the job queue across tenants, nothing else.
SELECT format(
    'CREATE ROLE %I LOGIN PASSWORD %L NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS',
    role_name, role_password)
FROM (VALUES
    ('sales_owner', :'owner_password'),
    ('sales_app', :'app_password'),
    ('sales_worker', :'worker_password')
) AS wanted(role_name, role_password)
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = wanted.role_name)
\gexec

REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE, CREATE ON SCHEMA public TO sales_owner;
GRANT USAGE ON SCHEMA public TO sales_app, sales_worker;
GRANT CONNECT ON DATABASE sales_dev TO sales_owner, sales_app, sales_worker;
-- Migrations create trusted extensions (pg_trgm, phase 3), which needs CREATE on the database.
-- sales_test is owned by sales_owner, so it has this already (user decision 2026-10-28).
GRANT CREATE ON DATABASE sales_dev TO sales_owner;

-- Separate database for integration tests, which may truncate tables between tests.
-- Integration tests refuse to run against a database whose name does not end in _test.
SELECT 'CREATE DATABASE sales_test OWNER sales_owner'
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'sales_test')
\gexec
GRANT CONNECT ON DATABASE sales_test TO sales_owner, sales_app, sales_worker;
\connect sales_test
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE, CREATE ON SCHEMA public TO sales_owner;
GRANT USAGE ON SCHEMA public TO sales_app, sales_worker;
