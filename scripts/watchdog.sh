#!/bin/bash
# OjoIA System Watchdog — runs from cron every minute
# Monitorea: Backend (api-eva), Tunnel (Cloudflare), Modelos IA (qwen-7b, qwen-35b, yolo, whisper, qwen-9b)
LOG="/home/sam/watchdog.log"
RESTART_COOLDOWN=300  # 5 minutos entre reinicios para evitar bucles

now() { date -Iseconds; }

# Verifica si un contenedor Docker está healthy y respondiendo
# Args: $1=container_name, $2=port (opcional)
is_container_healthy() {
  local c=$1
  local port=${2:-}
  local st=$(docker inspect "$c" --format '{{.State.Status}}' 2>/dev/null)

  [ "$st" != "running" ] && return 1
  if [ -n "$port" ]; then
    local code=$(curl -sf --max-time 3 -o /dev/null -w "%{http_code}" "http://127.0.0.1:${port}/health" 2>/dev/null)
    [ "$code" != "200" ] && return 1
  fi
  return 0
}

# Verifica si un contenedor fue detenido intencionalmente por el Megapanel
# Consulta al health-monitor (puerto 9000) que mantiene el flag "paused"
# Args: $1=service_name (ej: "qwen35b", "qwen7b", "yolo", "whisper", "qwen9b")
is_paused_by_operator() {
  local svc_name=$1
  # Usar archivo temporal para evitar problemas de comillas en bash
  local tmpf="/tmp/.watchdog_paused_check.sh"
  cat > "$tmpf" << 'PYEOF'
import urllib.request, json, sys
try:
    req = urllib.request.urlopen('http://127.0.0.1:9000/status', timeout=3)
    d = json.loads(req.read().decode())
    name = sys.argv[1] if len(sys.argv) > 1 else ''
    for s in d.get('services', []):
        if s.get('name') == name:
            sys.exit(0 if s.get('paused') else 1)
    sys.exit(1)
except Exception as e:
    print(f"watchdog_paused_check error: {e}", file=sys.stderr)
    sys.exit(1)
PYEOF
  python3 "$tmpf" "$svc_name"
  local rc=$?
  rm -f "$tmpf"
  return $rc
}

# Verifica si debemos reiniciar (cooldown de 5 min entre intentos)
should_restart() {
  local c=$1
  local f="/tmp/.watchdog_last_${c}"
  if [ -f "$f" ]; then
    local last=$(cat "$f" 2>/dev/null)
    local now=$(date +%s)
    [ $((now - last)) -lt $RESTART_COOLDOWN ] && return 1
  fi
  return 0
}

# Marca el último reinicio
mark_restart() {
  echo "$(date +%s)" > "/tmp/.watchdog_last_${$1}"
}

restart_backend() {
    if systemctl --user restart api_eva.service >/dev/null 2>&1; then
        echo "[$(now)] Backend restarted via user service" >> "$LOG"
    else
        echo "[$(now)] Backend restart failed" >> "$LOG"
    fi
}

restart_tunnel() {
    if sudo -n systemctl restart tunnel.service >/dev/null 2>&1; then
        echo "[$(now)] Tunnel restarted via sudo/system service" >> "$LOG"
    elif systemctl restart tunnel.service >/dev/null 2>&1; then
        echo "[$(now)] Tunnel restarted via system service" >> "$LOG"
    elif systemctl --user restart tunnel.service >/dev/null 2>&1; then
        echo "[$(now)] Tunnel restarted via user service" >> "$LOG"
    else
        echo "[$(now)] Tunnel restart failed: sudo/root required or tunnel.service unavailable" >> "$LOG"
    fi
}

# 1) health check backend
if ! curl -sf --max-time 3 http://localhost:8005/health >/dev/null 2>&1; then
    echo "[$(now)] Backend DOWN — restarting..." >> "$LOG"
    restart_backend
    sleep 2
fi

# 2) Cloudflare Edge check
if ! curl -sf --max-time 5 https://api.ojoia.com.do/health >/dev/null 2>&1; then
    echo "[$(now)] Tunnel OFFLINE — restarting..." >> "$LOG"
    restart_tunnel
    sleep 2
fi

# 3) Modelos IA — qwen-7b PRIMERO (GPU0); vl8b SOLO si 7B está healthy.
# El 7b calcula su VRAM contra la memoria libre: si el vl8b (7GB) carga antes,
# el 7b entra en crash-loop ("no GPU memory for KV cache"). Orden estricto.
SEVENB_OK=0
if is_container_healthy "qwen-7b" "8004"; then
    SEVENB_OK=1
else
    # Verificar si fue detenido intencionalmente por el operador
    if is_paused_by_operator "qwen7b"; then
        echo "[$(now)] qwen-7b DOWN — STOPPED intencionalmente por operador, no reiniciar" >> "$LOG"
    else
        echo "[$(now)] qwen-7b DOWN — intentando reiniciar..." >> "$LOG"
        if should_restart "qwen-7b"; then
            # Si el vl8b está corriendo, pausarlo: libera 7GB para que el 7b
            # pueda reservar su pool de VRAM (lección del crash-loop 2026-08-31).
            VL8B_ST=$(docker inspect qwen3vl8b --format '{{.State.Status}}' 2>/dev/null)
            if [ "$VL8B_ST" = "running" ] && ! is_paused_by_operator "qwen3vl8b"; then
                echo "[$(now)] qwen-7b DOWN — pausando qwen3vl8b para liberar VRAM..." >> "$LOG"
                docker stop qwen3vl8b 2>/dev/null
                date +%s > /tmp/.watchdog_vl8b_paused_for_7b
                sleep 5
            fi
            docker start qwen-7b 2>/dev/null && echo "[$(now)] qwen-7b reiniciado" >> "$LOG"
            date +%s > "/tmp/.watchdog_last_qwen-7b"
        fi
    fi
fi

# 3b) qwen3vl8b: solo si 7B está OK (entra en la VRAM que dejó el 7b).
# Si fue pausado por el watchdog para recuperar al 7b (flag de arriba),
# solo re-arrancarlo cuando el 7b ya esté healthy.
if [ $SEVENB_OK -eq 1 ]; then
    if [ -f /tmp/.watchdog_vl8b_paused_for_7b ]; then
        rm -f /tmp/.watchdog_vl8b_paused_for_7b
        echo "[$(now)] 7B OK — re-arrancando qwen3vl8b..." >> "$LOG"
        docker start qwen3vl8b 2>/dev/null
        date +%s > "/tmp/.watchdog_last_qwen3vl8b"
    fi
    if ! is_container_healthy "qwen3vl8b" "8019"; then
        st=$(docker inspect qwen3vl8b --format '{{.State.Status}}' 2>/dev/null)
        if [ "$st" != "running" ]; then
            if is_paused_by_operator "qwen3vl8b"; then
                echo "[$(now)] qwen3vl8b DOWN — STOPPED intencionalmente por operador, no reiniciar" >> "$LOG"
            elif should_restart "qwen3vl8b"; then
                echo "[$(now)] qwen3vl8b DOWN (7B OK) — iniciando..." >> "$LOG"
                docker start qwen3vl8b 2>/dev/null && echo "[$(now)] qwen3vl8b iniciado" >> "$LOG"
                date +%s > "/tmp/.watchdog_last_qwen3vl8b"
            fi
        fi
    fi
else
    # 7B no está OK: NO arrancar el vl8b (competiría por la VRAM del 7b)
    VL8B_ST=$(docker inspect qwen3vl8b --format '{{.State.Status}}' 2>/dev/null)
    if [ "$VL8B_ST" = "running" ] && ! is_paused_by_operator "qwen3vl8b"; then
        echo "[$(now)] qwen-7b DOWN — deteniendo qwen3vl8b para evitar conflicto VRAM..." >> "$LOG"
        docker stop qwen3vl8b 2>/dev/null
        date +%s > /tmp/.watchdog_vl8b_paused_for_7b
    fi
fi

# 4) qwen38-syv (GPU1, dedicado 23.4GB) — reiniciar si está down
if ! is_container_healthy "qwen38-syv" "18020"; then
    st=$(docker inspect qwen38-syv --format '{{.State.Status}}' 2>/dev/null)
    if [ "$st" != "running" ]; then
        if is_paused_by_operator "qwen38"; then
            echo "[$(now)] qwen38-syv DOWN — STOPPED intencionalmente por operador, no reiniciar" >> "$LOG"
        elif should_restart "qwen38-syv"; then
            echo "[$(now)] qwen38-syv DOWN — reiniciando..." >> "$LOG"
            docker start qwen38-syv 2>/dev/null && echo "[$(now)] qwen38-syv reiniciado" >> "$LOG"
            date +%s > "/tmp/.watchdog_last_qwen38-syv"
        fi
    fi
fi

# 5) Otros modelos IA (yolo, whisper) — reiniciar si están down
# (qwen-9b es manual, NO se auto-reinicia; qwen-35b-a3b está frío en disco)
for entry in "whisper:whisper-turbo:8008" "yolo:yolo-pose:8002"; do
    svc_name=$(echo "$entry" | cut -d: -f1)
    container=$(echo "$entry" | cut -d: -f2)
    port=$(echo "$entry" | cut -d: -f3)
    if ! is_container_healthy "$container" "$port"; then
        st=$(docker inspect "$container" --format '{{.State.Status}}' 2>/dev/null)
        if [ "$st" != "running" ]; then
            # Verificar si fue detenido intencionalmente por el operador
            if is_paused_by_operator "$svc_name"; then
                echo "[$(now)] $container DOWN — STOPPED intencionalmente por operador, no reiniciar" >> "$LOG"
            elif should_restart "$container"; then
                echo "[$(now)] $container DOWN — reiniciando..." >> "$LOG"
                docker start "$container" 2>/dev/null && echo "[$(now)] $container reiniciado" >> "$LOG"
                date +%s > "/tmp/.watchdog_last_${container}"
            fi
        fi
    fi
done
