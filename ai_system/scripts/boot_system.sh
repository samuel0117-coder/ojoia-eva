#!/bin/bash
# /home/sam/ai_system/scripts/boot_system.sh
# Arranque escalonado optimizado - prioriza servicios críticos
# Llamado por ai-arranque.service (systemd system-level)

LOG="/home/sam/ai_system/logs"
mkdir -p "$LOG"

G='\033[0;32m'; R='\033[0;31m'; Y='\033[1;33m'; C='\033[0;36m'; NC='\033[0m'
log()  { echo -e "${G}[$(date +%H:%M:%S)]${NC} $1"; }
warn() { echo -e "${Y}[$(date +%H:%M:%S)] ⚠️  $1${NC}"; }
ok()   { echo -e "  ${G}✅${NC} $1"; }
bad()  { echo -e "  ${R}❌${NC} $1"; }
info() { echo -e "  ${C}ℹ️${NC} $1"; }

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

wait_health() {
    local url=$1 name=$2 max=${3:-60}
    for i in $(seq 1 $((max/5))); do
        sleep 5
        if curl -sf "$url" >/dev/null 2>&1; then
            ok "$name (${i}x5s)"
            return 0
        fi
    done
    bad "$name timeout ${max}s"
    return 1
}

start_svc() {
    local svc=$1 name=$2 port=$3 wait_max=${4:-30}
    log "$name"
    if systemctl is-active --quiet ${svc} 2>/dev/null; then
        info "Ya está corriendo"
        return 0
    fi
    systemctl start ${svc} 2>/dev/null
    if [ -n "$port" ]; then
        wait_port "$port" "$name" "$wait_max" || true
    else
        sleep 2
    fi
}

start_svc_user() {
    local svc=$1 name=$2 port=$3 wait_max=${4:-30}
    log "$name"
    if systemctl --user is-active --quiet ${svc} 2>/dev/null; then
        info "Ya está corriendo"
        return 0
    fi
    systemctl --user start ${svc} 2>/dev/null
    if [ -n "$port" ]; then
        wait_port "$port" "$name" "$wait_max" || true
    else
        sleep 2
    fi
}

echo ""
log "═══════════════════════════════════════════════════"
log "  ARRANQUE SISTEMA AI (optimizado)"
log "═══════════════════════════════════════════════════"

# ═══════════════════════════════════════════════════
# FASE 0: Red (crítico - sin esto nada funciona)
# ═══════════════════════════════════════════════════
echo ""; log "FASE 0 — Red y Túnel"
pkill -f "cloudflared tunnel" 2>/dev/null || true
sleep 2
start_svc tunnel "Túnel Cloudflare" "" 30
log "⏸️  5s estabilización DNS..."
sleep 5

# ═══════════════════════════════════════════════════
# FASE 1: OjoIA Core (CPU - máxima prioridad)
# ═══════════════════════════════════════════════════
echo ""; log "FASE 1 — OjoIA Core (CPU)"
start_svc api-eva  "API Eva"      8005 15
sleep 2
start_svc yolo     "YOLO"         8002 15
sleep 2

# ═══════════════════════════════════════════════════
# FASE 2: Qwen (GPU 0 - pesado, carga diferida)
# ═══════════════════════════════════════════════════
echo ""; log "FASE 2 — Qwen 7B (GPU 0) — PESADO (~60-120s)"
log "  Iniciando en background (no bloquea arranque)"
(
    if ! systemctl is-active --quiet qwen 2>/dev/null; then
        systemctl start qwen 2>/dev/null
        for i in $(seq 1 24); do
            sleep 5
            if curl -sf http://localhost:8004/v1/models >/dev/null 2>&1; then
                echo -e "  ${G}✅${NC} Qwen listo (${i}x5s)"
                break
            fi
        done
    fi
) &
disown
info "Qwen cargando en background..."

# ═══════════════════════════════════════════════════
# FASE 3: ChatRD Core (CPU)
# ═══════════════════════════════════════════════════
echo ""; log "FASE 3 — ChatRD Core (CPU)"
start_svc_user chatrd       "ChatRD API"      8010 15
sleep 2
start_svc_user admin_panel  "Admin Panel"     8030 15
sleep 2
start_svc ui-server         "UI Server"       8080 15
sleep 2

# ═══════════════════════════════════════════════════
# FASE 4: ChatRD GPU (GPU 1 - Whisper + SDXL)
# ═══════════════════════════════════════════════════
echo ""; log "FASE 4 — ChatRD GPU (GPU 1)"
start_svc_user whisper             "Whisper"     8008 20
sleep 2
start_svc gpu1_image_server        "SDXL/Flux"   8015 30
sleep 2

# ═══════════════════════════════════════════════════
# FASE 5: CineIA (GPU 2 - ComfyUI + Movie)
# ═══════════════════════════════════════════════════
echo ""; log "FASE 5 — CineIA (GPU 2)"
start_svc comfyui                  "ComfyUI"     8006 30
sleep 2
start_svc_user movie_server        "Movie Server" 8090 20
sleep 2
start_svc_user cineia_studio_server "CineIA Studio" 8095 15
sleep 2

# ═══════════════════════════════════════════════════
# RESUMEN FINAL
# ═══════════════════════════════════════════════════
echo ""; log "═══════════════════════════════════════════════════"
log "  ARRANQUE COMPLETO"
log "═══════════════════════════════════════════════════"
echo ""

printf "  %-24s %-8s %-10s\n" "SERVICIO" "PUERTO" "ESTADO"
printf "  %-24s %-8s %-10s\n" "--------" "------" "------"

for svc_port in "tunnel::-" "api-eva:8005" "yolo:8002" "qwen:8004" \
                "chatrd:8010" "admin_panel:8030" "ui-server:8080" \
                "whisper:8008" "gpu1_image_server:8015" \
                "comfyui:8006" "movie_server:8090" "cineia_studio_server:8095"; do
    IFS=':' read -r svc port <<< "$svc_port"
    if [ "$svc" = "chatrd" ] || [ "$svc" = "admin_panel" ] || [ "$svc" = "movie_server" ] || [ "$svc" = "cineia_studio_server" ]; then
        status=$(systemctl --user is-active ${svc} 2>/dev/null || echo "inactive")
    else
        status=$(systemctl is-active ${svc} 2>/dev/null || echo "inactive")
    fi
    if [ "$status" = "active" ]; then
        printf "  %-24s %-8s ${G}%-10s${NC}\n" "$svc" "$port" "$status"
    else
        printf "  %-24s %-8s ${R}%-10s${NC}\n" "$svc" "$port" "$status"
    fi
done

echo ""
log "GPUs:"
nvidia-smi --query-gpu=index,memory.used,memory.free --format=csv,noheader,nounits 2>/dev/null | while IFS=',' read idx used free; do
    log "  GPU $idx: $(echo $used | tr -d ' ')MB usados / $(echo $free | tr -d ' ')MB libres"
done
echo ""
log "═══════════════════════════════════════════════════"
