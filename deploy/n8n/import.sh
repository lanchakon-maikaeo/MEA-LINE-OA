#!/bin/sh
# Import the Django service credential and versioned workflows, then publish the active ones.
# Re-running replaces workflows with the same IDs, so nothing is duplicated.
set -eu
umask 077
creds=$(mktemp)
trap 'rm -f "$creds"' EXIT
printf '[{"id":"faojDjangoToken1","name":"Django internal token","type":"httpHeaderAuth","data":{"name":"Authorization","value":"Bearer %s"}}]' \
  "$ORCHESTRATOR_TOKEN" > "$creds"
n8n import:credentials --input="$creds"
n8n import:workflow --separate --input=/workflows
# AI triage (faojTriageAi0001) stays unpublished until its activation is decided (M06).
for id in ${N8N_PUBLISH_IDS:-faojCaseNotify01 faojRecovery0001 faojConvAi000001}; do
  n8n publish:workflow --id="$id"
done
