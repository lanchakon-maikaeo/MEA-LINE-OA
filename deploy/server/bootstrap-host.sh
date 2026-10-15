#!/usr/bin/env bash
# One-time host setup for staging/production (M12): directories, generated secrets, database TLS,
# swap and systemd timers. Safe to re-run: existing secrets and certificates are kept.
#
#   sudo ./bootstrap-host.sh --env staging|production --public-host <ip>.sslip.io \
#        [--fallback-phone 02-…] [--fallback-form-url https://forms…]
#   FAOJ_ROOT=… ./bootstrap-host.sh --env rehearsal --public-host localhost   (laptop rehearsal)
#
# LINE credentials and the backup keys are typed by the operator into the *.secret.env files
# afterwards; this script never prints a secret.
set -euo pipefail
umask 077
FAOJ_ROOT=${FAOJ_ROOT:-/srv/faoj}
ENV='' HOST='' PHONE='' FORM=''
while [ $# -gt 0 ]; do
  case "$1" in
    --env) ENV=$2; shift 2;;
    --public-host) HOST=$2; shift 2;;
    --fallback-phone) PHONE=$2; shift 2;;
    --fallback-form-url) FORM=$2; shift 2;;
    *) echo "unknown option $1" >&2; exit 2;;
  esac
done
case "$ENV" in staging|production|rehearsal) ;; *) echo "--env staging|production|rehearsal" >&2; exit 2;; esac
[ -n "$HOST" ] || { echo "--public-host is required" >&2; exit 2; }
LINUX=false; [ "$(uname -s)" = Linux ] && LINUX=true
if $LINUX && [ "$(id -u)" != 0 ]; then echo "run as root" >&2; exit 1; fi

rand() { python3 -c 'import secrets; print(secrets.token_urlsafe(int(__import__("sys").argv[1])))' "${1:-32}"; }
fernet() { python3 -c 'import base64, os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())'; }
write_once() {  # path, mode, content — never overwrites
  [ -e "$1" ] && { echo "keep $1"; return 0; }
  ( umask 077; printf '%s\n' "$3" > "$1" ); chmod "$2" "$1"; echo "made $1"
}

mkdir -p "$FAOJ_ROOT"/{env,releases,run,drills,restore,tls/db} \
  "$FAOJ_ROOT"/data/{postgres,media,n8n,caddy,clamav,dumps,restic-cache,pgbackrest-spool}
chmod 755 "$FAOJ_ROOT" "$FAOJ_ROOT/run"
if $LINUX; then
  chown 999:999 "$FAOJ_ROOT/data/postgres" "$FAOJ_ROOT/data/pgbackrest-spool"   # postgres in the db image
  chown 10001:10001 "$FAOJ_ROOT/data/media"                                    # app user in the app image
  chown 1000:1000 "$FAOJ_ROOT/data/n8n"                                        # node user in the n8n image
fi
chmod 700 "$FAOJ_ROOT/data/media" "$FAOJ_ROOT/data/n8n"

case "$ENV" in
  production) WORKERS=3; PGBUF=1GB; DBMEM=2g; N8NMEM=1g; BACKUP=pgbackrest; ARCHIVE=on; CLAM=true; INV=manual_reference;;
  staging)    WORKERS=2; PGBUF=256MB; DBMEM=900m; N8NMEM=800m; BACKUP=dump; ARCHIVE=off; CLAM=true; INV=fake;;
  rehearsal)  WORKERS=2; PGBUF=128MB; DBMEM=600m; N8NMEM=600m; BACKUP=pgbackrest; ARCHIVE=on; CLAM=false; INV=fake;;
esac
APP_ENV=$ENV; [ "$ENV" = rehearsal ] && APP_ENV=staging
PORTS="HTTP_PORT=80
HTTPS_PORT=443"
LOCAL_CERTS=""
ORIGIN="https://$HOST"
if [ "$ENV" = rehearsal ]; then
  PORTS="HTTP_PORT=8080
HTTPS_PORT=8443
N8N_EDITOR_PORT=15678"
  LOCAL_CERTS=local_certs
  ORIGIN="https://$HOST:8443"
fi

write_once "$FAOJ_ROOT/env/compose.env" 644 "# Host settings for faoj (non-secret). Edit, then 'faoj deploy' again.
FAOJ_ROOT=$FAOJ_ROOT
FAOJ_ENV=$ENV
PUBLIC_HOST=$HOST
$PORTS
CADDY_LOCAL_CERTS=$LOCAL_CERTS
FALLBACK_PHONE=$PHONE
# ADR 0008: one number per topic (กคว. / กองอุปกรณ์การจำหน่าย / กตฟ.); FALLBACK_PHONE is for the rest.
FALLBACK_PHONE_METER=
FALLBACK_PHONE_EQUIPMENT=
FALLBACK_PHONE_USAGE=
FALLBACK_FORM_URL=$FORM
# ADR 0015: LINE's webhook host (ngrok static domain); empty keeps the ngrok container off.
NGROK_DOMAIN=
# ADR 0015 Q6/Q20: faoj auto-promote (production, minor releases only, off until switched on).
# The staging gate "metric" needs a staging host reporting to OCI Monitoring; "none" skips it.
AUTO_PROMOTE_WINDOW=18-07
AUTO_PROMOTE_SOAK_MINUTES=60
AUTO_PROMOTE_STAGING_GATE=metric
WEB_WORKERS=$WORKERS
PG_SHARED_BUFFERS=$PGBUF
DB_MEM=$DBMEM
N8N_MEM=$N8NMEM
CLAMAV_ENABLED=$CLAM
BACKUP_MODE=$BACKUP
PG_ARCHIVE_MODE=$ARCHIVE
# Object Storage (S3-compatible) for pgBackRest and restic; filled in from Terraform outputs.
BACKUP_S3_ENDPOINT=
BACKUP_S3_REGION=ap-singapore-1
BACKUP_DB_BUCKET=faoj-backup
BACKUP_S3_VERIFY_TLS=y
DRILL_SOURCE_ENV=production
# Metrics to OCI Monitoring (instance principal); stdout on rehearsals.
METRICS_SINK=$([ "$ENV" = rehearsal ] && echo stdout || echo oci)
OCI_REGION=ap-singapore-1
OCI_COMPARTMENT_OCID="

write_once "$FAOJ_ROOT/env/django.env" 644 "# Django settings (non-secret) for $ENV.
APP_ENV=$APP_ENV
ALLOWED_HOSTS=$HOST,web
CSRF_TRUSTED_ORIGINS=$ORIGIN
STAFF_WEB_BASE_URL=$ORIGIN
INVENTORY_MODE=$INV
AI_ENABLED=false
OUTBOUND_ENABLED=false
LINE_PUSH_ENABLED=false
# Outside production only these LINE user IDs get replies (comma-separated).
LINE_ALLOWED_RECIPIENTS=
STAFF_EMAIL_ENABLED=false"

OWNER_PW=$(rand 32) APP_PW=$(rand 32) N8N_PW=$(rand 32) ORCH=$(rand 48)
write_once "$FAOJ_ROOT/env/db.secret.env" 600 "POSTGRES_PASSWORD=$(rand 32)
FAOJ_OWNER_PASSWORD=$OWNER_PW
FAOJ_APP_PASSWORD=$APP_PW
N8N_DB_PASSWORD=$N8N_PW
# pgBackRest repository (production): encryption pass and the Object Storage key pair.
PGBACKREST_REPO1_CIPHER_PASS=$(rand 48)
PGBACKREST_REPO1_S3_KEY=
PGBACKREST_REPO1_S3_KEY_SECRET="
# Re-read what was really kept, so the app files match an older db.secret.env.
# shellcheck disable=SC1091
. "$FAOJ_ROOT/env/db.secret.env"
DBURL_TAIL="@db:5432/faoj?sslmode=verify-full&sslrootcert=/run/faoj-tls/ca.crt"
write_once "$FAOJ_ROOT/env/app.secret.env" 600 "DJANGO_SECRET_KEY=$(rand 64)
DATABASE_URL=postgresql://faoj_app:$FAOJ_APP_PASSWORD$DBURL_TAIL
ORCHESTRATOR_TOKEN=$ORCH
AI_SECRET_KEY=$(fernet)
LINE_CHANNEL_SECRET=
LINE_CHANNEL_ACCESS_TOKEN="
write_once "$FAOJ_ROOT/env/migrate.secret.env" 600 "DATABASE_URL=postgresql://faoj_owner:$FAOJ_OWNER_PASSWORD$DBURL_TAIL"
ORCH_KEPT=$(grep '^ORCHESTRATOR_TOKEN=' "$FAOJ_ROOT/env/app.secret.env" | cut -d= -f2-)
write_once "$FAOJ_ROOT/env/n8n.secret.env" 600 "DB_POSTGRESDB_PASSWORD=$N8N_DB_PASSWORD
N8N_ENCRYPTION_KEY=$(rand 48)
ORCHESTRATOR_TOKEN=$ORCH_KEPT"
write_once "$FAOJ_ROOT/env/ngrok.secret.env" 600 "# ngrok agent authtoken (owner's account), typed in by the owner.
NGROK_AUTHTOKEN="
write_once "$FAOJ_ROOT/env/backup.secret.env" 600 "# restic → Object Storage (files + n8n volume). Key pair from the backup-writer user.
RESTIC_REPOSITORY=
RESTIC_PASSWORD=$(rand 48)
AWS_ACCESS_KEY_ID=
AWS_SECRET_ACCESS_KEY="

if [ ! -s "$FAOJ_ROOT/tls/db/server.key" ]; then
  tls="$FAOJ_ROOT/tls/db"
  openssl req -x509 -new -nodes -newkey rsa:3072 -days 3650 -subj "/CN=faoj-db-ca" \
    -keyout "$tls/ca.key" -out "$tls/ca.crt" 2>/dev/null
  openssl req -new -nodes -newkey rsa:3072 -subj "/CN=db" -keyout "$tls/server.key" -out "$tls/server.csr" 2>/dev/null
  printf 'subjectAltName=DNS:db\nextendedKeyUsage=serverAuth\n' > "$tls/ext.cnf"
  openssl x509 -req -in "$tls/server.csr" -CA "$tls/ca.crt" -CAkey "$tls/ca.key" -CAcreateserial \
    -days 1825 -extfile "$tls/ext.cnf" -out "$tls/server.crt" 2>/dev/null
  rm -f "$tls/server.csr" "$tls/ext.cnf" "$tls/ca.srl"
  chmod 644 "$tls/ca.crt" "$tls/server.crt"; chmod 600 "$tls/server.key" "$tls/ca.key"
  chmod 755 "$tls"
  echo "made database TLS (CA valid 10 years, server 5 years)"
fi

if $LINUX; then
  if ! swapon --show | grep -q /swapfile; then
    fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile >/dev/null && swapon /swapfile
    grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
    sysctl -q vm.swappiness=10; echo 'vm.swappiness=10' > /etc/sysctl.d/90-faoj.conf
  fi
  here=$(cd "$(dirname "$0")" && pwd)
  for unit in "$here"/systemd/*; do
    sed "s#@FAOJ_ROOT@#$FAOJ_ROOT#g" "$unit" > "/etc/systemd/system/$(basename "$unit")"
  done
  systemctl daemon-reload
  timers="faoj-metrics.timer"
  case "$ENV" in
    # auto-promote is a no-op until 'faoj auto-promote on "reason"' (ADR 0015 Q6/Q20).
    production) timers="$timers faoj-backup-full.timer faoj-backup-diff.timer faoj-backup-files.timer faoj-quota-check.timer faoj-auto-promote.timer";;
    # auto-deploy is a no-op until an operator runs 'faoj auto-deploy on "reason"' (option ข).
    staging) timers="$timers faoj-backup-dump.timer faoj-auto-deploy.timer"
      [ -s "$FAOJ_ROOT/env/drill.secret.env" ] && timers="$timers faoj-restore-drill.timer";;
  esac
  for t in $timers; do systemctl enable --now "$t" >/dev/null; done
  ln -sfn "$FAOJ_ROOT/releases/current/bin/faoj" /usr/local/sbin/faoj
  echo "timers: $timers"
fi
echo "Next: put LINE credentials in $FAOJ_ROOT/env/app.secret.env and backup keys in db/backup.secret.env, then 'faoj deploy <image@sha256:…>'."
