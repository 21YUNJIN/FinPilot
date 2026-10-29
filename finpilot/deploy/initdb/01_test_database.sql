# Extra database used by the integration/security suites (TEST_DATABASE_URL).
#
# Executed once by the postgres image entrypoint on first initialisation
# (mounted at /docker-entrypoint-initdb.d). \gexec is a psql meta-command, so
# CREATE DATABASE runs outside a transaction block.
#
# NOT VERIFIED ON THE REFERENCE HOST (no container runtime); the Windows-native
# path creates the same database with createdb/psql instead -- see deploy/README.md.
SELECT format('CREATE DATABASE %I OWNER %I', current_user || '_test', current_user)
WHERE NOT EXISTS (
    SELECT 1 FROM pg_database WHERE datname = current_user || '_test'
)\gexec