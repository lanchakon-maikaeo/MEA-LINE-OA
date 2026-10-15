#!/usr/bin/env bash
# The owner gives GitHub Actions its OCIR push credential (ADR 0015 Q6/Q20; Release workflow).
# Creates (or reuses) the OCI user faoj-ci in group faoj-ci (push/prune faoj/* only, iam.tf),
# makes an auth token and hands it straight to `gh secret set`: the token is never printed,
# never written to disk and never passes through a chat.
#
#   deploy/ci-ocir-setup.sh            needs `oci session authenticate … --profile-name faoj`
#                                      and `gh auth login` with the repo scope
set -euo pipefail
export OCI_CLI_PROFILE=faoj OCI_CLI_AUTH=security_token PYTHONWARNINGS=ignore
COMPARTMENT=${E5_COMPARTMENT:-ocid1.compartment.oc1..aaaaaaaairylkjofkqqwzysmo2ies2ptax4qlmsotny2cwwxg2yitpg6g4lq}
say() { echo "[$(date +%H:%M:%S)] $*" >&2; }
die() { echo "ci-ocir-setup: $*" >&2; exit 1; }

oci session refresh --profile faoj >/dev/null 2>&1 || die "OCI session expired: oci session authenticate --region ap-singapore-1 --tenancy-name MEAFlow --profile-name faoj"
gh auth status >/dev/null 2>&1 || die "gh is not logged in: gh auth login"
tenancy=$(oci iam compartment get --compartment-id "$COMPARTMENT" --query 'data."compartment-id"' --raw-output)
namespace=$(oci os ns get --query data --raw-output)

user=$(oci iam user list --compartment-id "$tenancy" --all \
  --query 'data[?starts_with(name, `faoj-ci`)] | [0]' --output json)
if [ "$user" = null ] || [ -z "$user" ]; then
  # OCI mails an activation link to this address: it must be one the owner reads.
  read -r -p "E-mail YOU own for the new OCI user faoj-ci (e.g. yourname+ci@gmail.com): " email
  [ -n "$email" ] || die "an e-mail is needed for a user in the Default identity domain"
  say "creating OCI user faoj-ci"
  user=$(oci iam user create --compartment-id "$tenancy" --name faoj-ci --email "$email" \
    --description "GitHub Actions: push and prune faoj/* in OCIR only (ADR 0015)" --output json \
    | python3 -c 'import json,sys; print(json.dumps(json.load(sys.stdin)["data"]))')
fi
user_id=$(printf '%s' "$user" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
user_name=$(printf '%s' "$user" | python3 -c 'import json,sys; print(json.load(sys.stdin)["name"])')

# CI needs an auth token only: no console password, API keys or other keys, so an activation
# link that reaches the wrong person opens nothing (15 Oct 2026).
oci iam user update-user-capabilities --user-id "$user_id" --can-use-auth-tokens true \
  --can-use-console-password false --can-use-api-keys false --can-use-customer-secret-keys false \
  --can-use-smtp-credentials false --can-use-db-credentials false \
  --can-use-o-auth2-client-credentials false >/dev/null

group_id=$(oci iam group list --compartment-id "$tenancy" --all --query 'data[?name==`faoj-ci`].id | [0]' --raw-output)
[ -n "$group_id" ] && [ "$group_id" != null ] || die "group faoj-ci is missing (Terraform iam.tf)"
if ! oci iam user list-groups --user-id "$user_id" --compartment-id "$tenancy" --query 'data[].name' --raw-output | grep -q '"faoj-ci"'; then
  say "adding $user_name to group faoj-ci"
  oci iam group add-user --group-id "$group_id" --user-id "$user_id" >/dev/null
fi

tokens=$(oci iam auth-token list --user-id "$user_id" --query 'length(data[?"lifecycle-state"==`ACTIVE`])' --raw-output)
[ "${tokens:-0}" -lt 2 ] || die "faoj-ci already has 2 auth tokens (OCI's limit): delete an old one first"
say "creating an auth token and storing it as the GitHub secret OCIR_TOKEN (not shown)"
oci iam auth-token create --user-id "$user_id" --description "GitHub Actions $(date +%Y-%m-%d)" \
    --query data.token --raw-output | tr -d '\n' | gh secret set OCIR_TOKEN
# OCIR user names: <namespace>/<user> for the Default identity domain.
printf '%s' "$namespace/$user_name" | gh secret set OCIR_USERNAME
gh variable set OCIR_REGISTRY --body "ap-singapore-1.ocir.io" >/dev/null
gh variable set OCIR_NAMESPACE --body "$namespace" >/dev/null
say "done: OCIR_USERNAME, OCIR_TOKEN (secrets) and OCIR_REGISTRY, OCIR_NAMESPACE (variables) are set"
say "a new token may take a few minutes before OCIR accepts it"
gh secret list
