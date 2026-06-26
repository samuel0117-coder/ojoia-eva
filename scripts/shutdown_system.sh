#!/bin/bash
# /home/sam/shutdown_system.sh
# Apagado controlado del sistema - detiene todo en orden seguro

RED='\033[0;31m'; GREEN='\033[0;32m'; Y='\033[1;33m'; NC='\033[0m'

log() { echo -e "${GREEN}[APAGADO]${NC} $1"; }
warn() { echo -e "${Y}[APAGADO]${NC} $1"; }

echo ""
warn "═══════════════════════════════════════════════════"
warn "  APAGANDO SISTEMA AI"
warn "═══════════════════════════════════════════════════"
echo ""

# FASE 1: Detener servicios de usuario
log "Deteniendo servicios de usuario..."
systemctl --user stop movie_server.service 2>/dev/null
systemctl --user stop cineia_studio_server.service 2>/dev/null
systemctl --user stop whisper.service 2>/dev/null
systemctl --user stop mpris-proxy.service 2>/dev/null
log "✅ Servicios de usuario detenidos"

# FASE 2: Detener servicios de sistema (orden inverso al arranque)
log "Deteniendo CineIA (GPU 2)..."
systemctl stop comfyui.service 2>/dev/null

log "Deteniendo ChatRD GPU (GPU 1)..."
systemctl stop gpu1_image_server.service 2>/dev/null

log "Deteniendo ChatRD Core (CPU)..."
systemctl stop ui-server.service 2>/dev/null

log "Deteniendo Qwen (GPU 0)..."
systemctl stop qwen.service 2>/dev/null

log "Deteniendo OjoIA Core..."
systemctl stop api-eva.service 2>/dev/null
systemctl stop yolo.service 2>/dev/null

log "Deteniendo Túnel..."
systemctl stop tunnel.service 2>/dev/null

log "✅ Todos los servicios detenidos"

# FASE 3: Verificar que no quedan procesos huérfanos
sleep 3
log "Verificando procesos..."
ORPHANS=$(ps aux | grep -E "api_eva|yolo_server|qwen|sglang|whisper|comfyui|gpu1_image_server|cloudflared|tunnel" | grep -v grep | wc -l)
if [ "$ORPHANS" -gt 0 ]; then
    warn "⚠️ ${ORPHANS} procesos huérfanos detectados. Matando..."
    pkill -9 -f "api_eva" 2>/dev/null
    pkill -9 -f "yolo_server" 2>/dev/null
    pkill -9 -f "sglang" 2>/dev/null
    pkill -9 -f "whisper_server" 2>/dev/null
    pkill -9 -f "gpu1_image_server" 2>/dev/null
    pkill -9 -f "cloudflared" 2>/dev/null
    sleep 2
fi

echo ""
log "═══════════════════════════════════════════════════"
log "  ✅ SISTEMA APAGADO CORRECTAMENTE"
log "  Para reiniciar: bash /home/sam/startup_system.sh"
log "═══════════════════════════════════════════════════"
