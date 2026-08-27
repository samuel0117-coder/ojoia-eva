#!/bin/bash
# /home/sam/ai_system/scripts/boot_system.sh
# Arranque escalonado de servicios AI a nivel de sistema
# Llamado por ai-arranque.service (systemd system-level)

LOG="/home/sam/ai_system/logs"
mkdir -p "$LOG"

G='\033[0;32m'; R='\033[0;31m'; Y='\033[1;33m'; NC='\033[0m'
log()  { echo -e "${G}[$(date +%H:%M:%S)]${NC} $1"; }
warn() { echo -e "${Y}[$(date +%H:%M:%S)] ⚠️  $1${NC}"; }
ok()   { echo -e "  ${G}✅${NC} $1"; }
bad()  { echo -e "  ${R}❌${NC} $1"; }

wait_port() {
    local port=$1 name=$2 max=${3:-30}
    for i in $(seq 1 $max); do
        sleep 1
        if ss -tlnp 2>/dev/null | grep -q ":$port "; then
            ok "$name (${i}s)"
            return 0
        fi
    done
    bad "$name timeout ${max}s"
    return 1
}

start_svc() {
    local svc=$1 name=$2 port=$3 wait_max=${4:-30}
    log "$name"
    systemctl start ${svc}.service 2>/dev/null
    if [ -n "$port" ]; then
        wait_port "$port" "$name" "$wait_max" || true
    fi
}

echo ""
log "═══════════════════════════════════════════════════"
log "  ARRANQUE SISTEMA AI (system-level)"
log "═══════════════════════════════════════════════════"

# FASE 0: Túnel
echo ""; log "FASE 0 — Túnel Cloudflare"
pkill -f "cloudflared tunnel" 2>/dev/null || true; sleep 2
start_svc tunnel "Túnel" "" 30
log "⏸️  10s estabilización DNS..."; sleep 10

# FASE 1: OjoIA Crítico (CPU)
echo ""; log "FASE 1 — OjoIA Crítico (CPU)"
start_svc api-eva  "API Eva"    8005 15
sleep 3
start_svc yolo     "YOLO"       8002 15
sleep 3

# FASE 2: GPU 0 — Qwen (PRIORIDAD MÁXIMA)
echo ""; log "FASE 2 — Qwen 7B (GPU 0) — PESADO (~60-90s)"
systemctl start qwen.service 2>/dev/null
for i in $(seq 1 36); do
    sleep 5
    if curl -sf http://localhost:8004/v1/models >/dev/null 2>&1; then
        ok "Qwen API ($((i*5))s)"
        break
    fi
    [ $((i % 3)) -eq 0 ] && log "  cargando... ($((i*5))s) GPU0: $(nvidia-smi -i 0 --query-gpu=memory.used --format=csv,noheader,nounits 2>/dev/null | tr -d ' ')MB"
done
sleep 5

# FASE 3: GPU 1 — Whisper + SDXL
echo ""; log "FASE 3 — Whisper + SDXL (GPU 1)"
start_svc whisper  "Whisper"    8008 20
sleep 3
start_svc sdxl     "SDXL"       8011 60
sleep 5

# FASE 4: GPU 2 — ComfyUI + Voxtral + AudioLDM2
echo ""; log "FASE 4 — ComfyUI + Voxtral + AudioLDM2 (GPU 2)"
start_svc comfyui  "ComfyUI"    8006 60
sleep 5
start_svc voxtral  "Voxtral"    8010 90
sleep 3
start_svc audioldm2 "AudioLDM2" 8009 20
sleep 3

# FASE 5: UI + Proxy (CPU)
echo ""; log "FASE 5 — UI Server + Project Server (CPU)"
start_svc ui-server      "UI Server"      8080 15
start_svc project-server "Project Server" 8012 10

# RESUMEN
echo ""; log "═══════════════════════════════════════════════════"
log "  ARRANQUE COMPLETO"
log "═══════════════════════════════════════════════════"
echo ""

printf "  %-16s %-8s %-10s\n" "SERVICIO" "PUERTO" "ESTADO"
printf "  %-16s %-8s %-10s\n" "--------" "------" "------"

for svc_port in "tunnel::-" "api-eva:8005" "yolo:8002" "qwen:8004" "whisper:8008" "sdxl:8011" "comfyui:8006" "voxtral:8010" "audioldm2:8009" "ui-server:8080" "project-server:8012"; do
    IFS=':' read -r svc port <<< "$svc_port"
    status=$(systemctl is-active ${svc}.service 2>/dev/null || echo "inactive")
    if [ "$status" = "active" ]; then
        printf "  %-16s %-8s ${G}%-10s${NC}\n" "$svc" "$port" "$status"
    else
        printf "  %-16s %-8s ${R}%-10s${NC}\n" "$svc" "$port" "$status"
    fi
done

echo ""
log "GPUs:"
nvidia-smi --query-gpu=index,memory.used,memory.free --format=csv,noheader,nounits 2>/dev/null | while IFS=',' read idx used free; do
    log "  GPU $idx: $(echo $used | tr -d ' ')MB / $(echo $free | tr -d ' ')MB libres"
done
echo ""
log "═══════════════════════════════════════════════════"
