#!/usr/bin/env bash
# Production from faoj-prod-e5 to faoj-prod-gcp (Docs/release/gcp-move). Run from the repository on
# the Mac, logged in with `gcloud auth login`. Phase 1 (this file so far) brings up an EMPTY
# stack: no keys, no LINE webhook, no data; E5 keeps serving. Each step can be re-run.
#
#   deploy/gcp-move.sh prepare        Docker, unattended upgrades, the 150 GB disk at /srv/faoj
#   deploy/gcp-move.sh pull-token [USER]  OCI user faoj-gcp-pull in faoj-deployers (pull only), a new
#                                     auth token piped straight into docker login on the host
#   deploy/gcp-move.sh ocir-login     or: the owner types an existing pull-only token (hidden)
#   deploy/gcp-move.sh bootstrap IMG  bootstrap-host.sh from the release image (its own secrets)
#   deploy/gcp-move.sh skeleton-config  dump backups, no WAL archive, metrics to stdout, no ngrok
#   deploy/gcp-move.sh deploy IMG     `faoj deploy` the Release workflow's OCIR digest, detached
#   deploy/gcp-move.sh status         faoj status and /health/ready/
#   deploy/gcp-move.sh ssh [cmd]      a shell (or one command) on the host through IAP
#
# Phase 2 (needs `deploy/e5-move.sh tunnel` access; E5 runs GCP's checks in one-off containers):
#   deploy/gcp-move.sh escrow-check   hashes of the keys the data needs, to compare with the
#                                     owner's password manager
#   deploy/gcp-move.sh dry-run        E5's data into a throwaway database on GCP, deep compare
#   deploy/gcp-move.sh move           the move itself (5-move.md); asks YES before E5 stops
#   deploy/gcp-move.sh back-to-e5     undo a move that stopped before GCP opened
#   deploy/gcp-move.sh reverse-move   after GCP took work: the move back to E5, same checks
#
# Deploy only images the Release workflow built and scanned (ap-singapore-1.ocir.io/…@sha256),
# never one built on a host.
set -euo pipefail
umask 077

PROJECT=${GCP_PROJECT:-mea-ded-center-line-oa}
ZONE=${GCP_ZONE:-asia-southeast3-a}
VM=${GCP_VM:-faoj-prod-gcp}
KEY=${GCP_KEY:-$HOME/.ssh/faoj_gcp_ed25519}
PUBLIC_HOST=${GCP_PUBLIC_HOST:-34-15-129-131.sslip.io}
REGISTRY=ap-singapore-1.ocir.io
NAMESPACE=axif86tn0tbj
APPROVAL=${GCP_APPROVAL:-"owner in chat 2 Nov 2026: empty skeleton on faoj-prod-gcp, no data (gcp-move phase 1)"}

say() { echo "[$(date +%H:%M:%S)] $*" >&2; }
die() { echo "gcp-move: $*" >&2; exit 1; }
on_gcp() {
  gcloud compute ssh "ubuntu@$VM" --project "$PROJECT" --zone "$ZONE" --tunnel-through-iap \
    --ssh-key-file "$KEY" --quiet --command "$*" -- -o LogLevel=ERROR 2>&1 \
    | grep -v -e 'consider installing NumPy' -e 'increasing_the_tcp_upload_bandwidth' -e '^$' || true
}
image_arg() {
  case "${1:-}" in "$REGISTRY"/*@sha256:*) ;; *) die "give the Release workflow's app image ($REGISTRY/…@sha256:…)";; esac
}

prepare() {
  say "packages, data disk at /srv/faoj, /etc/faoj/host.env"
  on_gcp 'set -e
    sudo apt-get update -qq
    sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq docker.io docker-compose-v2 python3 openssl unattended-upgrades >/dev/null
    sudo systemctl enable --now docker unattended-upgrades >/dev/null
    dev=/dev/disk/by-id/google-faoj-data
    sudo blkid "$dev" >/dev/null 2>&1 || sudo mkfs.ext4 -q -L faojdata "$dev"
    sudo mkdir -p /srv/faoj /etc/faoj
    grep -q LABEL=faojdata /etc/fstab || echo "LABEL=faojdata /srv/faoj ext4 defaults,nofail 0 2" | sudo tee -a /etc/fstab >/dev/null
    mountpoint -q /srv/faoj || sudo mount /srv/faoj
    printf "FAOJ_ENV=production\nFAOJ_HOST=faoj-prod-gcp\nFAOJ_DATA_VOLUME=yes\n" | sudo tee /etc/faoj/host.env >/dev/null  # pragma: allowlist secret
    findmnt -n -o SOURCE,SIZE /srv/faoj; docker --version; free -g | sed -n 2p'
}

ocir_login() {
  local user token
  read -r -p "OCIR username [$NAMESPACE/<user in faoj-deployers>]: " user
  case "$user" in */*) ;; *) user="$NAMESPACE/$user";; esac
  read -r -s -p "auth token (hidden): " token; echo >&2
  [ -n "$token" ] || die "no token"
  # The token goes over ssh stdin to docker on the host; never in argv, a file here or the screen.
  printf '%s' "$token" | gcloud compute ssh "ubuntu@$VM" --project "$PROJECT" --zone "$ZONE" \
    --tunnel-through-iap --ssh-key-file "$KEY" --quiet \
    --command "sudo docker login $REGISTRY -u '$user' --password-stdin >/dev/null && echo 'OCIR login saved'" \
    -- -o LogLevel=ERROR 2>&1 | grep -v -e NumPy -e tcp_upload -e '^$'
}

# The host saves the login; the token passes from `oci` through ssh stdin and is never printed,
# written on the Mac or put in argv (like ci-ocir-setup.sh). A new token can take minutes to work.
login_on_host() {  # user; token on stdin
  gcloud compute ssh "ubuntu@$VM" --project "$PROJECT" --zone "$ZONE" --tunnel-through-iap \
    --ssh-key-file "$KEY" --quiet -- -o LogLevel=ERROR \
    "t=\$(cat); for i in \$(seq 1 15); do printf %s \"\$t\" | sudo docker login $REGISTRY -u '$1' --password-stdin >/dev/null 2>&1 && { echo 'OCIR login saved'; exit 0; }; sleep 20; done; echo 'OCIR login failed'; exit 1" \
    2>&1 | grep -v -e NumPy -e tcp_upload -e '^$'
}

pull_token() {
  export OCI_CLI_PROFILE=faoj OCI_CLI_AUTH=security_token PYTHONWARNINGS=ignore
  oci session refresh --profile faoj >/dev/null 2>&1 || die "OCI session expired: oci session authenticate --region ap-singapore-1 --tenancy-name MEAFlow --profile-name faoj"
  local tenancy name=${1:-faoj-gcp-pull} user_id tokens
  tenancy=$(oci iam compartment get --compartment-id "${E5_COMPARTMENT:-ocid1.compartment.oc1..aaaaaaaairylkjofkqqwzysmo2ies2ptax4qlmsotny2cwwxg2yitpg6g4lq}" \
    --query 'data."compartment-id"' --raw-output)
  user_id=$(oci iam user list --compartment-id "$tenancy" --all --query "data[?name=='$name'].id | [0]" --raw-output)
  if [ "$name" != faoj-gcp-pull ]; then
    # An existing user (2 Nov 2026 the owner chose faoj-ci): its groups and capabilities stay as they are.
    [ -n "$user_id" ] && [ "$user_id" != null ] || die "no OCI user $name"
  elif [ -z "$user_id" ] || [ "$user_id" = null ]; then
    local email
    read -r -p "E-mail YOU own for the new OCI user faoj-gcp-pull (e.g. yourname+gcp@gmail.com): " email
    [ -n "$email" ] || die "an e-mail is needed for a user in the Default identity domain"
    say "creating OCI user faoj-gcp-pull"
    user_id=$(oci iam user create --compartment-id "$tenancy" --name faoj-gcp-pull --email "$email" \
      --description "faoj-prod-gcp: pull faoj/* from OCIR only (gcp-move)" --query data.id --raw-output)
  fi
  [ "$name" = faoj-gcp-pull ] && { set_up_pull_user "$tenancy" "$user_id" || return 1; }
  tokens=$(oci iam auth-token list --user-id "$user_id" --query 'length(data[?"lifecycle-state"==`ACTIVE`])' --raw-output)
  [ "${tokens:-0}" -lt 2 ] || die "$name already has 2 auth tokens (OCI's limit): delete an old one first"
  say "new auth token for $name straight into docker login on $VM (not shown; retries while OCIR accepts it)"
  oci iam auth-token create --user-id "$user_id" --description "faoj-prod-gcp $(date +%Y-%m-%d)" \
      --query data.token --raw-output | tr -d '\n' | login_on_host "$NAMESPACE/$name"
}

set_up_pull_user() {  # tenancy user_id: auth tokens only, group faoj-deployers (read repos)
  local tenancy=$1 user_id=$2 group_id
  oci iam user update-user-capabilities --user-id "$user_id" --can-use-auth-tokens true \
    --can-use-console-password false --can-use-api-keys false --can-use-customer-secret-keys false \
    --can-use-smtp-credentials false --can-use-db-credentials false \
    --can-use-o-auth2-client-credentials false >/dev/null
  group_id=$(oci iam group list --compartment-id "$tenancy" --all --query 'data[?name==`faoj-deployers`].id | [0]' --raw-output)
  [ -n "$group_id" ] && [ "$group_id" != null ] || die "group faoj-deployers is missing (Terraform iam.tf)"
  if ! oci iam user list-groups --user-id "$user_id" --compartment-id "$tenancy" --query 'data[].name' --raw-output | grep -q '"faoj-deployers"'; then
    say "adding faoj-gcp-pull to faoj-deployers (read repos only)"
    oci iam group add-user --group-id "$group_id" --user-id "$user_id" >/dev/null
  fi
}

bootstrap() {
  image_arg "${1:-}"
  say "bootstrap-host.sh from $1 (secrets made on the host, never shown)"
  on_gcp "set -e
    sudo docker pull -q '$1' >/dev/null
    tmp=\$(mktemp -d); cid=\$(sudo docker create '$1')
    sudo docker cp \"\$cid:/app/deploy/server/.\" \"\$tmp/\" >/dev/null; sudo docker rm \"\$cid\" >/dev/null
    sudo FAOJ_ROOT=/srv/faoj \"\$tmp/bootstrap-host.sh\" --env production --public-host $PUBLIC_HOST
    sudo rm -rf \"\$tmp\""
}

skeleton_config() {
  say "empty skeleton: local dumps only, no archive, metrics to stdout, no ngrok"
  on_gcp 'set -e
    f=/srv/faoj/env/compose.env
    for kv in BACKUP_MODE=dump PG_ARCHIVE_MODE=off METRICS_SINK=stdout NGROK_DOMAIN=; do
      k=${kv%%=*}; sudo sed -i "s|^$k=.*|$kv|" "$f"; sudo grep -q "^$k=" "$f" || echo "$kv" | sudo tee -a "$f" >/dev/null
    done
    # No Object Storage key and no OCI instance principal here yet (phase 2 turns backups on).
    sudo systemctl disable --now faoj-backup-full.timer faoj-backup-diff.timer faoj-backup-files.timer faoj-quota-check.timer 2>/dev/null || true
    sudo grep -E "^(BACKUP_MODE|PG_ARCHIVE_MODE|METRICS_SINK|NGROK_DOMAIN|PUBLIC_HOST)=" "$f"
    systemctl list-timers "faoj-*" --no-pager --no-legend | awk "{print \$(NF-1)}"'
}

deploy() {
  image_arg "${1:-}"
  local sha=${1##*@sha256:}; sha=${sha:0:12}
  say "faoj deploy $sha (detached; log /srv/faoj/run/deploy-$sha.log)"
  # systemd-run keeps the deploy alive when the IAP session drops (1 Nov 2026 on E5).
  on_gcp "set -e
    # The new release's own faoj runs the deploy, so a fix in faoj applies to this deploy too.
    tmp=/srv/faoj/run/bundle-$sha; sudo mkdir -p /srv/faoj/run
    sudo rm -rf \$tmp; sudo mkdir -p \$tmp; cid=\$(sudo docker create '$1')
    sudo docker cp \"\$cid:/app/deploy/server/.\" \$tmp/ >/dev/null; sudo docker rm \"\$cid\" >/dev/null
    bin=\$tmp/bin/faoj
    sudo systemctl reset-failed faoj-deploy-$sha 2>/dev/null || true
    sudo systemd-run --unit faoj-deploy-$sha --setenv FAOJ_ROOT=/srv/faoj \
      --property StandardOutput=file:/srv/faoj/run/deploy-$sha.log \
      --property StandardError=file:/srv/faoj/run/deploy-$sha.log \
      \$bin deploy '$1' --approval '$APPROVAL'"
  say "follow: deploy/gcp-move.sh ssh 'sudo tail -f /srv/faoj/run/deploy-$sha.log'"
}

status() {
  on_gcp 'sudo faoj status 2>&1 | tail -20; echo; sudo faoj maintenance status'
  curl -s -m 10 "https://$PUBLIC_HOST/health/ready/" || echo "(no answer from https://$PUBLIC_HOST/)"
  echo
}

# --- Phase 2: keys, data and webhook in one window (Docs/release/gcp-move/5-move.md) ----------
# E5 is read-only for this script (owner, 2 Nov 2026): manifests, pg_dump and tar stream out of it
# through this Mac. Only `move` writes there (maintenance, read-only database, stopped services),
# after the owner types YES, and `back-to-e5` undoes exactly that.

REPO=$(cd "$(dirname "$0")/.." && pwd)
STATE=${GCP_STATE:-$HOME/.config/faoj/gcp}
NGROK_DOMAIN=${E5_NGROK_DOMAIN:-crabmeat-lake-lunar.ngrok-free.dev}
OPENED=/srv/faoj/run/gcp-opened  # on GCP: from here on, going back means moving data back

REMOTE_LIB='set -euo pipefail
rel=$(readlink -f /srv/faoj/releases/current 2>/dev/null || true)
dc() { docker compose -p faoj -f "$rel/compose.yaml" --env-file /srv/faoj/env/compose.env \
  --env-file "$rel/release.env" --profile clamav "$@"; }
psql_faoj() { dc exec -T -u postgres db psql -qAt -v ON_ERROR_STOP=1 -d faoj "$@"; }
manage() { dc run --rm -T migrate /app/.venv/bin/python manage.py "$@"; }
# On E5: the same migrate service (env, network, media, TLS) with the GCP image; db untouched.
tool() { APP_IMAGE=$TOOL dc run --rm -T --no-deps migrate /app/.venv/bin/python manage.py "$@"; }
set_kv() { python3 - "$1" "$2" "$3" <<"PY"
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
'

on_e5() { "$REPO/deploy/e5-move.sh" ssh "$@"; }
# A script on stdin runs as root, its own stdin /dev/null (docker compose would eat the script).
e5_root() { on_e5 "f=\$(mktemp) && cat > \$f && sudo bash \$f $* < /dev/null; rc=\$?; rm -f \$f; exit \$rc"; }
gcp_cmd() {  # one command line on GCP; stdin, stdout and the exit code pass through
  gcloud compute ssh "ubuntu@$VM" --project "$PROJECT" --zone "$ZONE" --tunnel-through-iap \
    --ssh-key-file "$KEY" --quiet --command "$1" -- -o LogLevel=ERROR \
    2> >(grep -v -e NumPy -e tcp_upload -e '^WARNING' -e '^$' >&2)
}
gcp_root() { gcp_cmd "f=\$(mktemp) && cat > \$f && sudo bash \$f $* < /dev/null; rc=\$?; rm -f \$f; exit \$rc"; }
lib() { echo "$REMOTE_LIB"; cat; }
# From the terminal itself: ssh before it may have read (and dropped) piped stdin.
confirm_yes() {
  local answer=${GCP_MOVE_ANSWER:-}
  [ -n "$answer" ] || read -r -p "Type YES to start: " answer < /dev/tty
  [ "$answer" = YES ] || die "not started"
}

tunnels() {
  "$REPO/deploy/e5-move.sh" tunnel
  gcp_cmd true || die "no IAP ssh to $VM (gcloud auth login?)"
}

# The checks (move_manifest --deep, decrypt_check) are GCP's release. E5 runs them in one-off
# containers of that image (owner, 2 Nov 2026): pulled onto E5, nothing there restarts or changes
# release, so E5 never goes down for this. Schema differences show as applied_migrations.
TOOL_IMAGE=""
tool_image() {
  TOOL_IMAGE=${GCP_TOOL_IMAGE:-$(gcp_cmd "sudo sed -n 's/^APP_IMAGE=//p' /srv/faoj/releases/current/release.env")}
  image_arg "$TOOL_IMAGE"
  say "E5 (pull only, services untouched): checks from ${TOOL_IMAGE##*@sha256:}"
  on_e5 "sudo /usr/local/sbin/faoj-ocir-login && sudo docker pull -q '$TOOL_IMAGE' >/dev/null" \
    || die "E5 could not pull $TOOL_IMAGE"
}

not_opened() {
  gcp_cmd "test ! -e $OPENED" || die "GCP has taken work since $(gcp_cmd "cat $OPENED"): going back means moving its data back, not switching"
}

escrow_check() {
  say "hashes (16 hex) of the keys the data needs, read on E5; compare: pbpaste | shasum -a 256 | cut -c1-16"
  lib <<'EOF' | e5_root
for pair in app.secret.env:AI_SECRET_KEY app.secret.env:PII_SECRET_KEY app.secret.env:DJANGO_SECRET_KEY \
    db.secret.env:PGBACKREST_REPO1_CIPHER_PASS backup.secret.env:RESTIC_PASSWORD; do
  f=${pair%%:*}; k=${pair#*:}
  v=$(grep "^$k=" "/srv/faoj/env/$f" | tail -1 | cut -d= -f2-)
  printf '%-30s %s\n' "$k" "$( [ -n "$v" ] && printf %s "$v" | sha256sum | cut -c1-16 || echo EMPTY)"
done
EOF
}

e5_manifest() {  # $1 local file
  { echo "TOOL='$TOOL_IMAGE'"; lib <<'EOF'; } | e5_root > "$1"
tool move_manifest --deep
EOF
  python3 -c 'import json,sys; json.load(open(sys.argv[1]))' "$1" || die "E5 manifest is not JSON (see $1)"
}

# pg_dump of faoj on E5 → this Mac (sha256) → $2 on GCP (sha256 again).
stream_dump() {  # db dest
  rm -f "$STATE/dump.sha"
  lib <<EOF | e5_root | tee >(shasum -a 256 | cut -d' ' -f1 > "$STATE/dump.sha") | gcp_cmd "sudo tee $2 >/dev/null"
dc exec -T -u postgres db pg_dump -Fc --no-owner --no-privileges $1
EOF
  local i=0 mac gcp
  until [ -s "$STATE/dump.sha" ]; do i=$((i + 1)); [ $i -lt 50 ] || die "no checksum of the $1 dump"; sleep 0.2; done
  mac=$(cat "$STATE/dump.sha"); gcp=$(gcp_cmd "sudo sha256sum $2 | cut -d' ' -f1")
  [ "$mac" = "$gcp" ] || die "dump $1 changed in transit ($mac vs $gcp)"
  say "dump $1: $(gcp_cmd "sudo du -h $2 | cut -f1"), sha256 ${mac:0:16} on both ends"
}

stream_media() {  # dest dir on GCP
  lib <<'EOF' | e5_root | gcp_cmd "sudo mkdir -p $1 && sudo tar -x --same-owner -p -C $1"
tar -C /srv/faoj/data/media -cf - .
EOF
}

dry_run() {
  tunnels; tool_image
  local ts d; ts=$(date +%Y%m%d-%H%M%S); d=/srv/faoj/restore/dry-$ts
  mkdir -p "$STATE"; chmod 700 "$STATE"
  say "E5 (read-only): decrypt_check with E5's own keys"
  { echo "TOOL='$TOOL_IMAGE'"; lib <<'EOF'; } | e5_root
tool decrypt_check
EOF
  say "E5: deep manifest, dump, deep manifest (E5 keeps serving, so a write in between means retry)"
  gcp_cmd "sudo install -d -m 700 $d"
  e5_manifest "$STATE/dry-source.json"
  stream_dump faoj "$d/app.dump"
  e5_manifest "$STATE/dry-after.json"
  cmp -s "$STATE/dry-source.json" "$STATE/dry-after.json" || { gcp_cmd "sudo rm -rf $d"; die "E5 changed during the dump (it is live): run dry-run again"; }
  say "E5 → GCP: private files"
  stream_media "$d/media"
  gcp_cmd "sudo tee $d/source.json >/dev/null" < "$STATE/dry-source.json"
  say "GCP: restore into a throwaway database and compare deep (the real database is not touched)"
  lib <<EOF | gcp_root
d=$d
APP_IMAGE='$TOOL_IMAGE'
pw=\$(python3 -c 'import secrets; print(secrets.token_hex(16))')
key=\$(python3 -c 'import secrets; print(secrets.token_urlsafe(64))')
trap 'docker rm -f faoj-dry-db >/dev/null 2>&1; docker network rm faoj-dry >/dev/null 2>&1; rm -rf "\$d"' EXIT
docker network create faoj-dry >/dev/null 2>&1 || true
# ci mode only talks to a demo database name on a local host name: mea_ci at "db".
docker run -d --name faoj-dry-db --network faoj-dry --network-alias db -e POSTGRES_PASSWORD=\$pw \
  -e POSTGRES_DB=mea_ci -v "\$d:/dry:ro" postgres:17-bookworm@sha256:3645570cccdfa447589da9f57dd740faa29b30938e861289a5574b6ca6b03826 >/dev/null
for i in \$(seq 1 30); do docker exec faoj-dry-db pg_isready -U postgres -d mea_ci >/dev/null 2>&1 && break; sleep 2; done
docker exec faoj-dry-db pg_restore -U postgres -d mea_ci --no-owner --no-privileges --exit-on-error /dry/app.dump
docker run --rm -i --network faoj-dry --entrypoint /app/.venv/bin/python \
  -e APP_ENV=ci -e DJANGO_SECRET_KEY=\$key -e ALLOWED_HOSTS=localhost -e PRIVATE_MEDIA_ROOT=/media \
  -e DATABASE_URL=postgresql://postgres:\$pw@db:5432/mea_ci -v "\$d/media:/media" \
  "\$APP_IMAGE" manage.py move_manifest --compare /dev/stdin < "\$d/source.json"
EOF
  rm -f "$STATE"/dry-*.json
  say "dry run passed: E5's data restores on GCP with nothing missing or changed (copies deleted)"
}

keys() {  # E5's env → GCP, merged by the rules in 5-move.md; names and hashes only
  local ts; ts=$(date +%Y%m%d-%H%M%S)
  say "E5 env → GCP /srv/faoj/restore/e5-env-$ts (root only), GCP env kept as env.before-$ts"
  lib <<'EOF' | e5_root | gcp_cmd "sudo install -d -m 700 /srv/faoj/restore/e5-env-$ts && sudo tar -x -C /srv/faoj/restore/e5-env-$ts"
tar -C /srv/faoj -cf - env
EOF
  lib <<EOF | gcp_root
src=/srv/faoj/restore/e5-env-$ts/env; dst=/srv/faoj/env
grep -qx FAOJ_HOST=faoj-prod-gcp /etc/faoj/host.env || { echo "not faoj-prod-gcp: refusing" >&2; exit 1; }
cp -a "\$dst" "/srv/faoj/env.before-$ts"
python3 - "\$src" "\$dst" <<'PY'
import hashlib, os, re, shutil, sys
src, dst = sys.argv[1:3]
# What stays this host's own: its database and n8n passwords, URLs, backup path and stanza key.
KEEP = {
    "app.secret.env": {"DATABASE_URL", "ORCHESTRATOR_TOKEN"},
    "db.secret.env": None,  # only the two S3 keys below come from E5
    "n8n.secret.env": "all",
    "migrate.secret.env": "all",
    "django.env": {"ALLOWED_HOSTS", "CSRF_TRUSTED_ORIGINS", "STAFF_WEB_BASE_URL"},
    "compose.env": {"FAOJ_ROOT", "PUBLIC_HOST", "METRICS_SINK", "NGROK_DOMAIN", "BACKUP_MODE",
                    "PG_ARCHIVE_MODE", "BACKUP_REPO_PATH", "DRILL_REPO_PATH"},
}
FROM_E5_ONLY = {"db.secret.env": {"PGBACKREST_REPO1_S3_KEY", "PGBACKREST_REPO1_S3_KEY_SECRET"}}
line = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$")
def read(path):
    out = {}
    if os.path.isfile(path):
        for l in open(path).read().splitlines():
            m = line.match(l)
            if m: out[m.group(1)] = m.group(2)
    return out
def h(v): return hashlib.sha256(v.encode()).hexdigest()[:16] if v else "EMPTY"
for name in sorted(os.listdir(src)):
    s, d = os.path.join(src, name), os.path.join(dst, name)
    if os.path.isdir(s):
        os.makedirs(d, exist_ok=True)
        for f in sorted(os.listdir(s)):
            shutil.copy2(os.path.join(s, f), os.path.join(d, f))
            print(f"{name}/{f}: copied from E5")
        continue
    keep = KEEP.get(name, set())
    if keep == "all":
        print(f"{name}: kept (this host's own)"); continue
    e5, here = read(s), read(d)
    take = {k for k in e5 if (k in FROM_E5_ONLY.get(name, ())) or (keep is not None and k not in keep)}
    if not os.path.exists(d):
        shutil.copy2(s, d); print(f"{name}: new file from E5"); continue
    text = open(d).read()
    for k in sorted(take):
        v = e5[k]
        text, n = re.subn(rf"^{re.escape(k)}=.*$", lambda m: f"{k}={v}", text, flags=re.M)
        if not n: text = text.rstrip("\n") + f"\n{k}={v}\n"
        print(f"{name}: {k} from E5 ({h(v)})")
    mode = os.stat(d).st_mode; open(d, "w").write(text); os.chmod(d, mode)
for name in sorted(os.listdir(dst)):
    path = os.path.join(dst, name)
    for k, v in (read(path).items() if os.path.isfile(path) else ()):
        if "168-107-70-170" in v or "168.107.70.170" in v:
            print(f"CHECK {name}: {k} still names E5's address")
PY
EOF
}

# Freeze and unfreeze run the same way on either host: e5_root or gcp_root as $1.
freeze_on() {
  lib <<'EOF' | "$1"
faoj maintenance on
dc --profile ngrok stop -t 60 web worker n8n ngrok >/dev/null
psql_faoj -c "ALTER DATABASE faoj SET default_transaction_read_only = on" \
  -c "SELECT count(pg_terminate_backend(pid)) FROM pg_stat_activity WHERE datname = 'faoj' AND pid <> pg_backend_pid()" >/dev/null
sleep 2
left=$(psql_faoj -c "SELECT count(*) FROM pg_stat_activity WHERE datname = 'faoj' AND backend_type = 'client backend' AND pid <> pg_backend_pid()")
[ "$left" = 0 ] || { echo "$(hostname) still has $left client sessions" >&2; exit 1; }
faoj backup incr
faoj backup files
echo "$(hostname) frozen: maintenance on, services stopped, database read-only, backup at the freeze point"
EOF
}

unfreeze_on() {
  lib <<'EOF' | "$1"
psql_faoj -c "ALTER DATABASE faoj RESET default_transaction_read_only"
dc --profile ngrok up -d web worker n8n ngrok >/dev/null
faoj maintenance off
echo "$(hostname) serves again"
EOF
}
e5_freeze() { freeze_on e5_root; }
e5_unfreeze() { unfreeze_on e5_root; }

move() {
  tunnels; not_opened; tool_image
  local ts d; ts=$(date +%Y%m%d-%H%M%S); d=/srv/faoj/restore/move-$ts
  mkdir -p "$STATE/move-$ts"; chmod 700 "$STATE" "$STATE/move-$ts"
  echo "This stops E5 (maintenance, services, read-only database) and moves keys, data and LINE to GCP." >&2
  confirm_yes
  say "GCP: maintenance on, stop web/worker/n8n"
  lib <<'EOF' | gcp_root
faoj maintenance on; dc stop -t 60 web worker n8n >/dev/null
EOF
  say "E5: freeze"
  e5_freeze || { e5_unfreeze; die "freeze failed; E5 serves again, nothing moved"; }
  set +e
  (
    set -e
    gcp_cmd "sudo install -d -m 700 $d"
    e5_manifest "$STATE/move-$ts/source.json"
    stream_dump faoj "$d/app.dump"
    stream_dump n8n "$d/n8n.dump"   # kept as evidence; n8n's workflows come with the release
    e5_manifest "$STATE/move-$ts/after.json"
    cmp -s "$STATE/move-$ts/source.json" "$STATE/move-$ts/after.json" || die "E5 changed while frozen"
    gcp_cmd "sudo tee $d/source.json >/dev/null" < "$STATE/move-$ts/source.json"
    say "E5 → GCP: private files (the skeleton's folder is kept as media.skeleton-$ts)"
    gcp_cmd "sudo mv /srv/faoj/data/media /srv/faoj/data/media.skeleton-$ts && sudo install -d -o 10001 -g 10001 -m 700 /srv/faoj/data/media"
    stream_media /srv/faoj/data/media
    keys
    say "GCP: restore, migrate, deep compare, decrypt_check"
    lib <<EOF | gcp_root
grep -qx FAOJ_HOST=faoj-prod-gcp /etc/faoj/host.env || { echo "not faoj-prod-gcp: refusing to drop anything" >&2; exit 1; }
psql_faoj -c 'DROP SCHEMA public CASCADE' -c 'CREATE SCHEMA public AUTHORIZATION faoj_owner'
dc exec -T -u postgres db pg_restore -d faoj --no-owner --no-privileges --role=faoj_owner --exit-on-error < $d/app.dump
dc run --rm -T migrate >/dev/null
manage move_manifest --compare /dev/stdin < $d/source.json
manage decrypt_check
EOF
  )
  local rc=$?; set -e
  if [ $rc -ne 0 ]; then
    say "STOPPED before GCP opened: E5 serves again, its data untouched; GCP stays in maintenance"
    e5_unfreeze; exit 1
  fi
  say "GCP: production backups, LINE webhook, open"
  # A failure before $OPENED: E5 stays frozen (no data lost); fix and run move's tail by hand, or back-to-e5.
  lib <<EOF | gcp_root || die "opening GCP failed; if $OPENED is missing, E5 is frozen and back-to-e5 is safe"
f=/srv/faoj/env/compose.env
for kv in BACKUP_MODE=pgbackrest PG_ARCHIVE_MODE=on BACKUP_REPO_PATH=/pgbackrest/production-gcp \
    DRILL_REPO_PATH=/pgbackrest/production-gcp NGROK_DOMAIN=$NGROK_DOMAIN; do set_kv "\$f" "\${kv%%=*}" "\${kv#*=}"; done
set -a; . "\$f"; set +a
dc up -d --force-recreate --wait db
dc exec -T -u postgres db pgbackrest stanza-create --log-level-console=warn
dc --profile ngrok up -d --wait web worker n8n caddy ngrok
systemctl enable --now faoj-backup-full.timer faoj-backup-diff.timer faoj-backup-files.timer >/dev/null
date -u +%Y-%m-%dT%H:%M:%SZ > $OPENED
faoj maintenance off
faoj smoke
faoj backup full
faoj backup files
EOF
  say "moved: LINE → ngrok → GCP; staff web https://$PUBLIC_HOST/ . E5 stays frozen (read-only) until 10 Nov; dumps kept in $d"
}

back_to_e5() {
  tunnels; not_opened
  lib <<'EOF' | gcp_root
set_kv /srv/faoj/env/compose.env NGROK_DOMAIN ""
dc --profile ngrok stop ngrok >/dev/null 2>&1 || true
faoj maintenance on
EOF
  e5_unfreeze
}

# GCP has taken work and something forces production back to E5 before 10 Nov: the same move
# the other way (freeze GCP, dump, deep compare on E5, decrypt_check), so GCP's writes go along.
reverse_move() {
  tunnels
  gcp_cmd "test -e $OPENED" || die "GCP never took work: back-to-e5 is enough"
  tool_image
  local ts d; ts=$(date +%Y%m%d-%H%M%S); d=/srv/faoj/restore/reverse-$ts
  mkdir -p "$STATE/reverse-$ts"; chmod 700 "$STATE" "$STATE/reverse-$ts"
  echo "This stops GCP (maintenance, services, read-only database) and moves its data back to E5." >&2
  confirm_yes
  say "GCP: freeze"
  freeze_on gcp_root || { unfreeze_on gcp_root; die "freeze failed; GCP serves again, nothing moved"; }
  set +e
  (
    set -e
    lib <<'EOF' | gcp_root > "$STATE/reverse-$ts/source.json"
manage move_manifest --deep
EOF
    # E5's own release must know every migration GCP applied, or it would run on a newer schema.
    lib <<'EOF' | e5_root | sed -n 's/^ *\[.\] *//p' | sort > "$STATE/reverse-$ts/e5-known"
manage showmigrations --plan
EOF
    python3 - "$STATE/reverse-$ts/source.json" "$STATE/reverse-$ts/e5-known" <<'PY' || die "deploy GCP's release on E5 first (it is offline now, so no one notices)"
import json, sys
applied = set(json.load(open(sys.argv[1]))["applied_migrations"])
known = set(open(sys.argv[2]).read().split())
missing = sorted(applied - known)
if missing:
    sys.exit(f"E5's release does not know {missing[:5]}")
PY
    on_e5 "sudo install -d -m 700 $d"
    rm -f "$STATE/dump.sha"
    lib <<'EOF' | gcp_root | tee >(shasum -a 256 | cut -d' ' -f1 > "$STATE/dump.sha") | on_e5 "sudo tee $d/app.dump >/dev/null"
dc exec -T -u postgres db pg_dump -Fc --no-owner --no-privileges faoj
EOF
    local i=0; until [ -s "$STATE/dump.sha" ]; do i=$((i + 1)); [ $i -lt 50 ] || die "no checksum of the dump"; sleep 0.2; done
    [ "$(cat "$STATE/dump.sha")" = "$(on_e5 "sudo sha256sum $d/app.dump | cut -d' ' -f1")" ] || die "dump changed in transit"
    lib <<'EOF' | gcp_root > "$STATE/reverse-$ts/after.json"
manage move_manifest --deep
EOF
    cmp -s "$STATE/reverse-$ts/source.json" "$STATE/reverse-$ts/after.json" || die "GCP changed while frozen"
    on_e5 "sudo tee $d/source.json >/dev/null" < "$STATE/reverse-$ts/source.json"
    say "GCP → E5: private files (E5's old folder kept as media.before-reverse-$ts)"
    on_e5 "sudo mv /srv/faoj/data/media /srv/faoj/data/media.before-reverse-$ts && sudo install -d -o 10001 -g 10001 -m 700 /srv/faoj/data/media"
    lib <<'EOF' | gcp_root | on_e5 "sudo tar -x --same-owner -p -C /srv/faoj/data/media"
tar -C /srv/faoj/data/media -cf - .
EOF
    say "keys changed on GCP since the move (names only; E5 keeps its own values):"
    for side in gcp_root e5_root; do
      lib <<'EOF' | "$side" > "$STATE/reverse-$ts/keys-$side"
for f in /srv/faoj/env/app.secret.env /srv/faoj/env/django.env; do
  sed -n 's/^\([A-Z_][A-Z0-9_]*\)=\(.*\)$/\1 \2/p' "$f" | while read -r k v; do
    printf '%s %s\n' "$k" "$(printf %s "$v" | sha256sum | cut -c1-16)"; done
done | sort
EOF
    done
    comm -3 "$STATE/reverse-$ts/keys-gcp_root" "$STATE/reverse-$ts/keys-e5_root" | awk '{print "  " $1}' | sort -u \
      | grep -v -e DATABASE_URL -e ORCHESTRATOR_TOKEN -e ALLOWED_HOSTS -e CSRF_TRUSTED_ORIGINS -e STAFF_WEB_BASE_URL || echo "  (none)"
    say "E5: restore, migrate, deep compare, decrypt_check"
    { echo "TOOL='$TOOL_IMAGE'"; lib <<EOF; } | e5_root
grep -qx FAOJ_HOST=faoj-prod-e5 /etc/faoj/host.env || { echo "not faoj-prod-e5: refusing to drop anything" >&2; exit 1; }
psql_faoj -c 'ALTER DATABASE faoj RESET default_transaction_read_only'
psql_faoj -c 'DROP SCHEMA public CASCADE' -c 'CREATE SCHEMA public AUTHORIZATION faoj_owner'
dc exec -T -u postgres db pg_restore -d faoj --no-owner --no-privileges --role=faoj_owner --exit-on-error < $d/app.dump
dc run --rm -T migrate >/dev/null
tool move_manifest --compare /dev/stdin < $d/source.json
tool decrypt_check
EOF
  )
  local rc=$?; set -e
  if [ $rc -ne 0 ]; then
    say "STOPPED before E5 opened: GCP serves again with its data untouched; E5 stays in maintenance"
    unfreeze_on gcp_root; exit 1
  fi
  say "GCP: LINE webhook off; E5: open"
  lib <<EOF | gcp_root
set_kv /srv/faoj/env/compose.env NGROK_DOMAIN ""
mv $OPENED $OPENED.reversed-$ts
EOF
  lib <<'EOF' | e5_root || die "opening E5 failed: GCP is frozen with all data, E5 holds the same; fix E5 or unfreeze GCP"
dc --profile ngrok up -d --wait web worker n8n caddy ngrok
faoj maintenance off
faoj smoke
faoj backup full
faoj backup files
EOF
  say "moved back: LINE → ngrok → E5. GCP stays frozen; dumps kept in $d on E5"
}

cmd=${1:-}; shift || true
case "$cmd" in
  prepare) prepare;;
  pull-token) pull_token "$@";;
  ocir-login) ocir_login;;
  bootstrap) bootstrap "$@";;
  skeleton-config) skeleton_config;;
  deploy) deploy "$@";;
  status) status;;
  escrow-check) tunnels; escrow_check;;
  dry-run) dry_run;;
  move) move;;
  back-to-e5) back_to_e5;;
  reverse-move) reverse_move;;
  ssh) if [ $# -gt 0 ]; then on_gcp "$@"; else
         exec gcloud compute ssh "ubuntu@$VM" --project "$PROJECT" --zone "$ZONE" --tunnel-through-iap --ssh-key-file "$KEY"; fi;;
  *) sed -n '2,26p' "$0"; exit 2;;
esac
