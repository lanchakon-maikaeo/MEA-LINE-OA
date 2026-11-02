#!/usr/bin/env bash
# The Mac as a warm standby for production (ADR 0015 Q9). Same `faoj`, Compose stack and images
# as the E5 host, under FAOJ_ROOT=~/faoj-standby. Normally only PostgreSQL runs here, in standby
# mode, replaying production's WAL from Object Storage (5-10 minutes behind) with the
# backup-READER key: this Mac can read production's backups but never write to them.
#
#   deploy/standby.sh setup      copy production's settings from its host (GCP since 2 Nov 2026;
#                                STANDBY_SOURCE=e5 for E5), keep this Mac's backup-reader key or
#                                ask the owner to type it
#   deploy/standby.sh rotate-reader-key   new reader key made by OCI, secret written to the
#                                settings unseen, old keys deleted, standby restarted
#   deploy/standby.sh build      build the arm64 db + app images here and unpack the release
#   deploy/standby.sh start      first time: restore the newest backup as a standby; then start
#   deploy/standby.sh status     in recovery? how far behind production? last files copy
#   deploy/standby.sh files      copy private files and n8n data from the restic backup
#   deploy/standby.sh check      launchd, every 5 minutes: keep it running, files daily, warn
#                                when it falls more than 15 minutes behind
#   deploy/standby.sh failover   a person decides production is gone: stop its ngrok if it
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
prod_ssh() {  # one command on production's host as ubuntu; stdout passes through clean
  if [ "${STANDBY_SOURCE:-gcp}" = e5 ]; then
    "$REPO/deploy/e5-move.sh" tunnel >&2; "$REPO/deploy/e5-move.sh" ssh "$@"
  else "$REPO/deploy/gcp-move.sh" ssh "$@"; fi
}
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
  # The backup-reader key already here survives a new copy (it lives only on this Mac).
  local old="" pair
  if [ -r "$ROOT/env/db.secret.env" ]; then
    old="$ROOT/env.before-$(date +%Y%m%d-%H%M%S)"; cp -a "$ROOT/env" "$old"
  fi
  say "copying production's settings and database TLS from ${STANDBY_SOURCE:-gcp}"
  prod_ssh 'sudo tar -C /srv/faoj -czf - env tls/db' | tar -C "$ROOT" -xzf -
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
  rm -f "$ROOT/env/drill.secret.env"  # production's writer key: never on this Mac
  if [ -n "$old" ] && grep -q '^AWS_ACCESS_KEY_ID=.' "$old/backup.secret.env" 2>/dev/null; then
    for pair in db.secret.env:PGBACKREST_REPO1_S3_KEY db.secret.env:PGBACKREST_REPO1_S3_KEY_SECRET \
        backup.secret.env:AWS_ACCESS_KEY_ID backup.secret.env:AWS_SECRET_ACCESS_KEY; do
      set_kv "$ROOT/env/${pair%%:*}" "${pair#*:}" "$(grep "^${pair#*:}=" "$old/${pair%%:*}" | tail -1 | cut -d= -f2-)"
    done
    say "kept this Mac's backup-reader key; the copy before this setup is $old"
  else
    reader_key
  fi
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
  # An OCI Customer Secret Key's secret is 44 base64 characters; anything else was mispasted.
  printf '%s' "$secret" | grep -Eq '^[A-Za-z0-9+/]{43}=$' \
    || die "that is not a secret key (${#secret} characters; OCI's are 44 ending in =): paste it once, as shown when it was generated"
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

# A new backup-reader key without the console: OCI makes it, its secret goes straight into the
# standby and backup-pull settings (never shown), the old keys are deleted, the standby picks
# the new one up. For a key that leaked (15 Oct 2026) or a routine change.
rotate_reader_key() {
  local tenancy user
  oci session refresh --profile faoj >/dev/null 2>&1 || die "OCI session expired: oci session authenticate …"
  tenancy=$(oci iam compartment get --compartment-id "${E5_COMPARTMENT:-ocid1.compartment.oc1..aaaaaaaairylkjofkqqwzysmo2ies2ptax4qlmsotny2cwwxg2yitpg6g4lq}" \
    --query 'data."compartment-id"' --raw-output)
  user=$(oci iam user list --compartment-id "$tenancy" --all \
    --query 'data[?starts_with(name, `faoj-backup-reader`)].id | [0]' --raw-output)
  [ -n "$user" ] && [ "$user" != null ] || die "no faoj-backup-reader user"
  local old count
  old=$(oci iam customer-secret-key list --user-id "$user" --query 'data[].id' --raw-output | tr -d '[]", ' | grep -v '^$' || true)
  count=$(printf '%s\n' "$old" | grep -c . || true)
  [ "$count" -lt 2 ] || die "faoj-backup-reader already has 2 keys (OCI's limit): delete one first"
  say "creating a new Customer Secret Key (its secret is written to the settings, not shown)"
  oci iam customer-secret-key create --user-id "$user" --display-name "faoj-standby-$(date +%Y%m%d-%H%M)" --output json \
    | ROOT="$ROOT" python3 -c '
import json, os, re, sys
data = json.load(sys.stdin)["data"]
access, secret = data["id"], data["key"]
assert re.fullmatch(r"[0-9a-f]{40}", access) and re.fullmatch(r"[A-Za-z0-9+/]{43}=", secret)
root = os.environ["ROOT"]
home = os.path.expanduser("~")
targets = {
    f"{root}/env/db.secret.env": {"PGBACKREST_REPO1_S3_KEY": access, "PGBACKREST_REPO1_S3_KEY_SECRET": secret},
    f"{root}/env/backup.secret.env": {"AWS_ACCESS_KEY_ID": access, "AWS_SECRET_ACCESS_KEY": secret},
    f"{home}/.config/faoj/backup-pull.env": {"S3_KEY": access, "S3_SECRET": secret},
}
for path, values in targets.items():
    if not os.path.exists(path):
        continue
    text = open(path).read()
    for key, value in values.items():
        line = f"{key}={value}"
        text, n = re.subn(rf"^{re.escape(key)}=.*$", lambda m: line, text, flags=re.M)
        if not n:
            text = text.rstrip("\n") + "\n" + line + "\n"
    mode = os.stat(path).st_mode
    open(path, "w").write(text)
    os.chmod(path, mode)
print(f"new key ...{access[-6:]} written to the standby and backup-pull settings")
'
  for id in $old; do
    oci iam customer-secret-key delete --user-id "$user" --customer-secret-key-id "$id" --force >/dev/null
    say "deleted the old key ...${id: -6}"
  done
  if [ -e "$ROOT/run/standby-restored" ]; then
    dc up -d db >/dev/null  # recreated with the new key in its environment
    say "standby restarted with the new key; OCI may take ~5 minutes to accept it (WAL replay resumes by itself)"
  fi
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
  if [ ! -e "$ROOT/run/standby-restored" ] && [ -s "$ROOT/data/postgres/pgdata/PG_VERSION" ]; then
    # A cluster without the restore marker was made by the image's initdb (the database was
    # started before any restore). Drop it only when it holds no tables of the app.
    dc up -d --wait db >/dev/null 2>&1 || true  # psql before it is ready answers nothing
    local tables
    tables=$(dc exec -T db gosu postgres psql -Atq -d faoj -c \
      "select count(*) from pg_tables where schemaname = 'public'" 2>/dev/null || echo unknown)
    [ "$tables" = 0 ] || die "a database that is not a restored standby holds $tables tables in $ROOT/data/postgres: look before removing it"
    say "removing the empty cluster that initdb made before the first restore"
    dc stop db >/dev/null; dc rm -f db >/dev/null
    rm -rf "$ROOT/data/postgres/pgdata"
  fi
  if [ ! -s "$ROOT/data/postgres/pgdata/PG_VERSION" ]; then
    say "restoring production's newest backup as a standby (first start)"
    dc run --rm --no-deps -T --entrypoint "" db \
      gosu postgres pgbackrest --stanza=faoj restore --type=standby --log-level-console=warn
    # Object Storage allows 50,000 requests a month (Always Free), shared with production's
    # backups: ask for new WAL every 5 minutes, not every 5 seconds (about 17k a month).
    echo "wal_retrieve_retry_interval = '5min'" >> "$ROOT/data/postgres/pgdata/postgresql.auto.conf"
    touch "$ROOT/run/standby-restored"
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
  # Explicit checks: called as `files || true` from check, set -e does not apply in here.
  # --no-lock: the reader key cannot write restic's lock file (it would retry forever).
  local host; host=$(sed -n 's/^BACKUP_HOST_TAG=//p' "$ROOT/env/compose.env")
  dc run --rm -T files-backup -o s3.bucket-lookup=path --no-lock restore latest --host "${host:-production}" \
    --target /restore/files || { say "no files copied (restic failed)"; return 1; }
  # The restic image (pinned in compose.yaml) has a shell: copy without another image.
  dc run --rm -T --no-deps -v "$ROOT/restore/files/backup:/from:ro" -v "$ROOT/data:/to" \
    --entrypoint sh files-backup -c 'cp -a /from/media/. /to/media/ 2>/dev/null; cp -a /from/n8n/. /to/n8n/ 2>/dev/null; true'
  rm -rf "$ROOT/restore/files"
  date '+%Y-%m-%d %H:%M' > "$ROOT/run/files-at"
}

check() {
  [ -e "$ROOT/releases/current" ] || exit 0
  [ -e "$ROOT/run/promoted" ] && exit 0  # after a failover this Mac is production
  # Never start the database before a restore: the image would initdb an empty primary.
  [ -e "$ROOT/run/standby-restored" ] || exit 0
  dc up -d db >/dev/null 2>&1 || true
  local lag; lag=$(lag_seconds || true)
  echo "[$(date '+%m-%d %H:%M')] lag ${lag:-?}s" >> "$LOG"
  if [ -n "$lag" ] && [ "$lag" -gt 900 ]; then
    notify "เครื่องสำรอง ฝอจ. ตามไม่ทัน" "ข้อมูลบน Mac ช้ากว่า production $((lag / 60)) นาที ดู deploy/standby.sh status"
  fi
  local last; last=$(stat -f %m "$ROOT/run/files-at" 2>/dev/null || echo 0)
  # Daily, not hourly: each restic restore costs Object Storage requests (Always Free budget).
  if [ $(( $(date +%s) - last )) -ge 86400 ]; then files >> "$LOG" 2>&1 || true; fi
}

failover() {
  [ -e "$ROOT/releases/current" ] || die "no standby here"
  echo "Failover makes THIS MAC production: the standby is promoted and LINE moves here."
  echo "Do it only when production is really gone (health alert, RB01). Data written there after the"
  echo "last replayed WAL (status shows how far behind) is not here."
  status
  read -r -p "Type FAILOVER to continue: " answer
  [ "$answer" = FAILOVER ] || die "cancelled"
  say "production (${STANDBY_SOURCE:-gcp}): stop its ngrok if it still answers (only one host may hold the LINE domain)"
  prod_ssh 'sudo bash -c "cd /srv/faoj/releases/current && docker compose -p faoj -f compose.yaml --env-file /srv/faoj/env/compose.env --env-file release.env --profile ngrok stop ngrok; faoj maintenance on"' \
    2>/dev/null || say "production does not answer: going on"
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
  rotate-reader-key) rotate_reader_key;;
  build) build;;
  start) start;;
  status) status;;
  files) files;;
  check) check;;
  failover) failover;;
  *) sed -n '2,23p' "$0"; exit 2;;
esac
