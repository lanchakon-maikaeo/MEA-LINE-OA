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
    tmp=/srv/faoj/run/bundle-$sha; sudo mkdir -p /srv/faoj/run
    if [ ! -x /usr/local/sbin/faoj ] || [ ! -e /srv/faoj/releases/current ]; then
      sudo rm -rf \$tmp; sudo mkdir -p \$tmp; cid=\$(sudo docker create '$1')
      sudo docker cp \"\$cid:/app/deploy/server/.\" \$tmp/ >/dev/null; sudo docker rm \"\$cid\" >/dev/null
      bin=\$tmp/bin/faoj
    else
      bin=/usr/local/sbin/faoj
    fi
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

cmd=${1:-}; shift || true
case "$cmd" in
  prepare) prepare;;
  pull-token) pull_token "$@";;
  ocir-login) ocir_login;;
  bootstrap) bootstrap "$@";;
  skeleton-config) skeleton_config;;
  deploy) deploy "$@";;
  status) status;;
  ssh) if [ $# -gt 0 ]; then on_gcp "$@"; else
         exec gcloud compute ssh "ubuntu@$VM" --project "$PROJECT" --zone "$ZONE" --tunnel-through-iap --ssh-key-file "$KEY"; fi;;
  *) sed -n '2,15p' "$0"; exit 2;;
esac
