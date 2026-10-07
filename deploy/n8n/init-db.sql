-- n8n metadata lives in its own database and role; it gets no access to business tables.
-- Run with: psql -v pw="$N8N_DB_PASSWORD" -f init-db.sql (idempotent).
SELECT format('CREATE ROLE n8n LOGIN PASSWORD %L', :'pw')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'n8n')\gexec
SELECT format('ALTER ROLE n8n PASSWORD %L', :'pw')\gexec
SELECT 'CREATE DATABASE n8n OWNER n8n'
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'n8n')\gexec
REVOKE CONNECT ON DATABASE mea_demo FROM PUBLIC;
