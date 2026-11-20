#!/usr/bin/env bash
# Production on faoj-prod-gcp (ADR 0043; how-to in Docs/deploy-gcp.md). Run from the repository
# on the Mac after `deploy/gcp-move.sh login`. Each step can be re-run.
#
#   deploy/gcp-move.sh login          the owner signs in to Google in the browser; this Mac keeps
#                                     faoj-deployer's access token for an hour, no refresh token
#   deploy/gcp-move.sh logout         forget that token now
#   deploy/gcp-move.sh deployer-setup  once, by the owner: the faoj-deployer service account
#   deploy/gcp-move.sh registry-setup  once, by the owner: Artifact Registry faoj, GitHub's keyless
#                                     push (faoj-ci) and the VM's pull
#   deploy/gcp-move.sh backup-setup   once, by the owner: backups to Cloud Storage after OCI Object
#                                     Storage filled up (20 Nov 2026); bucket, and the VM may write
#                                     to it (a 2-3 minute stop)
#   deploy/gcp-move.sh deploy IMG [--restore-point dump]  `faoj deploy` the Release workflow's image
#                                     digest, detached; GCP_APPROVAL="who/when approved" goes into
#                                     the release record
#   deploy/gcp-move.sh status         faoj status and /health/ready/
#   deploy/gcp-move.sh line-menu      publish the Rich Menu from the running release, nothing else
#   deploy/gcp-move.sh manage CMD...  one manage.py command in the migrate service
#   deploy/gcp-move.sh ssh [cmd]      a shell (or one command) on the host through IAP
#   deploy/gcp-move.sh staff-proxy    the staff web at https://dedcenter-….run.app (Cloud Run), for
#                                     networks that block sslip.io as Dynamic DNS (MEA's FortiGate)
#
# Setting up a host (phase 1 of Docs/release/gcp-move, 1 Nov 2026):
#   deploy/gcp-move.sh prepare        Docker, unattended upgrades, the 150 GB disk at /srv/faoj
#   deploy/gcp-move.sh bootstrap IMG  bootstrap-host.sh from the release image (its own secrets)
#   deploy/gcp-move.sh skeleton-config  dump backups, no WAL archive, no ngrok (until backup-setup)
#
# Deploy only images the Release workflow built and scanned (asia-southeast3-docker.pkg.dev/…@sha256),
# never one built on a host. The move from E5 (OCI, 2 Nov 2026) and its commands are in git history;
# OCI itself was deleted on 20 Nov 2026 (ADR 0043).
set -euo pipefail
umask 077

PROJECT=${GCP_PROJECT:-mea-ded-center-line-oa}
ZONE=${GCP_ZONE:-asia-southeast3-a}
VM=${GCP_VM:-faoj-prod-gcp}
KEY=${GCP_KEY:-$HOME/.ssh/faoj_gcp_ed25519}
PUBLIC_HOST=${GCP_PUBLIC_HOST:-34-15-129-131.sslip.io}
REGISTRY=asia-southeast3-docker.pkg.dev
GAR_REPO=$REGISTRY/$PROJECT/faoj
GITHUB_REPO=lanchakon-maikaeo/MEA-LINE-OA
CI_SA=faoj-ci@$PROJECT.iam.gserviceaccount.com
APPROVAL=${GCP_APPROVAL:-"owner in chat 2 Nov 2026: production on faoj-prod-gcp (ADR 0043)"}
ACCOUNT=${GCP_ACCOUNT:-mea.amr2026@gmail.com}
DEPLOYER=faoj-deployer@$PROJECT.iam.gserviceaccount.com
TOKEN=${GCP_TOKEN_FILE:-$HOME/.config/faoj/gcp-token}

say() { echo "[$(date +%H:%M:%S)] $*" >&2; }
die() { echo "gcp-move: $*" >&2; exit 1; }
on_gcp() {
  gcloud compute ssh "ubuntu@$VM" --project "$PROJECT" --zone "$ZONE" --tunnel-through-iap \
    --ssh-key-file "$KEY" --quiet --command "$*" -- -o LogLevel=ERROR 2>&1 \
    | grep -v -e 'consider installing NumPy' -e 'increasing_the_tcp_upload_bandwidth' -e '^$' || true
}
# The owner's account is the project's Owner, so it keeps no credential on the Mac: login mints an
# hour of faoj-deployer (IAP ssh, Cloud Run; no IAM) and revokes the owner's. When Google's hour is
# up, nothing here (a script, an agent, malware) reaches production until the owner signs in again.
login() {
  # Whatever happens after the browser, the owner's grant does not outlive this command.
  trap 'gcloud auth revoke --all --quiet >/dev/null 2>&1 || echo "gcp-move: the owner'"'"'s refresh token is still on this Mac: gcloud auth revoke --all" >&2; rm -f "$TOKEN.new"' EXIT
  gcloud auth login "$ACCOUNT" --force --brief
  mkdir -p "${TOKEN%/*}"
  gcloud auth print-access-token --impersonate-service-account "$DEPLOYER" \
    2> >(grep -v 'using service account impersonation' >&2) | tr -d '\n' > "$TOKEN.new" || true
  [ -s "$TOKEN.new" ] || die "no token for $DEPLOYER (just ran deployer-setup? wait 2 minutes and log in again)"
  mv "$TOKEN.new" "$TOKEN"
  say "signed in as faoj-deployer until about $(date -v+55M +%H:%M); deploy/gcp-move.sh logout when done"
}
logout() {
  rm -f "$TOKEN"
  say "signed out (Google ends the token itself within the hour)"
}
deployer_setup() {  # once; IAM changes, so the owner runs it
  local sa=$DEPLOYER compute
  trap 'gcloud auth revoke --all --quiet >/dev/null 2>&1' EXIT
  gcloud auth login "$ACCOUNT" --force --brief
  compute=$(gcloud projects describe "$PROJECT" --format='value(projectNumber)')-compute@developer.gserviceaccount.com
  gcloud iam service-accounts describe "$sa" --project "$PROJECT" >/dev/null 2>&1 \
    || gcloud iam service-accounts create faoj-deployer --project "$PROJECT" \
         --display-name "faoj deploys from the Mac (gcp-move.sh login)"
  # compute.viewer and the IAP tunnel are all `gcloud compute ssh` needs while the key is already in
  # the instance's metadata; run.admin and acting as the runtime account are for staff-proxy.
  for role in roles/compute.viewer roles/iap.tunnelResourceAccessor roles/run.admin; do
    gcloud projects add-iam-policy-binding "$PROJECT" --member "serviceAccount:$sa" --role "$role" \
      --condition None --quiet >/dev/null
  done
  gcloud iam service-accounts add-iam-policy-binding "$compute" --project "$PROJECT" \
    --member "serviceAccount:$sa" --role roles/iam.serviceAccountUser --quiet >/dev/null
  gcloud iam service-accounts add-iam-policy-binding "$sa" --project "$PROJECT" \
    --member "user:$ACCOUNT" --role roles/iam.serviceAccountTokenCreator --quiet >/dev/null
  say "faoj-deployer ready; IAM takes a minute or two, then: deploy/gcp-move.sh login"
}
# OCIR refused pushes from 11 Nov 2026 (OCI free tier). GitHub signs in through Workload Identity
# Federation (main of this repository only) as faoj-ci, writer on this one repository; the VM pulls
# as its own service account with a metadata token, so no key or token is stored anywhere.
registry_setup() {  # once; IAM changes, so the owner runs it
  local number vm_sa scopes pool=github provider
  trap 'gcloud auth revoke --all --quiet >/dev/null 2>&1' EXIT
  gcloud auth login "$ACCOUNT" --force --brief
  number=$(gcloud projects describe "$PROJECT" --format='value(projectNumber)')
  provider=projects/$number/locations/global/workloadIdentityPools/$pool/providers/github
  vm_sa=$(gcloud compute instances describe "$VM" --project "$PROJECT" --zone "$ZONE" --format='value(serviceAccounts[0].email)')
  scopes=$(gcloud compute instances describe "$VM" --project "$PROJECT" --zone "$ZONE" --format='value(serviceAccounts[0].scopes)')
  [ -n "$vm_sa" ] || die "$VM has no service account to pull with"
  case "$scopes" in *cloud-platform*|*devstorage.read*) ;; *) die "$VM's scopes ($scopes) cannot read Artifact Registry";; esac
  say "APIs, repository faoj in asia-southeast3"
  gcloud services enable artifactregistry.googleapis.com iamcredentials.googleapis.com sts.googleapis.com \
    --project "$PROJECT" --quiet
  gcloud artifacts repositories describe faoj --project "$PROJECT" --location asia-southeast3 >/dev/null 2>&1 \
    || gcloud artifacts repositories create faoj --project "$PROJECT" --location asia-southeast3 \
         --repository-format docker --description "faoj release images: Release pushes, $VM pulls" --quiet
  say "faoj-ci may push to faoj only; $vm_sa may pull from it"
  gcloud iam service-accounts describe "$CI_SA" --project "$PROJECT" >/dev/null 2>&1 \
    || gcloud iam service-accounts create faoj-ci --project "$PROJECT" \
         --display-name "GitHub Release: push to Artifact Registry faoj only"
  gcloud artifacts repositories add-iam-policy-binding faoj --project "$PROJECT" --location asia-southeast3 \
    --member "serviceAccount:$CI_SA" --role roles/artifactregistry.writer --quiet >/dev/null
  gcloud artifacts repositories add-iam-policy-binding faoj --project "$PROJECT" --location asia-southeast3 \
    --member "serviceAccount:$vm_sa" --role roles/artifactregistry.reader --quiet >/dev/null
  say "Workload Identity: $GITHUB_REPO on main only"
  gcloud iam workload-identity-pools describe "$pool" --project "$PROJECT" --location global >/dev/null 2>&1 \
    || gcloud iam workload-identity-pools create "$pool" --project "$PROJECT" --location global \
         --display-name "GitHub Actions" --quiet
  gcloud iam workload-identity-pools providers describe github --project "$PROJECT" --location global \
      --workload-identity-pool "$pool" >/dev/null 2>&1 \
    || gcloud iam workload-identity-pools providers create-oidc github --project "$PROJECT" --location global \
         --workload-identity-pool "$pool" --issuer-uri https://token.actions.githubusercontent.com \
         --attribute-mapping google.subject=assertion.sub,attribute.repository=assertion.repository,attribute.ref=assertion.ref \
         --attribute-condition "assertion.repository=='$GITHUB_REPO' && assertion.ref=='refs/heads/main'" --quiet
  gcloud iam service-accounts add-iam-policy-binding "$CI_SA" --project "$PROJECT" \
    --role roles/iam.workloadIdentityUser \
    --member "principalSet://iam.googleapis.com/projects/$number/locations/global/workloadIdentityPools/$pool/attribute.repository/$GITHUB_REPO" \
    --quiet >/dev/null
  say "GitHub variables (no secrets)"
  gh variable set GAR_REGISTRY --repo "$GITHUB_REPO" --body "$REGISTRY"
  gh variable set GAR_REPOSITORY --repo "$GITHUB_REPO" --body "$PROJECT/faoj"
  gh variable set GCP_WIF_PROVIDER --repo "$GITHUB_REPO" --body "$provider"
  gh variable set GCP_CI_SERVICE_ACCOUNT --repo "$GITHUB_REPO" --body "$CI_SA"
  say "ready; IAM takes a minute or two, then re-run the Release workflow"
}
# OCI Object Storage refused every upload from 20 Nov 2026 08:03Z (StorageLimitExceeded: the
# tenancy's 20 GB Always Free), so WAL and backups stopped. They move to Cloud Storage in this
# project, written as the VM's own service account through the metadata server. A key file was the
# first plan, but the project's policy forbids service account keys
# (iam.disableServiceAccountKeyCreation); so the VM's scopes change from Storage read-only to
# read-write, which Google only allows on a stopped VM. IAM still limits the account to this
# bucket and the faoj registry.
BACKUP_BUCKET=${GCP_BACKUP_BUCKET:-$PROJECT-faoj-backup}
backup_setup() {  # once; IAM changes and a 2-3 minute stop of the VM, so the owner runs it
  trap 'gcloud auth revoke --all --quiet >/dev/null 2>&1' EXIT
  gcloud auth login "$ACCOUNT" --force --brief
  local vm_sa scopes role
  vm_sa=$(gcloud compute instances describe "$VM" --project "$PROJECT" --zone "$ZONE" --format='value(serviceAccounts[0].email)')
  say "bucket gs://$BACKUP_BUCKET in asia-southeast3, private; $vm_sa writes to it"
  gcloud services enable storage.googleapis.com --project "$PROJECT" --quiet
  gcloud storage buckets describe "gs://$BACKUP_BUCKET" --project "$PROJECT" >/dev/null 2>&1 \
    || gcloud storage buckets create "gs://$BACKUP_BUCKET" --project "$PROJECT" --location asia-southeast3 \
         --default-storage-class STANDARD --uniform-bucket-level-access --public-access-prevention --quiet
  # restic looks the bucket up (storage.buckets.get) before init, which objectAdmin lacks.
  for role in roles/storage.objectAdmin roles/storage.legacyBucketReader; do
    gcloud storage buckets add-iam-policy-binding "gs://$BACKUP_BUCKET" --project "$PROJECT" \
      --member "serviceAccount:$vm_sa" --role "$role" --quiet >/dev/null
  done
  # The key-file plan's account (20 Nov 2026), never given a key: its grants and the account go.
  local old=faoj-backup@$PROJECT.iam.gserviceaccount.com
  if gcloud iam service-accounts describe "$old" --project "$PROJECT" >/dev/null 2>&1; then
    for role in roles/storage.objectAdmin roles/storage.legacyBucketReader; do
      gcloud storage buckets remove-iam-policy-binding "gs://$BACKUP_BUCKET" --project "$PROJECT" \
        --member "serviceAccount:$old" --role "$role" --quiet >/dev/null 2>&1 || true
    done
    gcloud iam service-accounts delete "$old" --project "$PROJECT" --quiet
  fi
  scopes=$(gcloud compute instances describe "$VM" --project "$PROJECT" --zone "$ZONE" \
             --format='value(serviceAccounts[0].scopes)' | tr ';,' '\n\n')
  if grep -q -e devstorage.read_write -e devstorage.full_control -e auth/cloud-platform <<<"$scopes"; then
    say "$VM already writes to Cloud Storage"
  else
    scopes="$(grep -v devstorage <<<"$scopes" | paste -sd, -),https://www.googleapis.com/auth/devstorage.read_write"
    say "stopping $VM to let it write to Cloud Storage: production is down for 2-3 minutes"
    gcloud compute instances stop "$VM" --project "$PROJECT" --zone "$ZONE" --quiet
    gcloud compute instances set-service-account "$VM" --project "$PROJECT" --zone "$ZONE" \
      --service-account "$vm_sa" --scopes "$scopes" --quiet
    gcloud compute instances start "$VM" --project "$PROJECT" --zone "$ZONE" --quiet
    # Every service restarts on its own (restart: unless-stopped; /srv/faoj mounts from fstab).
    local n=0
    until gcp_cmd "sudo faoj smoke" >/dev/null 2>&1; do
      n=$((n + 1)); [ "$n" -lt 40 ] || die "$VM is up but 'faoj smoke' still fails after 10 minutes"
      sleep 10
    done
    say "$VM is back; smoke ok"
  fi
  say "host settings: pgBackRest and restic to gs://$BACKUP_BUCKET (they take effect on the next deploy)"
  lib <<EOF | gcp_root
set_kv /srv/faoj/env/compose.env BACKUP_REPO_TYPE gcs
set_kv /srv/faoj/env/compose.env BACKUP_GCS_BUCKET $BACKUP_BUCKET
set_kv /srv/faoj/env/compose.env BACKUP_GCS_PROJECT $PROJECT
set_kv /srv/faoj/env/backup.secret.env RESTIC_REPOSITORY gs:$BACKUP_BUCKET:/restic
# The VM's token reaches the bucket from a container: list the (empty) pgBackRest repository with
# this release's database image, then write and delete one object as the host.
db_image=\$(sed -n 's/^DB_IMAGE=//p' "\$rel/release.env")
docker run --rm --user postgres -e PGBACKREST_REPO1_TYPE=gcs -e PGBACKREST_REPO1_GCS_BUCKET=$BACKUP_BUCKET \
  -e PGBACKREST_REPO1_GCS_KEY_TYPE=auto -e PGBACKREST_LOG_LEVEL_FILE=off \
  --entrypoint pgbackrest "\$db_image" repo-ls >/dev/null
token=\$(curl -sSf -H 'Metadata-Flavor: Google' \
  http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["access_token"])')
curl -sSf -o /dev/null -H "Authorization: Bearer \$token" --data-binary ok \
  "https://storage.googleapis.com/upload/storage/v1/b/$BACKUP_BUCKET/o?uploadType=media&name=setup-check"
curl -sSf -o /dev/null -X DELETE -H "Authorization: Bearer \$token" \
  "https://storage.googleapis.com/storage/v1/b/$BACKUP_BUCKET/o/setup-check"
echo "$VM reads and writes gs://$BACKUP_BUCKET"
EOF
  say "ready. Next, signed in: GCP_APPROVAL=\"…\" deploy/gcp-move.sh deploy <app@sha256> --restore-point dump"
}
# Docker on the VM signs in to Artifact Registry with the VM's own hour-long metadata token.
HOST_GAR_LOGIN="curl -sf -H 'Metadata-Flavor: Google' http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token | python3 -c 'import json,sys; print(json.load(sys.stdin)[\"access_token\"])' | sudo docker login -u oauth2accesstoken --password-stdin https://$REGISTRY >/dev/null"
signed_in() {
  [ -n "$(find "$TOKEN" -mmin -55 2>/dev/null)" ] || die "not signed in, or the hour is up: deploy/gcp-move.sh login"
  export CLOUDSDK_AUTH_ACCESS_TOKEN_FILE=$TOKEN
}
image_arg() {
  case "${1:-}" in "$GAR_REPO"/*@sha256:*) ;; *) die "give the Release workflow's app image ($GAR_REPO/app@sha256:…)";; esac
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

bootstrap() {
  image_arg "${1:-}"
  say "bootstrap-host.sh from $1 (secrets made on the host, never shown)"
  on_gcp "set -e
    $HOST_GAR_LOGIN
    sudo docker pull -q '$1' >/dev/null
    tmp=\$(mktemp -d); cid=\$(sudo docker create '$1')
    sudo docker cp \"\$cid:/app/deploy/server/.\" \"\$tmp/\" >/dev/null; sudo docker rm \"\$cid\" >/dev/null
    sudo FAOJ_ROOT=/srv/faoj \"\$tmp/bootstrap-host.sh\" --env production --public-host $PUBLIC_HOST
    sudo rm -rf \"\$tmp\""
}

skeleton_config() {
  say "empty skeleton: local dumps only, no archive, no ngrok"
  on_gcp 'set -e
    f=/srv/faoj/env/compose.env
    for kv in BACKUP_MODE=dump PG_ARCHIVE_MODE=off NGROK_DOMAIN=; do
      k=${kv%%=*}; sudo sed -i "s|^$k=.*|$kv|" "$f"; sudo grep -q "^$k=" "$f" || echo "$kv" | sudo tee -a "$f" >/dev/null
    done
    # No backup bucket yet (backup-setup turns backups on).
    sudo systemctl disable --now faoj-backup-full.timer faoj-backup-diff.timer faoj-backup-files.timer 2>/dev/null || true
    sudo grep -E "^(BACKUP_MODE|PG_ARCHIVE_MODE|NGROK_DOMAIN|PUBLIC_HOST)=" "$f"
    systemctl list-timers "faoj-*" --no-pager --no-legend | awk "{print \$(NF-1)}"'
}

deploy() {
  image_arg "${1:-}"
  local sha=${1##*@sha256:} extra=""; sha=${sha:0:12}
  case "${2:-}" in
    "") ;;
    --restore-point) [ "${3:-}" = dump ] || die "deploy IMG [--restore-point dump]"; extra=" --restore-point dump";;
    *) die "deploy IMG [--restore-point dump]";;
  esac
  say "faoj deploy $sha (detached; log /srv/faoj/run/deploy-$sha.log)"
  # systemd-run keeps the deploy alive when the IAP session drops (1 Nov 2026 on E5).
  on_gcp "set -e
    $HOST_GAR_LOGIN
    # The new release's own faoj runs the deploy, so a fix in faoj applies to this deploy too.
    tmp=/srv/faoj/run/bundle-$sha; sudo mkdir -p /srv/faoj/run
    sudo rm -rf \$tmp; sudo mkdir -p \$tmp; cid=\$(sudo docker create '$1')
    sudo docker cp \"\$cid:/app/deploy/server/.\" \$tmp/ >/dev/null; sudo docker rm \"\$cid\" >/dev/null
    bin=\$tmp/bin/faoj
    sudo systemctl reset-failed faoj-deploy-$sha 2>/dev/null || true
    sudo systemd-run --unit faoj-deploy-$sha --setenv FAOJ_ROOT=/srv/faoj \
      --property StandardOutput=file:/srv/faoj/run/deploy-$sha.log \
      --property StandardError=file:/srv/faoj/run/deploy-$sha.log \
      \$bin deploy '$1' --approval '$APPROVAL'$extra"
  say "follow: deploy/gcp-move.sh ssh 'sudo tail -f /srv/faoj/run/deploy-$sha.log'"
}

status() {
  on_gcp 'sudo faoj status 2>&1 | tail -20; echo; sudo faoj maintenance status'
  curl -s -m 10 "https://$PUBLIC_HOST/health/ready/" || echo "(no answer from https://$PUBLIC_HOST/)"
  echo
}

# MEA's FortiGate blocks sslip.io ("Dynamic DNS"); run.app is "Information Technology" (FortiGuard,
# 2 Nov 2026). Cloud Run runs the pinned Caddy as a plain reverse proxy to the VM: the VM still sees
# its own host name, so only CSRF_TRUSTED_ORIGINS and STAFF_WEB_BASE_URL learn the new one.
STAFF_PROXY=dedcenter
STAFF_PROXY_URL=https://$STAFF_PROXY-381083094942.asia-southeast3.run.app
CADDY_IMAGE=docker.io/library/caddy@sha256:4c6e91c6ed0e2fa03efd5b44747b625fec79bc9cd06ac5235a779726618e530d
staff_proxy() {
  local env; env=$(mktemp); trap 'rm -f "$env"' RETURN
  cat > "$env" <<EOF
CADDYFILE: |
  {
  	admin off
  	auto_https off
  }
  :{\$PORT} {
  	header -Server
  	reverse_proxy https://$PUBLIC_HOST {
  		header_up Host $PUBLIC_HOST
  		header_down Location https://$PUBLIC_HOST $STAFF_PROXY_URL
  	}
  }
EOF
  gcloud run deploy "$STAFF_PROXY" --project "$PROJECT" --region asia-southeast3 --image "$CADDY_IMAGE" \
    --env-vars-file "$env" --command sh \
    --args='^|^-c|printf "%s" "$CADDYFILE" > /tmp/Caddyfile && exec caddy run --config /tmp/Caddyfile --adapter caddyfile' \
    --port 8080 --memory 256Mi --cpu 1 --min-instances 0 --max-instances 3 --allow-unauthenticated --quiet
  lib <<EOF | gcp_root
h=\$(sed -n 's/^CSRF_TRUSTED_ORIGINS=//p' /srv/faoj/env/django.env)
case ",\$h," in *",$STAFF_PROXY_URL,"*) ;; *) set_kv /srv/faoj/env/django.env CSRF_TRUSTED_ORIGINS "\$h,$STAFF_PROXY_URL";; esac
set_kv /srv/faoj/env/django.env STAFF_WEB_BASE_URL "$STAFF_PROXY_URL"
dc up -d --force-recreate --wait web worker >/dev/null
grep -E '^(CSRF_TRUSTED_ORIGINS|STAFF_WEB_BASE_URL)=' /srv/faoj/env/django.env
EOF
  say "staff web: $STAFF_PROXY_URL/ (also still https://$PUBLIC_HOST/)"
}

# --- Commands on the host: a script on stdin runs as root with the current release's stack ----

REMOTE_LIB='set -euo pipefail
rel=$(readlink -f /srv/faoj/releases/current 2>/dev/null || true)
dc() { docker compose -p faoj -f "$rel/compose.yaml" --env-file /srv/faoj/env/compose.env \
  --env-file "$rel/release.env" --profile clamav "$@"; }
psql_faoj() { dc exec -T -u postgres db psql -qAt -v ON_ERROR_STOP=1 -d faoj "$@"; }
manage() { dc run --rm -T migrate /app/.venv/bin/python manage.py "$@"; }
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

gcp_cmd() {  # one command line on GCP; stdin, stdout and the exit code pass through
  gcloud compute ssh "ubuntu@$VM" --project "$PROJECT" --zone "$ZONE" --tunnel-through-iap \
    --ssh-key-file "$KEY" --quiet --command "$1" -- -o LogLevel=ERROR \
    2> >(grep -v -e NumPy -e tcp_upload -e '^WARNING' -e '^$' >&2)
}
# A script on stdin runs as root, its own stdin /dev/null (docker compose would eat the script).
gcp_root() { gcp_cmd "f=\$(mktemp) && cat > \$f && sudo bash \$f $* < /dev/null; rc=\$?; rm -f \$f; exit \$rc"; }
lib() { echo "$REMOTE_LIB"; cat; }
manage_gcp() {  # one manage.py command on GCP (the migrate service: same env, network and media)
  [ $# -gt 0 ] || die "usage: deploy/gcp-move.sh manage <command> [args...]"
  lib <<'EOF' | gcp_root "$(printf '%q ' "$@")"
manage "$@"
EOF
}

cmd=${1:-}; shift || true
case "$cmd" in login|logout|deployer-setup|registry-setup|backup-setup|"") ;; *) signed_in;; esac
case "$cmd" in
  login) login;;
  logout) logout;;
  deployer-setup) deployer_setup;;
  registry-setup) registry_setup;;
  backup-setup) backup_setup;;
  prepare) prepare;;
  bootstrap) bootstrap "$@";;
  skeleton-config) skeleton_config;;
  deploy) deploy "$@";;
  status) status;;
  staff-proxy) staff_proxy;;
  manage) manage_gcp "$@";;
  line-menu) say "publishing the Rich Menu from the running release; services stay up"; manage_gcp setup_line_rich_menu;;
  ssh) if [ $# -gt 0 ]; then gcp_cmd "$*"; else  # stdout stays clean for pipes (gcloud's WARNING goes to stderr)
         exec gcloud compute ssh "ubuntu@$VM" --project "$PROJECT" --zone "$ZONE" --tunnel-through-iap --ssh-key-file "$KEY"; fi;;
  *) sed -n '/^set -euo/q;2,$p' "$0"; exit 2;;
esac
