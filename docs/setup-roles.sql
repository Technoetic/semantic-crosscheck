-- semantic-crosscheck: database roles, schemas and grants (a psql script).
--
-- Two roles keep the source data read-only for every stage except the demo loader:
--   loader role  owns the source schema and creates its tables; its DSN goes in SXC_PG_OWNER_DSN and only
--                `sxc.py demo` uses it
--   check role   reads the source tables and owns the output schema; its DSN goes in SXC_PG_DSN (every other stage)
--
-- Run it with psql as a superuser, connected to the target database. The four names are psql variables, so nothing
-- in this file needs editing. psql itself is required: the file uses psql variables, \gexec and \gset.
--
--   psql -U postgres -d <database> -v loader_role=sxc_loader -v check_role=sxc_check \
--        -v source_schema=sxc_demo -v output_schema=sxc_out -f docs/setup-roles.sql
--
-- source_schema and output_schema must match source.schemas and source.output_schema in crosscheck.toml; the two
-- roles must differ, and so must the two schemas. A variable that is not set leaves its placeholder in the SQL; the
-- first statement uses all four, so the script stops there with a syntax error before it has changed anything.
-- Running the script again changes nothing.
--
-- This file contains no passwords. Set them interactively afterwards, so they never reach a file or the shell
-- history (psql sends only the hashed password):
--   \password sxc_loader
--   \password sxc_check
--
-- Existing data: run the same file with loader_role set to the role that owns (and creates) the existing source
-- tables and source_schema set to their schema, once per source schema. That role and that schema already exist and
-- are left as they are; the file creates the check role and its output schema and gives the check role read access.
-- The harness never connects as the owner role: only `sxc.py demo` uses SXC_PG_OWNER_DSN, and the demo belongs in a
-- throw-away database.
--
-- Tested as a superuser on PostgreSQL 17 (CI runs it that way). Without superuser rights (some managed services) an
-- administrator needs CREATEROLE with createrole_self_grant = 'set, inherit' (so it can SET ROLE to and inherit
-- from the roles it creates) and ownership of the database; with 'set' alone the file stops at the grants on the
-- loader's schema. That variant was checked by hand only; it is not tested in CI.

\set ON_ERROR_STOP on

-- 0. Every name must be set before anything changes: a variable that is missing stays a placeholder here, which
--    is a syntax error.
SELECT :'loader_role' AS loader_role, :'check_role' AS check_role, :'source_schema' AS source_schema,
       :'output_schema' AS output_schema;

-- 1. Login roles without any administrative attribute. Existing roles are left as they are.
SELECT format('CREATE ROLE %I LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS', r)
FROM (VALUES (:'loader_role'), (:'check_role')) AS v(r)
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r)
\gexec

-- 2. The source schema belongs to the loader, the output schema to the check role. Existing schemas keep their owner.
CREATE SCHEMA IF NOT EXISTS :"source_schema" AUTHORIZATION :"loader_role";
CREATE SCHEMA IF NOT EXISTS :"output_schema" AUTHORIZATION :"check_role";

-- 3. The check role reads the source schema and nothing more: USAGE (not CREATE) on the schema, SELECT on the tables
--    that exist now and, through default privileges, on the tables the loader role creates later.
GRANT USAGE ON SCHEMA :"source_schema" TO :"check_role";
GRANT SELECT ON ALL TABLES IN SCHEMA :"source_schema" TO :"check_role";
ALTER DEFAULT PRIVILEGES FOR ROLE :"loader_role" IN SCHEMA :"source_schema"
    GRANT SELECT ON TABLES TO :"check_role";

-- 4. build computes each closure in temporary tables (doctor checks privileges:temporary). PUBLIC holds TEMPORARY on
--    a new database; the explicit grant keeps build working where an administrator revoked it from PUBLIC.
SELECT current_database() AS sxc_database \gset
GRANT TEMPORARY ON DATABASE :"sxc_database" TO :"check_role";

-- 5. Optional hardening, left to the administrator because it also affects other users of an existing database.
--    PostgreSQL 15 and later give PUBLIC no CREATE on the public schema; a cluster upgraded from an older release
--    may still allow it:
-- REVOKE CREATE ON SCHEMA public FROM PUBLIC;
--    Allow only these roles (and existing grantees) to connect:
-- REVOKE CONNECT ON DATABASE :"sxc_database" FROM PUBLIC;
-- GRANT CONNECT ON DATABASE :"sxc_database" TO :"loader_role", :"check_role";

-- Check: `sxc.py doctor --config crosscheck.toml` verifies that the check role can read every source table, cannot
-- write any of them (nor own their schema or create objects in it), may create temporary tables and can create
-- objects in the output schema.
