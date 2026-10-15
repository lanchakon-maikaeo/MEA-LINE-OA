#!/usr/bin/env bash
# Laptop rehearsal of the OCI host flow (M12): a local registry stands in for OCIR and an S3
# gateway for Object Storage; everything else (bootstrap, faoj deploy/backup/restore-drill) is
# the same code the hosts run. Synthetic data only; nothing leaves the machine.
#
#   FAOJ_ROOT=/path/to/scratch deploy/rehearsal.sh up        registry + S3 + host bootstrap
#   FAOJ_ROOT=… deploy/rehearsal.sh release                  build db+app images, print the digest
#   FAOJ_ROOT=… deploy/rehearsal.sh down                     remove containers (keeps $FAOJ_ROOT)
set -euo pipefail
: "${FAOJ_ROOT:?set FAOJ_ROOT to a scratch directory}"
REG=localhost:5001
S3_REGION=ap-singapore-1
REPO=$(cd "$(dirname "$0")/.." && pwd)
REGISTRY_IMAGE=registry:2@sha256:a3d8aaa63ed8681a604f1dea0aa03f100d5895b6a58ace528858a7b332415373
S3_IMAGE=versity/versitygw@sha256:30292fc2eeacc67a36993b01f7a7a5e3361a19cced0e80c1d71cfa2a4b0a2499

secret() { python3 -c 'import secrets; print(secrets.token_hex(20))'; }
running() { [ "$(docker inspect -f '{{.State.Running}}' "$1" 2>/dev/null)" = true ]; }
set_kv() {  # file key value — replace a KEY= line in an env file
  python3 - "$1" "$2" "$3" <<'PY'
import re, sys
path, key, value = sys.argv[1:4]
text = open(path).read()
text, n = re.subn(rf"^{re.escape(key)}=.*$", f"{key}={value}", text, flags=re.M)
if not n:
    text = text.rstrip("\n") + f"\n{key}={value}\n"
open(path, "w").write(text)
PY
}

up() {
  mkdir -p "$FAOJ_ROOT/tls/backup"
  # A named volume: the gateway keeps object metadata in xattrs, which macOS bind mounts lack.
  docker volume create faoj-rehearsal-s3 >/dev/null
  docker run --rm -v faoj-rehearsal-s3:/data --entrypoint /bin/sh "$REGISTRY_IMAGE" \
    -c 'mkdir -p /data/faoj-backup /data/faoj-files'

  # A helper left stopped (Docker restarted) is made again: its settings belong to one FAOJ_ROOT.
  running faoj-registry || { docker rm -f faoj-registry >/dev/null 2>&1 || true
    docker run -d --name faoj-registry -p 127.0.0.1:5001:5000 "$REGISTRY_IMAGE" >/dev/null; }
  local tls="$FAOJ_ROOT/tls/backup"
  if [ ! -s "$tls/s3.key" ]; then
    openssl req -x509 -new -nodes -newkey rsa:2048 -days 30 -subj "/CN=faoj-rehearsal-s3" \
      -addext "subjectAltName=DNS:host.docker.internal,DNS:localhost" \
      -keyout "$tls/s3.key" -out "$tls/ca.crt" 2>/dev/null
    chmod 644 "$tls/ca.crt" "$tls/s3.key"
  fi
  [ -s "$FAOJ_ROOT/s3.secret" ] || printf 'KEY=%s\nSECRET=%s\n' "$(secret)" "$(secret)" > "$FAOJ_ROOT/s3.secret"
  # shellcheck disable=SC1091
  . "$FAOJ_ROOT/s3.secret"
  running faoj-s3 || { docker rm -f faoj-s3 >/dev/null 2>&1 || true
    docker run -d --name faoj-s3 -p 127.0.0.1:443:7070 \
    -e ROOT_ACCESS_KEY="$KEY" -e ROOT_SECRET_KEY="$SECRET" -e VGW_REGION="$S3_REGION" \
    -v faoj-rehearsal-s3:/data -v "$tls:/tls:ro" "$S3_IMAGE" \
    --cert /tls/ca.crt --key /tls/s3.key posix /data >/dev/null; }
  FAOJ_ROOT=$FAOJ_ROOT "$REPO/deploy/server/bootstrap-host.sh" --env rehearsal --public-host localhost \
    --fallback-phone 02-000-0000 --fallback-form-url https://forms.example.invalid/faoj
  local env="$FAOJ_ROOT/env"
  set_kv "$env/compose.env" BACKUP_S3_ENDPOINT host.docker.internal
  set_kv "$env/compose.env" BACKUP_S3_REGION "$S3_REGION"
  set_kv "$env/compose.env" BACKUP_S3_VERIFY_TLS n
  set_kv "$env/compose.env" DRILL_SOURCE_ENV rehearsal
  set_kv "$env/db.secret.env" PGBACKREST_REPO1_S3_KEY "$KEY"
  set_kv "$env/db.secret.env" PGBACKREST_REPO1_S3_KEY_SECRET "$SECRET"
  set_kv "$env/backup.secret.env" RESTIC_REPOSITORY "s3:https://host.docker.internal/faoj-files/rehearsal"
  set_kv "$env/backup.secret.env" AWS_ACCESS_KEY_ID "$KEY"
  set_kv "$env/backup.secret.env" AWS_SECRET_ACCESS_KEY "$SECRET"
  # The drill reads "production" (here: this rehearsal host) with its own copy of the keys.
  local cipher restic
  cipher=$(grep '^PGBACKREST_REPO1_CIPHER_PASS=' "$env/db.secret.env" | cut -d= -f2-)
  restic=$(grep '^RESTIC_PASSWORD=' "$env/backup.secret.env" | cut -d= -f2-)
  ( umask 077; printf '%s\n' "PGBACKREST_REPO1_S3_KEY=$KEY" "PGBACKREST_REPO1_S3_KEY_SECRET=$SECRET" \
      "PGBACKREST_REPO1_CIPHER_PASS=$cipher" "RESTIC_REPOSITORY=s3:https://host.docker.internal/faoj-files/rehearsal" \
      "RESTIC_PASSWORD=$restic" "AWS_ACCESS_KEY_ID=$KEY" "AWS_SECRET_ACCESS_KEY=$SECRET" > "$env/drill.secret.env" )
  echo "rehearsal host ready at $FAOJ_ROOT (https://localhost:8443 after deploy)"
}

release() {
  local commit db app
  commit=$(git -C "$REPO" rev-parse --short HEAD)$(git -C "$REPO" diff --quiet || echo -dirty)
  docker build -q -t "$REG/faoj/db:rehearsal" "$REPO/deploy/server/db" >/dev/null
  docker push -q "$REG/faoj/db:rehearsal" >/dev/null
  db=$REG/faoj/db@$(docker inspect -f '{{index .RepoDigests 0}}' "$REG/faoj/db:rehearsal" | cut -d@ -f2)
  docker build -q --build-arg RELEASE_COMMIT="$commit" --build-arg DB_IMAGE="$db" \
    -t "$REG/faoj/app:rehearsal" "$REPO" >/dev/null
  docker push -q "$REG/faoj/app:rehearsal" >/dev/null
  app=$REG/faoj/app@$(docker inspect -f '{{index .RepoDigests 0}}' "$REG/faoj/app:rehearsal" | cut -d@ -f2)
  echo "$app"
}

down() {
  docker compose -p faoj down --remove-orphans 2>/dev/null || true
  docker rm -f faoj-drill-db faoj-drill-web faoj-s3 faoj-registry >/dev/null 2>&1 || true
  docker volume rm faoj-rehearsal-s3 >/dev/null 2>&1 || true
  docker network rm faoj-drill >/dev/null 2>&1 || true
}

case "${1:-}" in up) up;; release) release;; down) down;; *) sed -n '2,9p' "$0";; esac
