#!/usr/bin/env bash
# Dispara el reporte diario 7:30 AM para todos los usuarios activos.
# Llamado por cron 7:30 + 7:33 como respaldo
set -e
TZ='America/Santo_Domingo'
export TZ
API_BASE="${API_BASE:-http://10.0.0.44:8005}"
STORAGE="${STORAGE:-/home/sam/storage}"
LOG=/home/sam/storage/reporte_morning.log
echo "[$(date '+%Y-%m-%d %H:%M:%S %Z')] dispatch_morning_reports START" >> "$LOG"
# Recorre usuarios que tengan user.json
for user_dir in "$STORAGE"/users/*/; do
  uid=$(basename "$user_dir")
  if [[ ! -f "$user_dir/user.json" ]]; then continue; fi
  # Saltar cuentas de test/debug
  case "$uid" in
    debug_*|test*|default|u|u1) continue ;;
  esac
  echo "  → $uid" >> "$LOG"
  # Si config existe, respétala; sino usa default
  body='{}'
  if [[ -f "$user_dir/business/report_config.json" ]]; then
    enabled=$(python3 -c "import json,sys; print(json.load(open('$user_dir/business/report_config.json')).get('enabled', True))")
    if [[ "$enabled" != "True" ]]; then
      echo "    skipped (enabled=false)" >> "$LOG"
      continue
    fi
  fi
  resp=$(curl -sS -X POST -H "Content-Type: application/json" \
    -d '{"camera_id":null,"date":"today"}' \
    "$API_BASE/api/reportes/send-v2?user_id=$uid" || echo "curl_fail")
  echo "    resp: $(echo "$resp" | head -c 200)" >> "$LOG"
done
echo "[$(date '+%Y-%m-%d %H:%M:%S %Z')] dispatch_morning_reports END" >> "$LOG"
