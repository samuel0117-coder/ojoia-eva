#!/bin/bash
# /home/sam/ai_system/scripts/arranque.sh
# ARRANQUE MANUAL v2 — Solo para uso manual (no boot automático)
# El boot automático lo maneja ai-arranque.service (systemd system-level)
#
# Uso:
#   arranque.sh start          Arranca todo en orden
#   arranque.sh stop           Para todo en orden inverso
#   arranque.sh restart        Stop + start
#   arranque.sh status         Estado de todos los servicios
#   arranque.sh gpu2 heavy     Pausa ligeros de GPU 2 para modelo grande
#   arranque.sh gpu2 restore   Restaura ligeros de GPU 2

set -euo pipefail

LOG="/home/sam/ai_system/logs"
mkdir -p "$LOG"

G='\033[0;32m'; R='\033[0;31m'; Y='\033[1;33m'; C='\033[0;36m'; NC='\033[0m'
log()  { echo -e "${G}[$(date +%H:%M:%S)]${NC} $1"; }
warn() { echo -e "${Y}[$(date +%H:%M:%S)] ⚠️  $1${NC}"; }
fail() { echo -e "${R}[$(date +%H:%M:%S)] ❌ $1${NC}"; }
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
    fail "$name timeout ${max}s"
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

stop_svc() {
    local svc=$1 name=$2
    systemctl stop ${svc}.service 2>/dev/null && ok "$name detenido" || true
}

show_status() {
    echo ""
    echo "═══════════════════════════════════════════════════"
    echo "  ESTADO DEL SISTEMA AI"
    echo "═══════════════════════════════════════════════════"
    echo ""
    printf "  %-16s %-8s %-10s %s\n" "SERVICIO" "PUERTO" "ESTADO" "GPU"
    printf "  %-16s %-8s %-10s %s\n" "--------" "------" "------" "---"

    declare -A SERVICES=(
        ["tunnel"]="tunnel::-"
        ["api-eva"]="api-eva:8005:CPU"
        ["yolo"]="yolo:8002:CPU"
        ["qwen"]="qwen:8004:GPU0"
        ["whisper"]="whisper:8008:GPU1"
        ["sdxl"]="sdxl:8011:GPU1"
        ["comfyui"]="comfyui:8006:GPU2"
        ["voxtral"]="voxtral:8010:GPU2"
        ["audioldm2"]="audioldm2:8009:GPU2"
        ["ui-server"]="ui-server:8080:CPU"
        ["project-server"]="project-server:8012:CPU"
    )

    for key in tunnel api-eva yolo qwen whisper sdxl comfyui voxtral audioldm2 ui-server project-server; do
        IFS=':' read -r svc port gpu <<< "${SERVICES[$key]}"
        status_svc=$(systemctl is-active ${svc}.service 2>/dev/null || echo "inactive")
        if [ "$status_svc" = "active" ]; then
            status="${G}running${NC}"
        else
            status="${R}stopped${NC}"
        fi
        printf "  %-16s %-8s %-22b %s\n" "$svc" "$port" "$status" "$gpu"
    done

    echo ""
    echo "  GPUs:"
    nvidia-smi --query-gpu=index,memory.used,memory.free --format=csv,noheader,nounits 2>/dev/null | while IFS=',' read idx used free; do
        printf "    GPU %s: %sMB usados / %sMB libres\n" "$idx" "$(echo $used | tr -d ' ')" "$(echo $free | tr -d ' ')"
    done
    echo ""
    echo "  RAM: $(free -m | awk '/^Mem:/{print $3}')MB usados / $(free -m | awk '/^Mem:/{print $2}')MB total"
    echo "═══════════════════════════════════════════════════"
}

cmd_start() {
    echo ""
    log "═══════════════════════════════════════════════════"
    log "  ARRANQUE MANUAL v2"
    log "═══════════════════════════════════════════════════"

    # FASE 0: Túnel
    echo ""; log "FASE 0 — Túnel Cloudflare"
    pkill -f "cloudflared tunnel" 2>/dev/null || true; sleep 2
    start_svc tunnel "Túnel" "" 30
    log "⏸️  10s estabilización DNS..."; sleep 10

    # FASE 1: OjoIA Crítico (CPU)
    echo ""; log "FASE 1 — OjoIA Crítico (CPU)"
    start_svc api-eva "API Eva" 8005 15
    sleep 3
    start_svc yolo "YOLO" 8002 15
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
    start_svc whisper "Whisper" 8008 20
    sleep 3
    start_svc sdxl "SDXL" 8011 60
    sleep 5

    # FASE 4: GPU 2 — ComfyUI + Voxtral + AudioLDM2
    echo ""; log "FASE 4 — ComfyUI + Voxtral + AudioLDM2 (GPU 2)"
    start_svc comfyui "ComfyUI" 8006 60
    sleep 5
    start_svc voxtral "Voxtral" 8010 90
    sleep 3
    start_svc audioldm2 "AudioLDM2" 8009 20
    sleep 3

    # FASE 5: UI + Proxy (CPU)
    echo ""; log "FASE 5 — UI Server + Project Server (CPU)"
    start_svc ui-server "UI Server" 8080 15
    start_svc project-server "Project Server" 8012 10

    echo ""; log "═══════════════════════════════════════════════════"
    log "  ARRANQUE COMPLETO"
    log "═══════════════════════════════════════════════════"
    show_status
}

cmd_stop() {
    echo ""
    log "═══════════════════════════════════════════════════"
    log "  DETENIENDO SISTEMA AI"
    log "═══════════════════════════════════════════════════"

    echo ""; log "FASE 5 — UI + Proxy"
    stop_svc project-server "Project Server"
    stop_svc ui-server "UI Server"

    echo ""; log "FASE 4 — GPU 2"
    stop_svc audioldm2 "AudioLDM2"
    stop_svc voxtral "Voxtral"
    stop_svc comfyui "ComfyUI"
    sleep 3

    echo ""; log "FASE 3 — GPU 1"
    stop_svc sdxl "SDXL"
    stop_svc whisper "Whisper"
    sleep 3

    echo ""; log "FASE 2 — GPU 0"
    stop_svc qwen "Qwen"
    sleep 5

    echo ""; log "FASE 1 — CPU"
    stop_svc yolo "YOLO"
    stop_svc api-eva "API Eva"

    echo ""; log "FASE 0 — Túnel"
    stop_svc tunnel "Túnel"

    pkill -9 -f "yolo_server\|whisper_server\|voxtral\|ComfyUI/main\|audioldm2\|api_eva\|sglang\|sdxl_server\|StableDiffusion\|uvicorn\|project_server" 2>/dev/null || true
    sleep 3

    echo ""; log "Sistema detenido. GPUs:"
    nvidia-smi --query-gpu=index,memory.used --format=csv,noheader,nounits 2>/dev/null | while IFS=',' read idx used; do
        log "  GPU $idx: $(echo $used | tr -d ' ')MB"
    done
}

cmd_gpu2() {
    local action="${1:-status}"
    case "$action" in
        heavy|restore|clean|light)
            bash /home/sam/ai_system/scripts/gpu2_manager.sh "$action"
            ;;
        status)
            bash /home/sam/ai_system/scripts/gpu2_manager.sh status
            ;;
        *)
            echo "Uso: $0 gpu2 {status|light|heavy|restore|clean}"
            ;;
    esac
}

ACTION="${1:-status}"
case "$ACTION" in
    start)   cmd_start ;;
    stop)    cmd_stop ;;
    restart) cmd_stop; sleep 5; cmd_start ;;
    status)  show_status ;;
    gpu2)    cmd_gpu2 "${2:-status}" ;;
    *)
        echo "Uso: $0 {start|stop|restart|status|gpu2}"
        echo ""
        echo "  start          Arranca todo en orden"
        echo "  stop           Para todo en orden inverso"
        echo "  restart        Stop + start"
        echo "  status         Estado de todos los servicios"
        echo "  gpu2 heavy     Pausa ligeros GPU 2 para modelo grande"
        echo "  gpu2 restore   Restaura ligeros GPU 2"
        ;;
esac
