#!/usr/bin/env bash
# Production on faoj-prod-gcp (ADR 0043; how-to in Docs/deploy-gcp.md). Run from the repository
# on the Mac after `deploy/gcp-move.sh login`. Each step can be re-run.
#
#   deploy/gcp-move.sh login          the owner signs in to Google in the browser; this Mac keeps
#                                     faoj-deployer's access token for an hour, no refresh token
#   deploy/gcp-move.sh logout         forget that token now
#   deploy/gcp-move.sh deployer-setup  once, by the owner: the faoj-deployer service account
#   deploy/gcp-move.sh registry-setup  once, by the owner: Artifact Registry faoj, GitHub's keyless
#                                     push (faoj-ci) and the VM's pull, after OCIR went free tier
#   deploy/gcp-move.sh backup-setup   once, by the owner: backups to Cloud Storage after OCI Object
#                                     Storage filled up (20 Nov 2026); bucket, faoj-backup and its key
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
#   deploy/gcp-move.sh pull-token [USER]  OCI user faoj-gcp-pull in faoj-deployers (pull only), a new
#                                     auth token piped straight into docker login on the host
#   deploy/gcp-move.sh ocir-login     or: the owner types an existing pull-only token (hidden)
#   deploy/gcp-move.sh bootstrap IMG  bootstrap-host.sh from the release image (its own secrets)
#   deploy/gcp-move.sh skeleton-config  dump backups, no WAL archive, metrics to stdout, no ngrok
#
# The move from E5 (phase 2, done 2 Nov 2026; needs `deploy/e5-move.sh tunnel` access; E5 runs
# GCP's checks in one-off containers):
#   deploy/gcp-move.sh escrow-check   hashes of the keys the data needs, to compare with the
#                                     owner's password manager
#   deploy/gcp-move.sh escrow-copy KEY  that key from E5 onto this Mac's clipboard (owner runs it)
#   deploy/gcp-move.sh dry-run        E5's data into a throwaway database on GCP, deep compare
#   deploy/gcp-move.sh move           the move itself (5-move.md); asks YES before E5 stops
#   deploy/gcp-move.sh back-to-e5     undo a move that stopped before GCP opened
#   deploy/gcp-move.sh reverse-move   after GCP took work: the move back to E5, same checks
#
# Deploy only images the Release workflow built and scanned (asia-southeast3-docker.pkg.dev/…@sha256;
# ap-singapore-1.ocir.io/… before 11 Nov 2026), never one built on a host.
set -euo pipefail
umask 077

PROJECT=${GCP_PROJECT:-mea-ded-center-line-oa}
ZONE=${GCP_ZONE:-asia-southeast3-a}
VM=${GCP_VM:-faoj-prod-gcp}
KEY=${GCP_KEY:-$HOME/.ssh/faoj_gcp_ed25519}
PUBLIC_HOST=${GCP_PUBLIC_HOST:-34-15-129-131.sslip.io}
REGISTRY=asia-southeast3-docker.pkg.dev
GAR_REPO=$REGISTRY/$PROJECT/faoj
OCIR=ap-singapore-1.ocir.io
NAMESPACE=axif86tn0tbj
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
# project. The VM's scopes only read Storage and changing them means stopping it, so pgBackRest and
# restic sign in with a key of faoj-backup, which reaches the objects of this one bucket and nothing
# else. The key goes from Google to the host; on this Mac it lives seconds in a private temp folder.
BACKUP_BUCKET=${GCP_BACKUP_BUCKET:-$PROJECT-faoj-backup}
BACKUP_SA=faoj-backup@$PROJECT.iam.gserviceaccount.com
backup_setup() {  # once (again to replace the key); IAM changes and a key, so the owner runs it
  local tmp; tmp=$(mktemp -d)
  trap 'rm -rf "$tmp"; gcloud auth revoke --all --quiet >/dev/null 2>&1' EXIT
  gcloud auth login "$ACCOUNT" --force --brief
  say "bucket gs://$BACKUP_BUCKET in asia-southeast3, private"
  gcloud services enable storage.googleapis.com iam.googleapis.com --project "$PROJECT" --quiet
  gcloud storage buckets describe "gs://$BACKUP_BUCKET" --project "$PROJECT" >/dev/null 2>&1 \
    || gcloud storage buckets create "gs://$BACKUP_BUCKET" --project "$PROJECT" --location asia-southeast3 \
         --default-storage-class STANDARD --uniform-bucket-level-access --public-access-prevention --quiet
  gcloud iam service-accounts describe "$BACKUP_SA" --project "$PROJECT" >/dev/null 2>&1 \
    || gcloud iam service-accounts create faoj-backup --project "$PROJECT" \
         --display-name "pgBackRest and restic on $VM: objects in gs://$BACKUP_BUCKET only"
  # restic looks the bucket up (storage.buckets.get) before init, which objectAdmin lacks.
  # A service account just created reads "does not exist" to Cloud Storage for a while (20 Nov 2026).
  local role try
  for role in roles/storage.objectAdmin roles/storage.legacyBucketReader; do
    try=0
    until gcloud storage buckets add-iam-policy-binding "gs://$BACKUP_BUCKET" --project "$PROJECT" \
            --member "serviceAccount:$BACKUP_SA" --role "$role" --quiet >/dev/null; do
      try=$((try + 1)); [ "$try" -lt 12 ] || die "Cloud Storage still does not see $BACKUP_SA; run backup-setup again"
      say "waiting for IAM to show faoj-backup to Cloud Storage ($try)"; sleep 10
    done
  done
  say "a new key for faoj-backup, straight onto $VM"
  gcloud iam service-accounts keys create "$tmp/key.json" --iam-account "$BACKUP_SA" --project "$PROJECT" --quiet
  local id; id=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["private_key_id"])' "$tmp/key.json")
  # The database container reads it as postgres (uid 999); restic runs as root.
  gcp_cmd "sudo install -d -m 0750 -o root -g 999 /srv/faoj/gcs && sudo install -m 0640 -o root -g 999 /dev/stdin /srv/faoj/gcs/faoj-backup.json" \
    < "$tmp/key.json"
  rm -rf "$tmp"
  local old
  for old in $(gcloud iam service-accounts keys list --iam-account "$BACKUP_SA" --project "$PROJECT" \
                 --managed-by user --format='value(name.basename())'); do
    [ "$old" = "$id" ] || gcloud iam service-accounts keys delete "$old" --iam-account "$BACKUP_SA" \
                            --project "$PROJECT" --quiet
  done
  say "host settings: pgBackRest and restic to gs://$BACKUP_BUCKET (they take effect on the next deploy)"
  lib <<EOF | gcp_root
set_kv /srv/faoj/env/compose.env BACKUP_REPO_TYPE gcs
set_kv /srv/faoj/env/compose.env BACKUP_GCS_BUCKET $BACKUP_BUCKET
set_kv /srv/faoj/env/compose.env BACKUP_GCS_PROJECT $PROJECT
set_kv /srv/faoj/env/backup.secret.env RESTIC_REPOSITORY gs:$BACKUP_BUCKET:/restic
# The key reaches the bucket: list the (empty) repository with this release's database image.
db_image=\$(sed -n 's/^DB_IMAGE=//p' "\$rel/release.env")
docker run --rm --user postgres -v /srv/faoj/gcs:/run/faoj-gcs:ro \
  -e PGBACKREST_REPO1_TYPE=gcs -e PGBACKREST_REPO1_GCS_BUCKET=$BACKUP_BUCKET \
  -e PGBACKREST_REPO1_GCS_KEY_TYPE=service -e PGBACKREST_REPO1_GCS_KEY=/run/faoj-gcs/faoj-backup.json \
  -e PGBACKREST_LOG_LEVEL_FILE=off --entrypoint pgbackrest "\$db_image" repo-ls >/dev/null
echo "faoj-backup can list gs://$BACKUP_BUCKET from the host"
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
  case "${1:-}" in "$GAR_REPO"/*@sha256:*|"$OCIR"/*@sha256:*) ;; *) die "give the Release workflow's app image ($GAR_REPO/app@sha256:…)";; esac
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
    --command "sudo docker login $OCIR -u '$user' --password-stdin >/dev/null && echo 'OCIR login saved'" \
    -- -o LogLevel=ERROR 2>&1 | grep -v -e NumPy -e tcp_upload -e '^$'
}

# The host saves the login; the token passes from `oci` through ssh stdin and is never printed,
# written on the Mac or put in argv (like ci-ocir-setup.sh). A new token can take minutes to work.
login_on_host() {  # user; token on stdin
  gcloud compute ssh "ubuntu@$VM" --project "$PROJECT" --zone "$ZONE" --tunnel-through-iap \
    --ssh-key-file "$KEY" --quiet -- -o LogLevel=ERROR \
    "t=\$(cat); for i in \$(seq 1 15); do printf %s \"\$t\" | sudo docker login $OCIR -u '$1' --password-stdin >/dev/null 2>&1 && { echo 'OCIR login saved'; exit 0; }; sleep 20; done; echo 'OCIR login failed'; exit 1" \
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
    $HOST_GAR_LOGIN
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
manage_gcp() {  # one manage.py command on GCP (the migrate service: same env, network and media)
  [ $# -gt 0 ] || die "usage: deploy/gcp-move.sh manage <command> [args...]"
  lib <<'EOF' | gcp_root "$(printf '%q ' "$@")"
manage "$@"
EOF
}
# From the terminal itself: ssh before it may have read (and dropped) piped stdin.
confirm_yes() {
  local answer=${GCP_MOVE_ANSWER:-}
  [ -n "$answer" ] || read -r -p "Type YES to start: " answer < /dev/tty
  [ "$answer" = YES ] || die "not started"
}

tunnels() {
  "$REPO/deploy/e5-move.sh" tunnel
  gcp_cmd true || die "no IAP ssh to $VM"
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

# One key from E5 straight onto this Mac's clipboard for the owner's password manager: never on
# the screen, in a file or in the chat. E5 is only read. The owner runs this, not the assistant.
escrow_copy() {
  local key=${1:-} file
  case "$key" in
    AI_SECRET_KEY|PII_SECRET_KEY|DJANGO_SECRET_KEY) file=app.secret.env;;
    PGBACKREST_REPO1_CIPHER_PASS) file=db.secret.env;;
    RESTIC_PASSWORD) file=backup.secret.env;;
    *) die "escrow-copy AI_SECRET_KEY|PII_SECRET_KEY|DJANGO_SECRET_KEY|PGBACKREST_REPO1_CIPHER_PASS|RESTIC_PASSWORD";;
  esac
  { echo "K=$key F=$file"; lib <<'EOF'; } | e5_root | tr -d '\n' | pbcopy
grep "^$K=" "/srv/faoj/env/$F" | tail -1 | cut -d= -f2- | tr -d '\n'
EOF
  [ -n "$(pbpaste)" ] || die "$key is empty on E5"
  say "$key is on the clipboard ($(pbpaste | shasum -a 256 | cut -c1-16)): paste it into the password manager, then copy something else"
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
                    "PG_ARCHIVE_MODE", "BACKUP_REPO_PATH", "DRILL_REPO_PATH",
                    "BACKUP_HOST_TAG", "DRILL_HOST_TAG"},
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
        # copy2 keeps the mode but not the owner: the app (uid 10001) must still read env/google.
        os.makedirs(d, exist_ok=True)
        os.chmod(d, os.stat(s).st_mode)
        for f in sorted(os.listdir(s)):
            sf, df = os.path.join(s, f), os.path.join(d, f)
            shutil.copy2(sf, df)
            os.chown(df, os.stat(sf).st_uid, os.stat(sf).st_gid)
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
    DRILL_REPO_PATH=/pgbackrest/production-gcp BACKUP_HOST_TAG=production-gcp DRILL_HOST_TAG=production-gcp \
    NGROK_DOMAIN=$NGROK_DOMAIN; do set_kv "\$f" "\${kv%%=*}" "\${kv#*=}"; done
# LINE reaches web under the ngrok name (2 Nov 2026: without it Django answered 400).
h=\$(sed -n 's/^ALLOWED_HOSTS=//p' /srv/faoj/env/django.env)
case ",\$h," in *",$NGROK_DOMAIN,"*) ;; *) set_kv /srv/faoj/env/django.env ALLOWED_HOSTS "\$h,$NGROK_DOMAIN";; esac
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
case "$cmd" in login|logout|deployer-setup|registry-setup|backup-setup|"") ;; *) signed_in;; esac
case "$cmd" in
  login) login;;
  logout) logout;;
  deployer-setup) deployer_setup;;
  registry-setup) registry_setup;;
  backup-setup) backup_setup;;
  prepare) prepare;;
  pull-token) pull_token "$@";;
  ocir-login) ocir_login;;
  bootstrap) bootstrap "$@";;
  skeleton-config) skeleton_config;;
  deploy) deploy "$@";;
  status) status;;
  escrow-check) tunnels; escrow_check;;
  escrow-copy) tunnels; escrow_copy "$@";;
  dry-run) dry_run;;
  move) move;;
  back-to-e5) back_to_e5;;
  reverse-move) reverse_move;;
  staff-proxy) staff_proxy;;
  manage) manage_gcp "$@";;
  line-menu) say "publishing the Rich Menu from the running release; services stay up"; manage_gcp setup_line_rich_menu;;
  ssh) if [ $# -gt 0 ]; then gcp_cmd "$*"; else  # stdout stays clean for pipes (gcloud's WARNING goes to stderr)
         exec gcloud compute ssh "ubuntu@$VM" --project "$PROJECT" --zone "$ZONE" --tunnel-through-iap --ssh-key-file "$KEY"; fi;;
  *) sed -n '/^set -euo/q;2,$p' "$0"; exit 2;;
esac
