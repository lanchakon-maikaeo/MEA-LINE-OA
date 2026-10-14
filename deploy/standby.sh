#!/usr/bin/env bash
# The Mac as a warm standby for production (ADR 0015 Q9). Same `faoj`, Compose stack and images
# as the E5 host, under FAOJ_ROOT=~/faoj-standby. Normally only PostgreSQL runs here, in standby
# mode, replaying production's WAL from Object Storage (about 5 minutes behind) with the
# backup-READER key: this Mac can read production's backups but never write to them.
#
#   deploy/standby.sh setup      copy production's settings from E5 (needs the e5-move tunnel),
#                                then the owner types the backup-reader secret key
#   deploy/standby.sh build      build the arm64 db + app images here and unpack the release
#   deploy/standby.sh start      first time: restore the newest backup as a standby; then start
#   deploy/standby.sh status     in recovery? how far behind production? last files copy
#   deploy/standby.sh files      copy private files and n8n data from the restic backup
#   deploy/standby.sh check      launchd, every 5 minutes: keep it running, files hourly, warn
#                                when it falls more than 15 minutes behind
#   deploy/standby.sh failover   a person decides production is gone: stop E5's ngrok if it
#                                still answers, promote this database, start the whole stack
#                                here and move LINE's ngrok domain to this Mac
#
# Install the 5-minute check with: make standby-schedule
set -euo pipefail
umask 077

REPO=$(cd "$(dirname "$0")/.." && pwd)
ROOT=${FAOJ_STANDBY_ROOT:-$HOME/faoj-standby}
REG=localhost:5001
REGISTRY_IMAGE=registry:2@sha256:a3d8aaa63ed8681a604f1dea0aa03f100d5895b6a58ace528858a7b332415373
NGROK_DOMAIN=${E5_NGROK_DOMAIN:-crabmeat-lake-lunar.ngrok-free.dev}
export FAOJ_ROOT=$ROOT OCI_CLI_PROFILE=faoj OCI_CLI_AUTH=security_token PYTHONWARNINGS=ignore
LOG="$ROOT/run/standby.log"

say() { echo "[$(date +%H:%M:%S)] $*" >&2; }
die() { echo "standby: $*" >&2; exit 1; }
notify() { osascript -e "display notification \"$2\" with title \"$1\"" >/dev/null 2>&1 || true; }
faoj() { "$ROOT/releases/current/bin/faoj" "$@"; }
dc() {
  local rel; rel=$(cd "$ROOT/releases/current" 2>/dev/null && pwd -P) || die "no release: run build"
  docker compose -p faoj -f "$rel/compose.yaml" --env-file "$ROOT/env/compose.env" \
    --env-file "$rel/release.env" "$@"
}
set_kv() {  # file key value
  python3 - "$1" "$2" "$3" <<'PY'
import os, re, sys
path, key, value = sys.argv[1:4]
text = open(path).read()
text, n = re.subn(rf"^{re.escape(key)}=.*$", lambda m: f"{key}={value}", text, flags=re.M)
if not n:
    text = text.rstrip("\n") + f"\n{key}={value}\n"
mode = os.stat(path).st_mode
open(path, "w").write(text)
os.chmod(path, mode)
PY
}

setup() {
  mkdir -p "$ROOT"/{env,releases,run,drills,restore,tls/db} \
    "$ROOT"/data/{postgres,media,n8n,caddy,clamav,dumps,restic-cache,pgbackrest-spool}
  say "copying production's settings and database TLS from E5 (through the e5-move tunnel)"
  "$REPO/deploy/e5-move.sh" tunnel
  "$REPO/deploy/e5-move.sh" ssh 'sudo tar -C /srv/faoj -czf - env tls/db' | tar -C "$ROOT" -xzf -
  chmod 700 "$ROOT/env" "$ROOT/tls/db"; chmod 600 "$ROOT"/env/*.secret.env "$ROOT"/tls/db/*.key
  local env="$ROOT/env/compose.env"
  # This Mac: local ports and certificates, no ClamAV, no WAL archive and no Object Storage
  # writes (a standby must never touch production's repository), ngrok only after a failover.
  set_kv "$env" FAOJ_ROOT "$ROOT"
  set_kv "$env" PUBLIC_HOST localhost
  set_kv "$env" HTTP_PORT 8080
  set_kv "$env" HTTPS_PORT 8443
  set_kv "$env" N8N_EDITOR_PORT 15678
  set_kv "$env" CADDY_LOCAL_CERTS local_certs
  set_kv "$env" CLAMAV_ENABLED false
  set_kv "$env" BACKUP_MODE dump
  set_kv "$env" PG_ARCHIVE_MODE off
  set_kv "$env" METRICS_SINK stdout
  set_kv "$env" NGROK_DOMAIN ""
  set_kv "$ROOT/env/django.env" ALLOWED_HOSTS "localhost,web,$NGROK_DOMAIN"
  set_kv "$ROOT/env/django.env" CSRF_TRUSTED_ORIGINS "https://localhost:8443"
  set_kv "$ROOT/env/django.env" STAFF_WEB_BASE_URL "https://localhost:8443"
  reader_key
}

reader_key() {
  local tenancy user access secret
  oci session refresh --profile faoj >/dev/null 2>&1 || die "OCI session expired: oci session authenticate …"
  tenancy=$(oci iam compartment get --compartment-id "${E5_COMPARTMENT:-ocid1.compartment.oc1..aaaaaaaairylkjofkqqwzysmo2ies2ptax4qlmsotny2cwwxg2yitpg6g4lq}" \
    --query 'data."compartment-id"' --raw-output)
  user=$(oci iam user list --compartment-id "$tenancy" --all \
    --query 'data[?starts_with(name, `faoj-backup-reader`)].id | [0]' --raw-output 2>/dev/null || true)
  [ -n "$user" ] && [ "$user" != null ] || die "create user faoj-backup-reader in group faoj-backup-readers and a Customer Secret Key for it first"
  access=$(oci iam customer-secret-key list --user-id "$user" \
    --query 'sort_by(data[?"lifecycle-state"==`ACTIVE`], &"time-created")[-1].id' --raw-output)
  printf '%s' "$access" | grep -Eq '^[0-9a-f]{40}$' || die "faoj-backup-reader has no active Customer Secret Key"
  secret=$(python3 -c 'import getpass; print(getpass.getpass("backup-reader secret key …'"${access: -6}"' (hidden, then Enter): ").strip())')
  [ -n "$secret" ] || die "no secret typed"
  # The reader key replaces the writer key everywhere on this Mac.
  set_kv "$ROOT/env/db.secret.env" PGBACKREST_REPO1_S3_KEY "$access"
  set_kv "$ROOT/env/db.secret.env" PGBACKREST_REPO1_S3_KEY_SECRET "$secret"
  set_kv "$ROOT/env/backup.secret.env" AWS_ACCESS_KEY_ID "$access"
  set_kv "$ROOT/env/backup.secret.env" AWS_SECRET_ACCESS_KEY "$secret"
  # Q10: the weekly encrypted copy to the organisation's OneDrive uses the same reader key.
  local endpoint pull="$HOME/.config/faoj/backup-pull.env"
  endpoint=$(grep '^BACKUP_S3_ENDPOINT=' "$ROOT/env/compose.env" | cut -d= -f2-)
  mkdir -p "$(dirname "$pull")"
  ( umask 077; printf '%s\n' "S3_ENDPOINT=$endpoint" "S3_REGION=ap-singapore-1" \
      "S3_KEY=$access" "S3_SECRET=$secret" "SOURCE_ENV=production" \
      "DEST=$HOME/Library/CloudStorage/OneDrive-MetropolitanElectricityAuthority/faoj-backup-offsite" > "$pull" )
  chmod 600 "$pull"
  say "standby settings ready in $ROOT/env and $pull (reader key ...${access: -6})"
}

build() {
  [ -r "$ROOT/env/compose.env" ] || die "run setup first"
  docker inspect faoj-registry >/dev/null 2>&1 || docker run -d --restart unless-stopped \
    --name faoj-registry -p 127.0.0.1:5001:5000 "$REGISTRY_IMAGE" >/dev/null
  local commit db app
  commit=$(git -C "$REPO" rev-parse --short HEAD)$(git -C "$REPO" diff --quiet || echo -dirty)
  say "building $commit (arm64) into $REG"
  docker build -q -t "$REG/faoj/db:$commit" "$REPO/deploy/server/db" >/dev/null
  docker push -q "$REG/faoj/db:$commit" >/dev/null
  db=$REG/faoj/db@$(docker inspect -f '{{index .RepoDigests 0}}' "$REG/faoj/db:$commit" | cut -d@ -f2)
  docker build -q --build-arg RELEASE_COMMIT="$commit" --build-arg DB_IMAGE="$db" -t "$REG/faoj/app:$commit" "$REPO" >/dev/null
  docker push -q "$REG/faoj/app:$commit" >/dev/null
  app=$REG/faoj/app@$(docker inspect -f '{{index .RepoDigests 0}}' "$REG/faoj/app:$commit" | cut -d@ -f2)
  echo "$app" > "$ROOT/run/release-image"
  "$REPO/deploy/server/bin/faoj" install "$app"
}

start() {
  [ -e "$ROOT/releases/current" ] || die "run build first"
  if [ ! -s "$ROOT/data/postgres/pgdata/PG_VERSION" ]; then
    say "restoring production's newest backup as a standby (first start)"
    dc run --rm --no-deps -T --entrypoint "" db \
      gosu postgres pgbackrest --stanza=faoj restore --type=standby --log-level-console=warn
  fi
  dc up -d db
  say "standby running; 'deploy/standby.sh status' shows how far behind it is"
}

lag_seconds() {  # seconds since the last replayed transaction ("" when not in recovery)
  dc exec -T db gosu postgres psql -Atq -d postgres -c \
    "select case when pg_is_in_recovery() then coalesce(extract(epoch from now() - pg_last_xact_replay_timestamp())::int, -1) end" 2>/dev/null | tr -d ' '
}

status() {
  local lag; lag=$(lag_seconds || true)
  if [ -z "$lag" ]; then echo "database: not in recovery (promoted, or not running)"
  elif [ "$lag" = -1 ]; then echo "database: standby, nothing replayed yet"
  else echo "database: standby, $((lag / 60)) min $((lag % 60)) s behind production"; fi
  echo "files: last copy $(cat "$ROOT/run/files-at" 2>/dev/null || echo never)"
}

files() {
  say "copying private files and n8n data from the restic backup"
  rm -rf "$ROOT/restore/files"
  dc run --rm -T files-backup -o s3.bucket-lookup=path restore latest --host production --target /restore/files
  # The restic image (pinned in compose.yaml) has a shell: copy without another image.
  dc run --rm -T --no-deps -v "$ROOT/restore/files/backup:/from:ro" -v "$ROOT/data:/to" \
    --entrypoint sh files-backup -c 'cp -a /from/media/. /to/media/ 2>/dev/null; cp -a /from/n8n/. /to/n8n/ 2>/dev/null; true'
  rm -rf "$ROOT/restore/files"
  date '+%Y-%m-%d %H:%M' > "$ROOT/run/files-at"
}

check() {
  [ -e "$ROOT/releases/current" ] || exit 0
  [ -e "$ROOT/run/promoted" ] && exit 0  # after a failover this Mac is production
  dc up -d db >/dev/null 2>&1 || true
  local lag; lag=$(lag_seconds || true)
  echo "[$(date '+%m-%d %H:%M')] lag ${lag:-?}s" >> "$LOG"
  if [ -n "$lag" ] && [ "$lag" -gt 900 ]; then
    notify "เครื่องสำรอง ฝอจ. ตามไม่ทัน" "ข้อมูลบน Mac ช้ากว่า production $((lag / 60)) นาที ดู deploy/standby.sh status"
  fi
  local last; last=$(stat -f %m "$ROOT/run/files-at" 2>/dev/null || echo 0)
  if [ $(( $(date +%s) - last )) -ge 3600 ]; then files >> "$LOG" 2>&1 || true; fi
}

failover() {
  [ -e "$ROOT/releases/current" ] || die "no standby here"
  echo "Failover makes THIS MAC production: the standby is promoted and LINE moves here."
  echo "Do it only when E5 is really gone (health alert, RB01). Data written on E5 after the"
  echo "last replayed WAL (status shows how far behind) is not here."
  status
  read -r -p "Type FAILOVER to continue: " answer
  [ "$answer" = FAILOVER ] || die "cancelled"
  say "E5: stop its ngrok if it still answers (only one host may hold the LINE domain)"
  "$REPO/deploy/e5-move.sh" ssh 'sudo bash -c "cd /srv/faoj/releases/current && docker compose -p faoj -f compose.yaml --env-file /srv/faoj/env/compose.env --env-file release.env --profile ngrok stop ngrok; faoj maintenance on"' \
    2>/dev/null || say "E5 does not answer: going on"
  say "promote the standby"
  dc exec -T db gosu postgres psql -Atq -d postgres -c 'select pg_promote(true, 60)'
  touch "$ROOT/run/promoted"
  set_kv "$ROOT/env/compose.env" NGROK_DOMAIN "$NGROK_DOMAIN"
  say "start the whole stack here (faoj deploy) with ngrok"
  faoj deploy "$(cat "$ROOT/run/release-image")" --approval "failover to the Mac standby $(date '+%Y-%m-%d %H:%M') (ADR 0015 Q9)"
  notify "ฝอจ. ย้ายมาที่ Mac แล้ว" "LINE ชี้มาที่ Mac ผ่าน ngrok เว็บเจ้าหน้าที่ https://localhost:8443"
  say "this Mac is production now. Back up with: FAOJ_ROOT=$ROOT $ROOT/releases/current/bin/faoj backup dump"
}

mkdir -p "$ROOT/run"
case "${1:-}" in
  setup) setup;;
  reader-key) reader_key;;
  build) build;;
  start) start;;
  status) status;;
  files) files;;
  check) check;;
  failover) failover;;
  *) sed -n '2,21p' "$0"; exit 2;;
esac
