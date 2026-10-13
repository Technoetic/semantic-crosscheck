-- semantic-crosscheck: database roles, schemas and grants (template).
--
-- Two roles keep the source data read-only for every stage except the demo loader:
--   loader role  owns the source schema; its DSN goes in SXC_PG_OWNER_DSN and only `sxc.py demo` uses it
--   check role   SELECT on the source tables, owns the output schema; its DSN goes in SXC_PG_DSN
--
-- Run it with psql as a superuser (or a role with CREATEROLE that may create schemas), connected to the
-- target database. The four names are psql variables, so nothing in this file needs editing:
--
--   psql -d <database> -v loader_role=sxc_loader -v check_role=sxc_check \
--        -v source_schema=sxc_demo -v output_schema=sxc_out -f docs/setup-roles.sql
--
-- source_schema and output_schema must match source.schemas and source.output_schema in crosscheck.toml.
-- A variable that is not set leaves its placeholder in the SQL, which is a syntax error, and the script stops.
--
-- This file contains no passwords. Set them interactively afterwards, so they never reach a file or the
-- shell history (psql sends only the hashed password):
--   \password sxc_loader
--   \password sxc_check
--
-- Existing data: when the source schema already exists and belongs to another role, skip the loader role
-- and the CREATE SCHEMA for the source, and run only the grants of section 3 as the schema's owner.

\set ON_ERROR_STOP on

-- 1. Login roles without any administrative attribute. Existing roles are left as they are.
SELECT format('CREATE ROLE %I LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS', r)
FROM (VALUES (:'loader_role'), (:'check_role')) AS v(r)
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r)
\gexec

-- 2. The source schema belongs to the loader, the output schema to the check role.
CREATE SCHEMA IF NOT EXISTS :"source_schema" AUTHORIZATION :"loader_role";
CREATE SCHEMA IF NOT EXISTS :"output_schema" AUTHORIZATION :"check_role";

-- 3. The check role reads the source schema and nothing more: USAGE (not CREATE) on the schema, SELECT on
--    the tables that exist now and, through default privileges, on tables the loader creates later.
GRANT USAGE ON SCHEMA :"source_schema" TO :"check_role";
GRANT SELECT ON ALL TABLES IN SCHEMA :"source_schema" TO :"check_role";
ALTER DEFAULT PRIVILEGES FOR ROLE :"loader_role" IN SCHEMA :"source_schema"
    GRANT SELECT ON TABLES TO :"check_role";

-- 4. Nobody creates objects in the public schema (the default on PostgreSQL 15 and later; stated here so
--    an older cluster upgraded to 17 behaves the same).
REVOKE CREATE ON SCHEMA public FROM PUBLIC;

-- 5. Optional: allow only these roles (and existing grantees) to connect to this database.
-- SELECT current_database() AS db \gset
-- REVOKE CONNECT ON DATABASE :"db" FROM PUBLIC;
-- GRANT CONNECT ON DATABASE :"db" TO :"loader_role", :"check_role";

-- Check: `sxc.py doctor --config crosscheck.toml` verifies that the check role can read every source table,
-- cannot write any of them and can create objects in the output schema.
