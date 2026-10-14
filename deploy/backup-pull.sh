#!/usr/bin/env bash
# make backup-pull (M12 Q13 a): copy production's encrypted backups out of OCI into a folder the
# organisation keeps (the owner's OneDrive), weekly via launchd. Files stay encrypted (pgBackRest
# repository cipher, restic); the keys to read them are kept apart (Vault + the owner's copy).
#
# Config: ~/.config/faoj/backup-pull.env (0600), with the faoj-backup-readers user's S3 key:
#   S3_ENDPOINT=<namespace>.compat.objectstorage.ap-singapore-1.oraclecloud.com
#   S3_REGION=ap-singapore-1
#   S3_KEY=…   S3_SECRET=…
#   DEST=$HOME/Library/CloudStorage/OneDrive-MetropolitanElectricityAuthority/faoj-backup-offsite
#   SOURCE_ENV=production        (repository path inside the buckets)
#   S3_CA_FILE=                  (rehearsals only: CA of a local S3 stand-in)
set -euo pipefail
CONFIG=${FAOJ_BACKUP_PULL_CONFIG:-$HOME/.config/faoj/backup-pull.env}
RCLONE=rclone/rclone:1.71@sha256:3103526c506266a9ecdf064efe99bf3677d92ef6407af124d8c56b4f49cbaa51
[ -r "$CONFIG" ] || { echo "missing $CONFIG (see the header of this script)" >&2; exit 1; }
perm=$(stat -f %Lp "$CONFIG" 2>/dev/null || stat -c %a "$CONFIG")
[ "$perm" = 600 ] || { echo "$CONFIG must be mode 600" >&2; exit 1; }
# shellcheck disable=SC1090
. "$CONFIG"
: "${S3_ENDPOINT:?}" "${S3_REGION:?}" "${S3_KEY:?}" "${S3_SECRET:?}" "${DEST:?}"
SOURCE_ENV=${SOURCE_ENV:-production}
# A launchd job may not write into OneDrive (macOS privacy): say so instead of failing quietly.
if ! mkdir -p "$DEST/pgbackrest" "$DEST/restic" 2>/dev/null; then
  osascript -e 'display notification "macOS ไม่ให้งานเบื้องหลังเขียนลง OneDrive รัน make backup-pull ใน Terminal แทน" with title "สำเนา backup รายสัปดาห์ ฝอจ."' >/dev/null 2>&1 || true
  echo "cannot write $DEST from here: run 'make backup-pull' in Terminal" >&2
  exit 1
fi
ca=(); [ -n "${S3_CA_FILE:-}" ] && ca=(-v "$S3_CA_FILE:/ca.crt:ro" -e RCLONE_CA_CERT=/ca.crt)
run() {  # bucket/path → local folder; sync = an exact copy, older versions are kept by the source
  docker run --rm "${ca[@]+"${ca[@]}"}" \
    -e RCLONE_CONFIG_OCI_TYPE=s3 -e RCLONE_CONFIG_OCI_PROVIDER=Other \
    -e RCLONE_CONFIG_OCI_ENDPOINT="https://$S3_ENDPOINT" -e RCLONE_CONFIG_OCI_REGION="$S3_REGION" \
    -e RCLONE_CONFIG_OCI_ACCESS_KEY_ID="$S3_KEY" -e RCLONE_CONFIG_OCI_SECRET_ACCESS_KEY="$S3_SECRET" \
    -e RCLONE_CONFIG_OCI_FORCE_PATH_STYLE=true \
    -v "$2:/dest" "$RCLONE" sync "oci:$1" /dest --fast-list --transfers 2 --stats-one-line -v 2>&1 | tail -n 3
}
started=$(date -u +%Y-%m-%dT%H:%M:%SZ)
run "faoj-backup/pgbackrest/$SOURCE_ENV" "$DEST/pgbackrest"
run "faoj-files/$SOURCE_ENV" "$DEST/restic"
printf '{"pulled_at": "%s", "source_env": "%s"}\n' "$started" "$SOURCE_ENV" > "$DEST/last-pull.json"
echo "backup-pull done: $DEST"
