#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════════
# cineia Watchdog v4.0 — corre desde cron cada minuto
# Vigila los 12 servicios reales de cineia + redis + cineia-flux + cloudflared.
# Si alguno cae, lo levanta. Si está down y no levanta, lo reporta al log.
# Integrado: watchdog_ram (reinicia cineia_studio_server si pasa 2GB).
# ═══════════════════════════════════════════════════════════════════════════════

LOG="/home/sam/watchdog.log"
MAX_LOG_LINES=5000

now() { date -Iseconds; }
log()  { echo "[$(now)] $1" >> "$LOG"; }
trim_log() { tail -n "$MAX_LOG_LINES" "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"; }

restart_user() {
  local svc="$1"
  if systemctl --user restart "$svc" >/dev/null 2>&1; then
    log "OK restart user: $svc"
    return 0
  fi
  log "FAIL restart user: $svc"
  return 1
}

restart_sys() {
  local svc="$1"
  if sudo -n systemctl restart "$svc" >/dev/null 2>&1; then
    log "OK restart sys (sudo): $svc"
  elif systemctl restart "$svc" >/dev/null 2>&1; then
    log "OK restart sys: $svc"
  else
    log "FAIL restart sys: $svc"
  fi
}

restart_docker() {
  local container="$1"
  if docker restart "$container" >/dev/null 2>&1; then
    log "OK restart docker: $container"
  else
    log "FAIL restart docker: $container"
  fi
}

# ─── 1) Servicios systemd user (12 reales) ──────────────────────────────────
USER_SERVICES=(
  "health-monitor:9000"
  "megapanel:9001"
  "cineia_studio_server:8095"
  "movie_server:8090"
  "post_server:8014"
  "cineia-audioldm2:8013"
  "cineia-sadtalker:8022"
  "cineia-musicgen:8023"
  "h3-gpu0:8189"
  "h3-gpu1:8190"
  "cineia-realesrgan:8021"
  "tunnel-killer:0"
)

for entry in "${USER_SERVICES[@]}"; do
  IFS=':' read -r svc port <<< "$entry"
  if ! systemctl --user is-active --quiet "$svc" 2>/dev/null; then
    log "DOWN: $svc — reiniciando"
    restart_user "$svc"
    sleep 2
    if [ "$port" != "0" ]; then
      sleep 3
      if ss -tln 2>/dev/null | grep -q ":$port "; then
        log "  OK $svc puerto $port volvio"
      else
        log "  FAIL $svc reiniciado pero puerto $port no abrio"
      fi
    fi
  fi
done

# ─── 2) Servicios systemd system ────────────────────────────────────────────
if ! systemctl is-active --quiet redis-ojoia.service 2>/dev/null; then
  log "DOWN: redis-ojoia.service — reiniciando"
  restart_sys redis-ojoia.service
  sleep 2
fi

# ─── 3) Docker containers ───────────────────────────────────────────────────
# cineia-flux: compartido con otras instancias, reinicia solo si no está.
if ! docker ps --format '{{.Names}}' 2>/dev/null | grep -q "^cineia-flux$"; then
  log "DOWN: docker cineia-flux — reiniciando"
  restart_docker cineia-flux
  sleep 5
  if ! ss -tln 2>/dev/null | grep -q ":8020 "; then
    log "  FAIL cineia-flux reiniciado pero puerto 8020 no abrio"
  fi
fi

# ─── 4) Cloudflare Tunnel ───────────────────────────────────────────────────
if ! pgrep -f "cloudflared.*tunnel" > /dev/null 2>&1; then
  log "DOWN: cloudflared — reiniciando"
  if sudo -n systemctl restart tunnel.service >/dev/null 2>&1; then
    log "  OK tunnel.service reiniciado (sudo)"
  else
    log "  FAIL tunnel.service restart fallo (requiere sudo)"
  fi
  sleep 5
fi

# ─── 5) Watchdog RAM (integrado de watchdog_ram.sh) ─────────────────────────
# Si cineia_studio_server consume > 2GB, reiniciarlo.
STUDIO_PID=$(pgrep -f "cineia_studio_server:APP\|cineia_studio_server.py" | head -1)
if [ -n "$STUDIO_PID" ]; then
  RAM_KB=$(ps -p "$STUDIO_PID" -o rss= 2>/dev/null | tr -d ' ')
  RAM_MB=$((RAM_KB / 1024))
  if [ "$RAM_KB" -gt 2097152 ]; then  # 2 GB en KB
    log "WARN: cineia_studio_server PID=$STUDIO_PID consume ${RAM_MB}MB > 2GB — reiniciando"
    kill -9 "$STUDIO_PID" 2>/dev/null
    sleep 2
    systemctl --user restart cineia_studio_server.service 2>/dev/null
    log "  cineia_studio_server reiniciado"
  fi
fi

# ─── 6) Health check externo (tunel arriba) ─────────────────────────────────
if ! curl -sf --max-time 5 https://cineia.ojoia.com.do/health >/dev/null 2>&1; then
  log "WARN: cineia.ojoia.com.do/health no responde"
fi

trim_log
