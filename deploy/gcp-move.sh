#!/usr/bin/env bash
# Production from faoj-prod-e5 to faoj-prod-gcp (Docs/release/gcp-move). Run from the repository on
# the Mac, logged in with `gcloud auth login`. Phase 1 (this file so far) brings up an EMPTY
# stack: no keys, no LINE webhook, no data; E5 keeps serving. Each step can be re-run.
#
#   deploy/gcp-move.sh prepare        Docker, unattended upgrades, the 150 GB disk at /srv/faoj
#   deploy/gcp-move.sh ocir-login     the owner types a pull-only OCIR auth token (hidden)
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
  ocir-login) ocir_login;;
  bootstrap) bootstrap "$@";;
  skeleton-config) skeleton_config;;
  deploy) deploy "$@";;
  status) status;;
  ssh) if [ $# -gt 0 ]; then on_gcp "$@"; else
         exec gcloud compute ssh "ubuntu@$VM" --project "$PROJECT" --zone "$ZONE" --tunnel-through-iap --ssh-key-file "$KEY"; fi;;
  *) sed -n '2,15p' "$0"; exit 2;;
esac
