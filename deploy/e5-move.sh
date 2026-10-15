#!/usr/bin/env bash
# Move production from the Mac to faoj-prod-e5 (ADR 0015). Run from the repository on the Mac,
# logged in with `oci session authenticate … --profile-name faoj`. Each step can be re-run.
#
#   deploy/e5-move.sh tunnel       Bastion port-forward session + ssh tunnel to E5 (3 h)
#   deploy/e5-move.sh mount        wait for cloud-init, mount the data volume at /srv/faoj
#   deploy/e5-move.sh source       copy this working tree (no secrets, no .git) to E5 ~/src
#   deploy/e5-move.sh bootstrap    bootstrap-host.sh --env production (makes its own secrets)
#   deploy/e5-move.sh build        build db + app images on E5 into a local registry
#   deploy/e5-move.sh dry-run      dump the Mac database, restore it in a throwaway database on
#                                  E5 and prove it with move_manifest --compare; nothing switches
#   deploy/e5-move.sh all          the six steps above in order
#   deploy/e5-move.sh configure    fresh source + bootstrap, then the non-secret settings
#                                  (Object Storage, compartment, hosts, AI/LINE flags like the Mac)
#   deploy/e5-move.sh secrets      the owner puts the secrets in (copied from the Mac .env and
#                                  ngrok config on a yes, or typed hidden); shown nowhere
#   deploy/e5-move.sh check        are the owner's secrets in? (hashes compared, never shown)
#   deploy/e5-move.sh s3-test      does the backup key work? (OCI's error code only)
#   deploy/e5-move.sh diagnose     why a deploy's smoke failed (status, smoke, n8n import, logs)
#   deploy/e5-move.sh deploy       build the current source and `faoj deploy` it (empty database)
#   deploy/e5-move.sh cutover      the move itself, RPO 0 (ADR 0015 Q4/Q14): both sides stop
#                                  taking writes, the Mac data is restored on E5, move_manifest
#                                  must match, then ngrok starts on E5. A mismatch stops here.
#   deploy/e5-move.sh back-to-mac  undo a cutover (E5 writes after it are NOT carried back)
#   deploy/e5-move.sh manage CMD…  one manage.py command on E5 as the migration (owner) role
#   deploy/e5-move.sh pii-key      PII_SECRET_KEY on E5 (made there if missing, never shown), a copy
#                                  for the Mac standby and on the clipboard for the password manager
#   deploy/e5-move.sh mail         the Gmail sender for OTP mail: address typed, app password hidden
#   deploy/e5-move.sh purge-demo [--keep N1,N2 --apply --username U --reason R]
#                                  remove the demo data (15 Oct 2026): a dry run that lists what
#                                  would go unless --apply, which takes a fresh backup first
#   deploy/e5-move.sh ssh [cmd]    a shell (or one command) on E5 through the tunnel
#
# Nothing here types a credential: LINE, AI, backup keys and the ngrok authtoken are put on the
# host by the owner (`check` lists what is still empty).
set -euo pipefail
umask 077

REPO=$(cd "$(dirname "$0")/.." && pwd)
STATE=${E5_STATE:-$HOME/.config/faoj/e5}
KEY=${E5_KEY:-$HOME/.ssh/faoj_oci_ed25519}
BASTION=${E5_BASTION:-ocid1.bastion.oc1.ap-singapore-1.amaaaaaazvo2bmqaz5ywe3sdzbvzyddhhahkrur22kf6idmw4sxcwzwvz3rq}
TARGET_IP=${E5_PRIVATE_IP:-10.20.1.194}
PUBLIC_HOST=${E5_PUBLIC_HOST:-168-107-70-170.sslip.io}
PORT=${E5_TUNNEL_PORT:-2222}
REGION=ap-singapore-1
REGISTRY_IMAGE=registry:2@sha256:a3d8aaa63ed8681a604f1dea0aa03f100d5895b6a58ace528858a7b332415373
PG_IMAGE=postgres:17-bookworm@sha256:3645570cccdfa447589da9f57dd740faa29b30938e861289a5574b6ca6b03826
COMPARTMENT=${E5_COMPARTMENT:-ocid1.compartment.oc1..aaaaaaaairylkjofkqqwzysmo2ies2ptax4qlmsotny2cwwxg2yitpg6g4lq}
NGROK_DOMAIN=${E5_NGROK_DOMAIN:-crabmeat-lake-lunar.ngrok-free.dev}
APPROVAL=${E5_APPROVAL:-"owner in chat 14 Oct 2026: production on faoj-prod-e5 (ADR 0015)"}
export OCI_CLI_PROFILE=faoj OCI_CLI_AUTH=security_token PYTHONWARNINGS=ignore
mkdir -p "$STATE"

say() { echo "[$(date +%H:%M:%S)] $*" >&2; }
die() { echo "e5-move: $*" >&2; exit 1; }
SSH_OPTS=(-i "$KEY" -p "$PORT" -o UserKnownHostsFile="$STATE/known_hosts"
  -o StrictHostKeyChecking=accept-new -o ServerAliveInterval=30 -o BatchMode=yes)
on_e5() { ssh "${SSH_OPTS[@]}" ubuntu@127.0.0.1 "$@"; }

tunnel() {
  if on_e5 true 2>/dev/null; then say "tunnel already up"; return; fi
  oci session refresh --profile faoj >/dev/null 2>&1 || die "OCI session expired: run oci session authenticate"
  local sid state="" host
  # Reuse the session of an earlier run while it is still active (each lasts 3 hours).
  sid=$(cat "$STATE/session" 2>/dev/null || true)
  [ -n "$sid" ] && state=$(oci bastion session get --session-id "$sid" --query 'data."lifecycle-state"' --raw-output 2>/dev/null || true)
  if [ "$state" != ACTIVE ]; then
    say "Bastion session (port 22 of $TARGET_IP, 3 hours)"
    sid=$(oci bastion session create-port-forwarding --bastion-id "$BASTION" --display-name e5-move \
      --target-private-ip "$TARGET_IP" --target-port 22 --ssh-public-key-file "$KEY.pub" \
      --session-ttl 10800 --query data.id --raw-output)
    for _ in $(seq 1 40); do
      state=$(oci bastion session get --session-id "$sid" --query 'data."lifecycle-state"' --raw-output)
      [ "$state" = ACTIVE ] && break
      [ "$state" = FAILED ] && die "Bastion session failed"
      sleep 5
    done
    [ "$state" = ACTIVE ] || die "Bastion session not active after 200 s"
    echo "$sid" > "$STATE/session"
  else
    say "reusing Bastion session $(echo "$sid" | tail -c 9)"
  fi
  host="host.bastion.$REGION.oci.oraclecloud.com"
  # -E keeps the tunnel's own messages: the first place to look when E5 does not answer.
  ssh -i "$KEY" -f -N -E "$STATE/tunnel.log" -o UserKnownHostsFile="$STATE/known_hosts" \
    -o StrictHostKeyChecking=accept-new -o ServerAliveInterval=30 -o ExitOnForwardFailure=yes \
    -L "127.0.0.1:$PORT:$TARGET_IP:22" -p 22 "$sid@$host" || die "ssh to the Bastion failed: $(tail -3 "$STATE/tunnel.log")"
  local err=""
  for _ in $(seq 1 24); do
    err=$(on_e5 true 2>&1) && { say "tunnel up: ssh -p $PORT ubuntu@127.0.0.1"; return; }
    sleep 5
  done
  die "E5 does not answer through the tunnel. E5: ${err:-no reply}; tunnel: $(tail -3 "$STATE/tunnel.log" 2>/dev/null)"
}

mount_data() {
  say "waiting for cloud-init, then mounting /srv/faoj"
  on_e5 'cloud-init status --wait >/dev/null; sudo /usr/local/sbin/faoj-mount-data && findmnt -n /srv/faoj && df -h /srv/faoj | tail -1'
}

source_tree() {
  say "copying the working tree ($(git -C "$REPO" rev-parse --short HEAD)$(git -C "$REPO" diff --quiet || echo -dirty)) without secrets"
  # Same exclusions as .dockerignore plus everything personal: no .env, Docs (passwords), media.
  COPYFILE_DISABLE=1 tar -C "$REPO" --no-xattrs --no-mac-metadata -czf - --exclude=.git --exclude='.env' --exclude='.env.*' --exclude=.venv \
      --exclude=__pycache__ --exclude=.ruff_cache --exclude=reports --exclude=Docs --exclude=docs \
      --exclude=archive --exclude=private-media --exclude=.claude --exclude=node_modules \
      --exclude='*.jpg' --exclude='*.jpeg' --exclude='*.webp' --exclude='._*' . \
    | on_e5 'rm -rf ~/src.new && mkdir -p ~/src.new && tar -xzf - -C ~/src.new && rm -rf ~/src && mv ~/src.new ~/src && du -sh ~/src'
  echo "$(git -C "$REPO" rev-parse --short HEAD)$(git -C "$REPO" diff --quiet || echo -dirty)" | on_e5 'cat > ~/src/.commit'
}

bootstrap() {
  on_e5 "cd ~/src && sudo ./deploy/server/bootstrap-host.sh --env production --public-host $PUBLIC_HOST"
}

build() {
  say "building on E5 (amd64) into 127.0.0.1:5001; first build takes several minutes"
  on_e5 "set -e
    sudo docker inspect faoj-registry >/dev/null 2>&1 || sudo docker run -d --restart unless-stopped \
      --name faoj-registry -p 127.0.0.1:5001:5000 -v /srv/faoj/data/registry:/var/lib/registry $REGISTRY_IMAGE >/dev/null
    cd ~/src; commit=\$(cat .commit); reg=127.0.0.1:5001
    sudo docker build -q -t \$reg/faoj/db:\$commit deploy/server/db >/dev/null
    sudo docker push -q \$reg/faoj/db:\$commit >/dev/null
    db=\$reg/faoj/db@\$(sudo docker inspect -f '{{index .RepoDigests 0}}' \$reg/faoj/db:\$commit | cut -d@ -f2)
    sudo docker build -q --build-arg RELEASE_COMMIT=\$commit --build-arg DB_IMAGE=\$db -t \$reg/faoj/app:\$commit . >/dev/null
    sudo docker push -q \$reg/faoj/app:\$commit >/dev/null
    app=\$reg/faoj/app@\$(sudo docker inspect -f '{{index .RepoDigests 0}}' \$reg/faoj/app:\$commit | cut -d@ -f2)
    echo \$app | sudo tee /srv/faoj/run/release-image >/dev/null
    echo \"db  \$db\"; echo \"app \$app\""
}

dry_run() {
  local dir="$STATE/dry" app
  mkdir -p "$dir"; rm -f "$dir"/*
  app=$(on_e5 'cat /srv/faoj/run/release-image 2>/dev/null') || true
  [ -n "$app" ] || die "no image on E5 yet: run build first"
  say "Mac: manifest, dump, manifest again (a write in between means retry)"
  (cd "$REPO" && python3 -m uv run --locked --env-file .env python manage.py move_manifest > "$dir/source.json")
  (cd "$REPO" && docker compose exec -T db pg_dump -U mea_demo -Fc --no-owner --no-privileges mea_demo) > "$dir/app.dump"
  (cd "$REPO" && python3 -m uv run --locked --env-file .env python manage.py move_manifest --compare "$dir/source.json") \
    || die "the Mac database changed during the dump; run dry-run again"
  say "copying $(du -h "$dir/app.dump" | cut -f1) to E5 (encrypted ssh)"
  on_e5 'sudo install -d -m 700 /srv/faoj/restore/dry'
  for f in source.json app.dump; do on_e5 "sudo tee /srv/faoj/restore/dry/$f >/dev/null" < "$dir/$f"; done
  say "E5: restore into a throwaway database and compare"
  on_e5 "set -e
    pw=\$(python3 -c 'import secrets; print(secrets.token_hex(16))')
    key=\$(python3 -c 'import secrets; print(secrets.token_urlsafe(64))')
    sudo docker rm -f faoj-dry-db >/dev/null 2>&1 || true
    sudo docker network create faoj-dry >/dev/null 2>&1 || true
    # ci mode only talks to a demo database name on a local host name: mea_ci at \"db\".
    sudo docker run -d --name faoj-dry-db --network faoj-dry --network-alias db -e POSTGRES_PASSWORD=\$pw -e POSTGRES_DB=mea_ci \
      -v /srv/faoj/restore/dry:/dry:ro $PG_IMAGE >/dev/null
    for i in \$(seq 1 30); do sudo docker exec faoj-dry-db pg_isready -U postgres -d mea_ci >/dev/null 2>&1 && break; sleep 2; done
    sudo docker exec faoj-dry-db pg_restore -U postgres -d mea_ci --no-owner --no-privileges --exit-on-error /dry/app.dump
    rc=0
    # The manifest goes in on stdin: /srv/faoj/restore stays root-only, the app runs as 10001.
    sudo cat /srv/faoj/restore/dry/source.json | sudo docker run --rm -i --network faoj-dry \
      --entrypoint /app/.venv/bin/python \
      -e APP_ENV=ci -e DJANGO_SECRET_KEY=\$key -e ALLOWED_HOSTS=localhost \
      -e DATABASE_URL=postgresql://postgres:\$pw@db:5432/mea_ci \
      $app manage.py move_manifest --compare /dev/stdin || rc=\$?
    sudo docker rm -f faoj-dry-db >/dev/null; sudo docker network rm faoj-dry >/dev/null
    sudo rm -f /srv/faoj/restore/dry/app.dump
    exit \$rc"
  rm -f "$dir/app.dump"
  say "dry run passed: the Mac data restores on E5 with nothing missing (dumps deleted on both sides)"
}

# Prepended to the root scripts below: compose exactly as faoj runs it, and a KEY=value setter.
REMOTE_LIB='set -euo pipefail
rel=$(readlink -f /srv/faoj/releases/current 2>/dev/null || true)
dc() { docker compose -p faoj -f "$rel/compose.yaml" --env-file /srv/faoj/env/compose.env \
  --env-file "$rel/release.env" --profile clamav "$@"; }
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
# Run the script on stdin as root on E5. It is saved to a file first and run with stdin from
# /dev/null: fed through `bash -s`, the first `docker compose exec/run` read the rest of the
# script as its own input and the lines after it never ran.
as_root() {
  on_e5 "f=\$(mktemp) && cat > \$f && sudo bash \$f $* < /dev/null; rc=\$?; rm -f \$f; exit \$rc"
}

configure() {
  local ns
  ns=$(oci os ns get --query data --raw-output) || die "OCI session expired: run oci session authenticate"
  source_tree
  say "bootstrap again (keeps every existing secret, adds ngrok.secret.env)"
  on_e5 "cd ~/src && sudo ./deploy/server/bootstrap-host.sh --env production --public-host $PUBLIC_HOST >/dev/null"
  say "non-secret settings"
  { echo "$REMOTE_LIB"; cat <<'EOF'
ns=$1 compartment=$2 host=$3 domain=$4
env=/srv/faoj/env
endpoint=$ns.compat.objectstorage.ap-singapore-1.oraclecloud.com
set_kv $env/compose.env BACKUP_S3_ENDPOINT "$endpoint"
set_kv $env/compose.env OCI_COMPARTMENT_OCID "$compartment"
grep -q '^NGROK_DOMAIN=' $env/compose.env || set_kv $env/compose.env NGROK_DOMAIN ""
set_kv $env/backup.secret.env RESTIC_REPOSITORY "s3:https://$endpoint/faoj-files/production"
# Like the Mac today: AI on (PII masked), LINE replies and pushes on, the ngrok host allowed.
set_kv $env/django.env ALLOWED_HOSTS "$host,web,$domain"
set_kv $env/django.env AI_ENABLED true
set_kv $env/django.env OUTBOUND_ENABLED true
set_kv $env/django.env LINE_PUSH_ENABLED true
echo "configured: backups to $endpoint, ngrok host $domain allowed (ngrok itself starts at cutover)"
EOF
  } | as_root "$ns" "$COMPARTMENT" "$PUBLIC_HOST" "$NGROK_DOMAIN"
}

check() {
  local ai dj
  # 16 hex characters of a SHA-256 of each Mac key: enough to compare, useless to an attacker.
  ai=$(grep '^AI_SECRET_KEY=' "$REPO/.env" | tail -1 | cut -d= -f2- | tr -d '\n' | shasum -a 256 | cut -c1-16)
  dj=$(grep '^DJANGO_SECRET_KEY=' "$REPO/.env" | tail -1 | cut -d= -f2- | tr -d '\n' | shasum -a 256 | cut -c1-16)
  cat <<'EOF' | as_root "$ai" "$dj"
set -uo pipefail
env=/srv/faoj/env; bad=0
val() { grep "^$2=" "$env/$1" 2>/dev/null | tail -1 | cut -d= -f2-; }
for pair in app.secret.env:LINE_CHANNEL_SECRET app.secret.env:LINE_CHANNEL_ACCESS_TOKEN \
    db.secret.env:PGBACKREST_REPO1_S3_KEY db.secret.env:PGBACKREST_REPO1_S3_KEY_SECRET \
    backup.secret.env:AWS_ACCESS_KEY_ID backup.secret.env:AWS_SECRET_ACCESS_KEY \
    ngrok.secret.env:NGROK_AUTHTOKEN; do
  f=${pair%%:*} k=${pair#*:}
  if [ -n "$(val "$f" "$k")" ]; then echo "ok     $f $k"; else echo "EMPTY  $f $k"; bad=1; fi
done
# OCI Customer Secret Keys: the access key is 40 hex digits; the secret is longer base64 (/ + =).
# A swapped pair makes Object Storage answer 400 Bad Request. Shapes only, never the values.
for pair in db.secret.env:PGBACKREST_REPO1_S3_KEY backup.secret.env:AWS_ACCESS_KEY_ID; do
  f=${pair%%:*} k=${pair#*:}; v=$(val "$f" "$k")
  [ -z "$v" ] && continue
  if printf %s "$v" | grep -Eq '^[0-9a-f]{40}$'; then echo "ok     $f $k has the shape of an access key"
  else echo "SHAPE  $f $k is not 40 hex digits: secret and access key swapped, or spaces pasted"; bad=1; fi
done
if [ -n "$(val db.secret.env PGBACKREST_REPO1_S3_KEY)" ] && \
   [ "$(val db.secret.env PGBACKREST_REPO1_S3_KEY)" != "$(val backup.secret.env AWS_ACCESS_KEY_ID)" ]; then
  echo "DIFF   db.secret.env and backup.secret.env hold different access keys"; bad=1
fi
h() { printf %s "$(val app.secret.env "$1")" | sha256sum | cut -c1-16; }
if [ "$(h AI_SECRET_KEY)" = "$1" ]; then echo "ok     app.secret.env AI_SECRET_KEY = the Mac's"
else echo "DIFF   app.secret.env AI_SECRET_KEY is not the Mac's: the stored AI key would not decrypt"; bad=1; fi
if [ "$(h DJANGO_SECRET_KEY)" = "$2" ]; then echo "ok     app.secret.env DJANGO_SECRET_KEY = the Mac's"
else echo "DIFF   app.secret.env DJANGO_SECRET_KEY is not the Mac's: staff would be signed out"; bad=1; fi
exit $bad
EOF
}

# The owner puts the secrets in from this terminal. Values travel as JSON on ssh stdin (never in
# argv, never echoed) and are written by root on E5; nothing is kept on the Mac.
secrets() {
  local json tenancy user access=""
  # The access key half of the backup-writer's newest Customer Secret Key comes from OCI itself
  # (the console makes it hard to copy); only the secret half is typed.
  oci session refresh --profile faoj >/dev/null 2>&1 || true
  tenancy=$(oci iam compartment get --compartment-id "$COMPARTMENT" --query 'data."compartment-id"' --raw-output 2>/dev/null || true)
  # The console may have used an e-mail as the user name (faoj-backup-writer+…@…).
  user=$(oci iam user list --compartment-id "$tenancy" --all \
    --query 'data[?starts_with(name, `faoj-backup-writer`)].id | [0]' --raw-output 2>/dev/null || true)
  [ -n "$user" ] && access=$(oci iam customer-secret-key list --user-id "$user" \
    --query 'sort_by(data[?"lifecycle-state"==`ACTIVE`], &"time-created")[-1].id' --raw-output 2>/dev/null || true)
  json=$(REPO="$REPO" E5_ACCESS_KEY="$access" python3 - <<'PY'
import getpass, json, os, re, sys
from pathlib import Path

def from_env(key):
    for line in Path(os.environ["REPO"], ".env").read_text().splitlines():
        if line.startswith(key + "="):
            return line.split("=", 1)[1].strip()
    return ""

def ask(label):
    return getpass.getpass(f"{label} (typed text is hidden, then Enter): ").strip()

def yes(label):  # stdin is this script, so answers come from the terminal itself
    sys.stderr.write(label + " [y/N] ")
    sys.stderr.flush()
    with open("/dev/tty") as tty:
        return tty.readline().strip().lower() in {"y", "yes"}

out = {"app.secret.env": {}, "db.secret.env": {}, "backup.secret.env": {}, "ngrok.secret.env": {}}
keys = ["LINE_CHANNEL_SECRET", "LINE_CHANNEL_ACCESS_TOKEN", "AI_SECRET_KEY", "DJANGO_SECRET_KEY"]
found = [k for k in keys if from_env(k)]
if found and yes(f"1) Copy {', '.join(found)} from the Mac .env to E5?"):
    out["app.secret.env"] = {k: from_env(k) for k in found}
found_access = os.environ.get("E5_ACCESS_KEY", "")
if found_access and not re.fullmatch(r"[0-9a-f]{40}", found_access):
    found_access = ""
label = "2) Enter the backup-writer Customer Secret Key now?"
if found_access:
    label += f" (access key ...{found_access[-6:]} read from OCI; only the secret is typed)"
if yes(label):
    access = found_access or ask("   Access key (40 characters 0-9 a-f)")
    secret = ask("   Secret key (shown once when the key was generated)")
    if secret and not re.fullmatch(r"[A-Za-z0-9+/]{43}=", secret):
        sys.exit(f"that is not a secret key ({len(secret)} characters; OCI's are 44 ending in =)")
    if access and secret:
        out["db.secret.env"] = {"PGBACKREST_REPO1_S3_KEY": access, "PGBACKREST_REPO1_S3_KEY_SECRET": secret}
        out["backup.secret.env"] = {"AWS_ACCESS_KEY_ID": access, "AWS_SECRET_ACCESS_KEY": secret}
token = ""
for conf in [Path.home() / "Library/Application Support/ngrok/ngrok.yml", Path.home() / ".config/ngrok/ngrok.yml"]:
    if conf.exists():
        m = re.search(r"^\s*authtoken:\s*['\"]?([^'\"\s]+)", conf.read_text(), re.M)
        if m:
            token = m.group(1)
            break
if token and yes("3) Copy the ngrok authtoken of this Mac's ngrok config?"):
    out["ngrok.secret.env"] = {"NGROK_AUTHTOKEN": token}
elif yes("3) Enter an ngrok authtoken now?"):
    t = ask("   ngrok authtoken")
    if t:
        out["ngrok.secret.env"] = {"NGROK_AUTHTOKEN": t}
print(json.dumps({f: kv for f, kv in out.items() if kv}))
PY
)
  [ "$json" != "{}" ] || { say "nothing to write"; return; }
  printf '%s\n' "$json" | on_e5 'sudo python3 -c "
import json, os, re, sys
for name, kv in json.load(sys.stdin).items():
    path = \"/srv/faoj/env/\" + name
    text = open(path).read()
    for key, value in kv.items():
        line = key + \"=\" + value
        text, n = re.subn(\"^\" + re.escape(key) + \"=.*$\", lambda m: line, text, flags=re.M)
        if not n:
            text = text.rstrip(chr(10)) + chr(10) + line + chr(10)
        print(\"set\", name, key)
    mode = os.stat(path).st_mode
    open(path, \"w\").write(text)
    os.chmod(path, mode)
"'
  check || true
}

# Ask Object Storage with the backup key from E5's database container and print only OCI's error
# code (SignatureDoesNotMatch = wrong secret; NotAuthorizedOrNotFound/AccessDenied = policy or a
# key OCI has not spread yet). The Authorization echo and every header are dropped.
s3_test() {
  { echo "$REMOTE_LIB"; cat <<'EOF'
set +e
out=$(dc exec -T -u postgres db pgbackrest repo-ls --log-level-console=error 2>&1); rc=$?
code=$(printf '%s\n' "$out" | grep -oE '<Code>[^<]+</Code>|failed with [0-9]+ \([^)]*\)' | sed 's/<[^>]*>//g' | sort -u | tr '\n' ' ')
if [ "$rc" = 0 ]; then echo "Object Storage answered: OK (the backup key works)"
elif [ -n "$code" ]; then echo "Object Storage answered: $code"
else
  # No OCI code: show pgBackRest's own ERROR lines, minus anything that could carry a key.
  echo "pgbackrest exit $rc:"
  printf '%s\n' "$out" | grep -E 'ERROR|HINT|WARN' | grep -viE 'authoriz|credential|secret|key|signature' | head -5
fi
EOF
  } | as_root
}

# Why did a deploy's smoke fail? Status, each smoke step on its own, n8n import's own output and
# the newest web/n8n log lines (lines that could carry a token are dropped).
diagnose() {
  { echo "$REMOTE_LIB"; cat <<'EOF'
set +e
clean() { grep -viE 'bearer|token|secret|password|authoriz|credential.*value' | tail -${1:-15}; }
faoj status
echo "--- n8n import"; dc run --rm n8n-import 2>&1 | clean 12; echo "exit ${PIPESTATUS[0]}"
echo "--- inside smoke"; faoj smoke 2>&1 | clean 12
echo "--- web log"; dc logs --tail 40 web 2>&1 | clean 12
echo "--- n8n log"; dc logs --tail 40 n8n 2>&1 | clean 12
EOF
  } | as_root
}

deploy_e5() {
  check || die "fill in the secrets above first (on E5: sudo nano /srv/faoj/env/<file>)"
  source_tree
  build
  say "faoj deploy on E5 (before the cutover: an empty database; after it: production, brief maintenance)"
  cat <<'EOF' | as_root "'$APPROVAL'"
set -euo pipefail
# The faoj of the source just built, not the one of an older (or half-installed) release.
/home/ubuntu/src/deploy/server/bin/faoj deploy "$(cat /srv/faoj/run/release-image)" --approval "$1"
EOF
}

mac_stop() {
  say "Mac: stop ngrok, web, worker and n8n (LINE now waits and redelivers)"
  pkill -f "ngrok http --url=(https://)?$NGROK_DOMAIN" 2>/dev/null || true
  for _ in $(seq 1 10); do pgrep -f "ngrok http --url=.*$NGROK_DOMAIN" >/dev/null || break; sleep 1; done
  pgrep -f "ngrok http --url=.*$NGROK_DOMAIN" >/dev/null && die "ngrok still runs on the Mac"
  (cd "$REPO" && docker compose stop web worker n8n >/dev/null)
}

mac_start() {
  (cd "$REPO" && docker compose start web worker n8n >/dev/null)
  nohup ngrok http --url="https://$NGROK_DOMAIN" 8000 --log=stdout --log-format=logfmt \
    >> "$STATE/mac-ngrok.log" 2>&1 &
  say "Mac serves again (ngrok pid $!)"
}

cutover() {
  check || die "fill in the secrets above first"
  on_e5 'sudo /usr/local/sbin/faoj status' >/dev/null 2>&1 || die "E5 has no deployed stack: run deploy first"
  local ts dir
  ts=$(date +%Y%m%d-%H%M%S); dir="$STATE/cutover-$ts"; mkdir -p "$dir"
  say "E5: maintenance on, stop web/worker/n8n"
  { echo "$REMOTE_LIB"; echo 'faoj maintenance on; dc stop -t 60 web worker n8n >/dev/null'; } | as_root
  mac_stop
  say "Mac: manifest, dump, manifest again"
  (cd "$REPO" && python3 -m uv run --locked --env-file .env python manage.py move_manifest > "$dir/source.json")
  (cd "$REPO" && docker compose exec -T db pg_dump -U mea_demo -Fc --no-owner --no-privileges mea_demo) > "$dir/app.dump"
  if ! (cd "$REPO" && python3 -m uv run --locked --env-file .env python manage.py move_manifest --compare "$dir/source.json"); then
    mac_start; die "the Mac database changed during the dump; nothing switched, run cutover again"
  fi
  say "copying $(du -h "$dir/app.dump" | cut -f1) to E5"
  on_e5 "sudo install -d -m 700 /srv/faoj/restore/cutover-$ts"
  for f in source.json app.dump; do on_e5 "sudo tee /srv/faoj/restore/cutover-$ts/$f >/dev/null" < "$dir/$f"; done
  say "E5: replace the empty schema with the Mac data, grants, compare"
  if ! { echo "$REMOTE_LIB"; cat <<'EOF'
d=/srv/faoj/restore/cutover-$1
dc exec -T -u postgres db psql -q -v ON_ERROR_STOP=1 -d faoj \
  -c 'DROP SCHEMA public CASCADE' -c 'CREATE SCHEMA public AUTHORIZATION faoj_owner'
dc exec -T -u postgres db pg_restore -d faoj --no-owner --no-privileges --role=faoj_owner --exit-on-error < $d/app.dump
dc run --rm -T migrate >/dev/null
dc run --rm -T migrate /app/.venv/bin/python manage.py move_manifest --compare /dev/stdin < $d/source.json
EOF
  } | as_root "$ts"; then
    say "E5 does not match the Mac: E5 stays in maintenance, the Mac serves again"
    mac_start; die "cutover stopped; nothing was lost (the Mac data is unchanged)"
  fi
  say "E5: start the app and ngrok, maintenance off"
  { echo "$REMOTE_LIB"; cat <<'EOF'
set_kv /srv/faoj/env/compose.env NGROK_DOMAIN "$1"
dc --profile ngrok up -d web worker n8n ngrok >/dev/null
for i in $(seq 1 60); do
  [ "$(dc ps --format '{{.Health}}' web worker | grep -c healthy)" -ge 2 ] && break; sleep 4
done
faoj maintenance off
faoj smoke || echo "SMOKE FAILED: run 'faoj status' on E5 (or back-to-mac)"
faoj backup full >/dev/null 2>&1 && echo "first full backup done" || echo "full backup failed: check 'faoj backup status'"
EOF
  } | as_root "$NGROK_DOMAIN"
  rm -f "$dir/app.dump"
  say "cutover done: LINE → ngrok → E5; staff web https://$PUBLIC_HOST/ . The Mac stack is stopped and keeps its data for 7 days (Q17)."
}

manage_e5() {  # one manage.py command on E5 (owner role, like migrate)
  [ $# -gt 0 ] || die "usage: deploy/e5-move.sh manage <command> [args…]"
  { printf '%s\n' "$REMOTE_LIB"; cat <<'EOF'; } | as_root "$(printf '%q ' "$@")"
dc run --rm -T migrate /app/.venv/bin/python manage.py "$@"
EOF
}

pii_key() {  # 15 Oct 2026 Q16/Q36: the key that seals roster e-mails lives on the server
  local standby="$HOME/faoj-standby/env/app.secret.env"
  cat <<'EOF' | as_root
set -euo pipefail
f=/srv/faoj/env/app.secret.env
if ! grep -q '^PII_SECRET_KEY=.' "$f"; then
  key=$(openssl rand -base64 32 | tr '+/' '-_')
  sed -i '/^PII_SECRET_KEY=/d' "$f"
  printf 'PII_SECRET_KEY=%s\n' "$key" >> "$f"
  echo "made a new PII_SECRET_KEY on E5 (not shown)"
else
  echo "E5 already has a PII_SECRET_KEY (kept)"
fi
EOF
  if [ -f "$standby" ]; then
    on_e5 "sudo grep '^PII_SECRET_KEY=' /srv/faoj/env/app.secret.env" | python3 -c '
import os, re, sys
line, path = sys.stdin.read().strip(), sys.argv[1]
text = open(path).read()
text, n = re.subn(r"^PII_SECRET_KEY=.*$", lambda m: line, text, flags=re.M)
text = text if n else text.rstrip("\n") + "\n" + line + "\n"
open(path, "w").write(text)
os.chmod(path, 0o600)' "$standby"
    say "the Mac standby has the same key ($standby)"
  else
    say "no Mac standby settings yet: run deploy/standby.sh setup later, it copies the key"
  fi
  on_e5 "sudo grep '^PII_SECRET_KEY=' /srv/faoj/env/app.secret.env" | cut -d= -f2- | tr -d '\n' | pbcopy
  say "the key is on the clipboard: paste it into your password manager as 'FAOJ PII_SECRET_KEY'"
  read -r -p "Enter when saved (the clipboard is then cleared) " _ </dev/tty
  printf '' | pbcopy
  say "clipboard cleared; deploy again (deploy/e5-move.sh deploy) so the app reads the key"
}

mail_setup() {  # 15 Oct 2026 Q25: OTP mail from a Gmail account made for sending only
  local json
  json=$(python3 - <<'PY'
import getpass, json, re, sys
with open("/dev/tty") as tty:
    sys.stderr.write("Gmail address that sends the codes: ")
    sys.stderr.flush()
    address = tty.readline().strip()
if not re.fullmatch(r"[^@\s]+@gmail\.com", address):
    sys.exit("that is not a Gmail address")
password = getpass.getpass("App password, 16 letters (typed text is hidden, then Enter): ")
password = password.replace(" ", "")
if not re.fullmatch(r"[a-z]{16}", password):
    sys.exit("an app password is 16 letters (Google account > Security > App passwords)")
print(json.dumps({"django.env": {"EMAIL_HOST": "smtp.gmail.com", "EMAIL_PORT": "587",
  "EMAIL_USE_TLS": "true", "EMAIL_HOST_USER": address, "DEFAULT_FROM_EMAIL": address},
  "app.secret.env": {"EMAIL_HOST_PASSWORD": password}}))
PY
) || die "nothing written"
  printf '%s\n' "$json" | on_e5 'sudo python3 -c "
import json, os, re, sys
for name, kv in json.load(sys.stdin).items():
    path = \"/srv/faoj/env/\" + name
    text = open(path).read()
    for key, value in kv.items():
        line = key + \"=\" + value
        text, n = re.subn(\"^\" + re.escape(key) + \"=.*$\", lambda m: line, text, flags=re.M)
        if not n:
            text = text.rstrip(chr(10)) + chr(10) + line + chr(10)
        print(\"set\", name, key)
    mode = os.stat(path).st_mode
    open(path, \"w\").write(text)
    os.chmod(path, mode)
"'
  say "mail settings written; deploy again (deploy/e5-move.sh deploy) so the app reads them"
}

purge_demo() {
  local arg apply=0
  for arg in "$@"; do [ "$arg" = --apply ] && apply=1; done
  on_e5 'sudo /usr/local/sbin/faoj status' >/dev/null 2>&1 || die "E5 has no deployed stack"
  if [ "$apply" = 1 ]; then
    say "fresh database backup before anything is removed (faoj backup diff)"
    on_e5 'sudo /usr/local/sbin/faoj backup diff' || die "the backup failed: nothing was removed"
  fi
  # The migration (owner) role: history tables are append-only for the app's role.
  { printf '%s\n' "$REMOTE_LIB"; cat <<'EOF'; } | as_root "$(printf '%q ' "$@")"
dc run --rm -T migrate /app/.venv/bin/python manage.py purge_demo_data "$@"
EOF
}

back_to_mac() {
  say "E5: stop ngrok and close (data written on E5 since the cutover stays on E5 only)"
  { echo "$REMOTE_LIB"; echo 'dc --profile ngrok stop ngrok >/dev/null; faoj maintenance on'; } | as_root
  mac_start
}

case "${1:-}" in
  tunnel) tunnel;;
  mount) mount_data;;
  source) source_tree;;
  bootstrap) bootstrap;;
  build) build;;
  dry-run) dry_run;;
  all) tunnel; mount_data; source_tree; bootstrap; build; dry_run;;
  configure) configure;;
  secrets) secrets;;
  check) check;;
  s3-test) s3_test;;
  diagnose) diagnose;;
  deploy) deploy_e5;;
  cutover) cutover;;
  back-to-mac) back_to_mac;;
  purge-demo) shift; purge_demo "$@";;
  pii-key) pii_key;;
  manage) shift; manage_e5 "$@";;
  mail) mail_setup;;
  ssh) shift; on_e5 "$@";;
  *) sed -n '2,35p' "$0"; exit 2;;
esac
