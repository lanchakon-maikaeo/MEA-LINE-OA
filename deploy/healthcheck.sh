#!/usr/bin/env bash
# make health-schedule (ADR 0015 Q13 a): from this Mac, every minute, look at production the way
# LINE and the staff reach it. Three failures in a row alert once (a macOS notification and,
# when set, the Teams channel); the first success after that says it is back. A maintenance
# page (503) is planned work, never an alert. Switching to the standby stays a person's call.
#
# Config (optional) ~/.config/faoj/health.env, mode 600:
#   STAFF_URL=https://168-107-70-170.sslip.io/health/ready/
#   WEBHOOK_URL=https://crabmeat-lake-lunar.ngrok-free.dev/webhooks/line
#   TEAMS_WEBHOOK_URL=        (an incoming-webhook URL of the Teams channel; empty = Mac only)
#   ALERT_AFTER=3
set -uo pipefail
CONFIG=${FAOJ_HEALTH_CONFIG:-$HOME/.config/faoj/health.env}
STATE=${FAOJ_HEALTH_STATE:-$HOME/.config/faoj/health}
mkdir -p "$STATE"
# shellcheck disable=SC1090
[ -r "$CONFIG" ] && . "$CONFIG"
STAFF_URL=${STAFF_URL:-https://168-107-70-170.sslip.io/health/ready/}
WEBHOOK_URL=${WEBHOOK_URL:-https://crabmeat-lake-lunar.ngrok-free.dev/webhooks/line}
ALERT_AFTER=${ALERT_AFTER:-3}

code() { curl -s -o /dev/null --max-time 15 -w '%{http_code}' "$1" || true; }
log() { echo "[$(date '+%m-%d %H:%M:%S')] $*" >> "$STATE/health.log"; }
alert() {  # title, text
  osascript -e "display notification \"$2\" with title \"$1\" sound name \"Basso\"" >/dev/null 2>&1 || true
  if [ -n "${TEAMS_WEBHOOK_URL:-}" ]; then
    python3 - "$1" "$2" <<'PY' | curl -s -o /dev/null --max-time 15 -H 'Content-Type: application/json' -d @- "$TEAMS_WEBHOOK_URL" || true
import json, sys
print(json.dumps({"text": f"**{sys.argv[1]}**\n\n{sys.argv[2]}"}))
PY
  fi
  log "alert: $1 — $2"
}

staff=$(code "$STAFF_URL")
line=$(code "$WEBHOOK_URL")   # GET on the webhook: Django answers 405, so it is reachable
case "$staff/$line" in
  200/405) result=ok ;;
  503/*|*/503) result=maintenance ;;
  000/000) # this Mac may be the one offline: only count it when the internet answers
    [ "$(code https://www.google.com/generate_204)" = 204 ] && result=down || result=offline ;;
  *) result=down ;;
esac

fails=$(cat "$STATE/fails" 2>/dev/null || echo 0)
alerted=$([ -e "$STATE/alerted" ] && echo yes || echo no)
case "$result" in
  ok)
    if [ "$alerted" = yes ]; then
      alert "ระบบ ฝอจ. กลับมาแล้ว" "เว็บเจ้าหน้าที่และ LINE ตอบปกติ (ล่มไป $fails นาที)"
      rm -f "$STATE/alerted"
    fi
    echo 0 > "$STATE/fails" ;;
  maintenance|offline)
    log "$result (staff $staff, line $line)" ;;
  down)
    fails=$((fails + 1)); echo "$fails" > "$STATE/fails"
    log "down $fails (staff $staff, line $line)"
    if [ "$fails" -ge "$ALERT_AFTER" ] && [ "$alerted" = no ]; then
      touch "$STATE/alerted"
      alert "ระบบ ฝอจ. ไม่ตอบ" "เว็บเจ้าหน้าที่ตอบ $staff, LINE ตอบ $line ติดกัน $fails นาที ดู RB01 แล้วตัดสินใจว่าจะสลับไปเครื่องสำรองหรือไม่"
    fi ;;
esac
