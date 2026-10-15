#!/bin/sh
# First start of an empty cluster (M12, E.2/E.7): one owner role runs migrations, the runtime
# role only reads and writes rows (apply_db_grants narrows it after each migrate), and n8n gets
# its own database with no access to business tables. Passwords come from the container env.
set -eu
: "${FAOJ_OWNER_PASSWORD:?}" "${FAOJ_APP_PASSWORD:?}" "${N8N_DB_PASSWORD:?}"
psql -v ON_ERROR_STOP=1 --username postgres --dbname postgres \
  -v owner_pw="$FAOJ_OWNER_PASSWORD" -v app_pw="$FAOJ_APP_PASSWORD" -v n8n_pw="$N8N_DB_PASSWORD" <<'SQL'
SELECT format('CREATE ROLE faoj_owner LOGIN PASSWORD %L', :'owner_pw')\gexec
SELECT format('CREATE ROLE faoj_app LOGIN PASSWORD %L', :'app_pw')\gexec
SELECT format('CREATE ROLE n8n LOGIN PASSWORD %L', :'n8n_pw')\gexec
CREATE DATABASE faoj OWNER faoj_owner;
CREATE DATABASE n8n OWNER n8n;
REVOKE ALL ON DATABASE faoj FROM PUBLIC;
REVOKE ALL ON DATABASE n8n FROM PUBLIC;
REVOKE ALL ON DATABASE postgres FROM PUBLIC;
GRANT CONNECT ON DATABASE faoj TO faoj_owner, faoj_app;
GRANT CONNECT ON DATABASE n8n TO n8n;
SQL
psql -v ON_ERROR_STOP=1 --username postgres --dbname faoj <<'SQL'
REVOKE ALL ON SCHEMA public FROM PUBLIC;
ALTER SCHEMA public OWNER TO faoj_owner;
GRANT USAGE ON SCHEMA public TO faoj_app;
ALTER DEFAULT PRIVILEGES FOR ROLE faoj_owner IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO faoj_app;
ALTER DEFAULT PRIVILEGES FOR ROLE faoj_owner IN SCHEMA public
  GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO faoj_app;
SQL
