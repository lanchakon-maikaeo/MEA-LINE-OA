#!/bin/bash
# Local UX preview on a synthetic database (mea_ci), never the Mac's copy of real data.
#   .claude/ux-preview.sh serve            runserver on 127.0.0.1:8011
#   .claude/ux-preview.sh manage <cmd…>    manage.py against mea_ci
set -euo pipefail
cd "$(dirname "$0")/.."
set -a; . ./.env; set +a
export DATABASE_URL="${DATABASE_URL%/*}/mea_ci" DJANGO_DEBUG=true APP_ENV=local
case "${1:-serve}" in
  serve) exec python3 -m uv run --locked python manage.py runserver 127.0.0.1:8011 ;;
  manage) shift; exec python3 -m uv run --locked python manage.py "$@" ;;
esac
