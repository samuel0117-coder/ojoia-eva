#!/bin/bash
# /home/sam/ai_system/start_all.sh — ARRANQUE ESCALONADO ESTABLE v3
# Uso: ./start_all.sh [--stop] [--status] [--flux]
# NO usar set -e — cada fase es independiente

VENV="/home/sam/ai_system/venv"
LOG="/home/sam/ai_system/logs"
mkdir -p "$LOG"

G='\033[0;32m'; R='\033[0;31m'; Y='\033[1;33m'; C='\033[0;36m'; NC='\033[0m'
log()  { echo -e "${G}[$(date +%H:%M:%S)]${NC} $1"; }
warn() { echo -e "${Y}[$(date +%H:%M:%S)]${NC} $1"; }

wait_port() {
    local port=$1 name=$2 max=${3:-30}
    for i in $(seq 1 $max); do
        sleep 1
        if ss -tlnp 2>/dev/null | grep -q ":$port "; then
            log "  $name ✅ (${i}s)"
            return 0
        fi
    done
    warn "  $name ❌ timeout ${max}s"
    return 1
}

# ── STOP ──────────────────────────────────────────────────────────────────────
if [ "$1" = "--stop" ]; then
    log "Deteniendo todo..."
    sudo systemctl stop ai-system 2>/dev/null || true
    sudo systemctl stop api-eva sglang 2>/dev/null || true
    pkill -9 -f "yolo_server\|whisper_server\|voxtral\|ComfyUI/main\|ui_server\|audioldm2" 2>/dev/null || true
    for port in 8002 8004 8005 8006 8008 8009 8010 8080; do
        sudo fuser -k ${port}/tcp 2>/dev/null || true
    done
    sleep 5; log "✅ Todo detenido"; exit 0
fi

# ── STATUS ────────────────────────────────────────────────────────────────────
if [ "$1" = "--status" ]; then
    echo -e "${C}═══ SERVICIOS AI ═══${NC}"
    for svc in "API:8005" "YOLO:8002" "Qwen:8004" "Whisper:8008" "Voxtral:8010" "ComfyUI:8006" "AudioLDM2:8009" "UI:8080"; do
        IFS=':' read -r name port <<< "$svc"
        printf "  %-14s :$port  " "$name"
        ss -tlnp 2>/dev/null | grep -q ":$port " && echo -e "${G}✅${NC}" || echo -e "${R}❌${NC}"
    done
    echo ""; free -h | grep -E "^Mem|^Inter"
    exit 0
fi

FLUX=false; [ "$1" = "--flux" ] && FLUX=true

echo ""
log "═══════════════════════════════════════════════════"
log "  ARRANQUE ESCALONADO v4  modo: $([ "$FLUX" ] && echo 'con Flux' || echo 'sin Flux')"
log "═══════════════════════════════════════════════════"

# ── TÚNEL (debe estar antes que todo) ─────────────────────────────────────────
log "Iniciando Cloudflare Tunnel..."
pkill -f "cloudflared tunnel" 2>/dev/null || true
sleep 2
systemctl --user start tunnel.service 2>/dev/null || true
for i in $(seq 1 30); do
    sleep 1
    if pgrep -f "cloudflared tunnel" >/dev/null 2>&1; then
        log "  Tunnel ✅ (${i}s)"
        break
    fi
done
log "⏸️  15s estabilización DNS..."; sleep 15

# ── LIMPIEZA INICIAL ──────────────────────────────────────────────────────────
log "Limpieza inicial..."
pkill -9 -f "yolo_server\|whisper_server\|voxtral\|ComfyUI/main\|ui_server\|audioldm2" 2>/dev/null || true
sleep 3
log "RAM inicial: $(free -m | awk '/^Mem:/{print $7}')MB"

# ═══════════════════════════════════════════════════════════════════════════════
# FASE 1: API Eva (systemd user)
# ═══════════════════════════════════════════════════════════════════════════════
echo ""; log "1/8 API Eva (CPU, systemd)"

# Crear/actualizar servicio systemd para API Eva
sudo tee /etc/systemd/system/api-eva.service > /dev/null << 'EOF'
[Unit]
Description=OjoIA API Eva
After=network.target
[Service]
Type=simple
User=sam
Group=sam
WorkingDirectory=/home/sam/ai_system
ExecStart=/home/sam/ai_system/venv/bin/python /home/sam/ai_system/api_eva.py
Restart=on-failure
RestartSec=5
Environment=PATH=/home/sam/ai_system/venv/bin:/usr/bin:/bin
StandardOutput=append:/home/sam/ai_system/logs/api_eva.log
StandardError=append:/home/sam/ai_system/logs/api_eva_error.log
[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload 2>/dev/null
sudo systemctl enable api-eva 2>/dev/null
sudo systemctl restart api-eva 2>/dev/null
sleep 10
wait_port 8005 "API Eva" 15
log "⏸️  10s estabilización"; sleep 10

# ═══════════════════════════════════════════════════════════════════════════════
# FASE 2: YOLO (CPU)
# ═══════════════════════════════════════════════════════════════════════════════
echo ""; log "2/8 YOLO (CPU)"
nohup bash -c "cd /home/sam/ai_system && CUDA_VISIBLE_DEVICES='' $VENV/bin/python yolo_server.py" >> "$LOG/yolo.log" 2>&1 &
wait_port 8002 "YOLO" 15
log "⏸️  5s"; sleep 5

# ═══════════════════════════════════════════════════════════════════════════════
# FASE 3: Whisper (GPU 1) — carga diferida
# ═══════════════════════════════════════════════════════════════════════════════
echo ""; log "3/8 Whisper (GPU 1, carga diferida)"
nohup bash -c "cd /home/sam/ai_system && CUDA_VISIBLE_DEVICES=1 HF_HOME=/home/sam/.cache/hf_models $VENV/bin/python whisper_server.py" >> "$LOG/whisper.log" 2>&1 &
wait_port 8008 "Whisper" 20
log "⏸️  10s"; sleep 10

# ═══════════════════════════════════════════════════════════════════════════════
# FASE 4: SGLang/Qwen (GPU 0) — PESADO
# ═══════════════════════════════════════════════════════════════════════════════
echo ""; log "4/8 SGLang/Qwen (GPU 0) — CARGA PESADA (~90s)"

sudo tee /etc/systemd/system/sglang.service > /dev/null << 'EOF'
[Unit]
Description=SGLang Qwen2.5-VL (GPU 0)
After=network.target
[Service]
Type=simple
User=sam
Group=sam
ExecStart=/home/sam/ai_system/venv/bin/sglang serve --model-path /opt/ai_models/Qwen2.5-VL-7B-Instruct-AWQ --host 0.0.0.0 --port 8004 --dtype float16 --mem-fraction-static 0.45 --context-length 2048 --max-running-requests 4 --chunked-prefill-size 512 --trust-remote-code --disable-cuda-graph --skip-server-warmup --log-level warning
Restart=on-failure
RestartSec=15
Environment=CUDA_VISIBLE_DEVICES=0
StandardOutput=append:/tmp/sglang.log
StandardError=append:/tmp/sglang.log
[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload 2>/dev/null
sudo systemctl enable sglang 2>/dev/null
sudo systemctl restart sglang 2>/dev/null

for i in $(seq 1 36); do
    sleep 5
    if curl -sf http://localhost:8004/v1/models >/dev/null 2>&1; then
        log "Qwen ✅ ($((i*5))s)"
        break
    fi
    [ $((i % 6)) -eq 0 ] && log "  cargando... ($((i*5))s) GPU0: $(nvidia-smi -i 0 --query-gpu=memory.used --format=noheader 2>/dev/null)"
done
log "⏸️  15s estabilización GPU 0"; sleep 15

# ═══════════════════════════════════════════════════════════════════════════════
# FASE 5: Voxtral (GPU 2)
# ═══════════════════════════════════════════════════════════════════════════════
echo ""; log "5/8 Voxtral (GPU 2)"
nohup bash -c "cd /home/sam/ai_system && CUDA_VISIBLE_DEVICES=2 $VENV/bin/python voxtral/src/serve.py --port 8010 --no-compile --flow-steps 3 --model-dir voxtral/models/original" >> "$LOG/voxtral.log" 2>&1 &
wait_port 8010 "Voxtral" 90
log "⏸️  15s estabilización GPU 2"; sleep 15

# ═══════════════════════════════════════════════════════════════════════════════
# FASE 6: ComfyUI (GPU 2) — solo si --flux
# ═══════════════════════════════════════════════════════════════════════════════
if [ "$FLUX" = true ]; then
    echo ""; log "6/8 ComfyUI (GPU 2)"
    nohup bash -c "cd /home/sam/ai_system/ComfyUI && CUDA_VISIBLE_DEVICES=2 ../venv/bin/python main.py --listen 0.0.0.0 --port 8006" >> "$LOG/comfyui.log" 2>&1 &
    wait_port 8006 "ComfyUI" 80
    log "⏸️  10s"; sleep 10

    # ═══════════════════════════════════════════════════════════════════════════
    # FASE 7: AudioLDM2 (GPU 2) — carga diferida
    # ═══════════════════════════════════════════════════════════════════════════
    echo ""; log "7/8 AudioLDM2 (GPU 2, carga diferida)"
    nohup bash -c "cd /home/sam/ai_system && CUDA_VISIBLE_DEVICES=2 HF_HOME=/home/sam/.cache/hf_models TORCH_COMPILE_DISABLE=1 $VENV/bin/python audioldm2_server.py" >> "$LOG/audioldm2.log" 2>&1 &
    wait_port 8009 "AudioLDM2" 20
    log "⏸️  10s"; sleep 10
else
    # Sin Flux: ComfyUI carga lazy (solo servidor, sin modelos en VRAM)
    echo ""; log "6/8 ComfyUI server (GPU 2, modelos bajo demanda)"
    setsid env CUDA_VISIBLE_DEVICES=2 HF_HOME=/home/sam/.cache/hf_models TORCH_COMPILE_DISABLE=1 $VENV/bin/python /home/sam/ai_system/ComfyUI/main.py --listen 0.0.0.0 --port 8006 --disable-cuda-malloc >> "$LOG/comfyui.log" 2>&1 &
    wait_port 8006 "ComfyUI" 60
    log "⏸️  10s"; sleep 10

# ═══════════════════════════════════════════════════════════════════════════════
# FASE 8: UI Server (HTTP)
# ═══════════════════════════════════════════════════════════════════════════════
echo ""; log "8/8 UI Server (HTTP :8080)"
nohup bash -c "cd /home/sam/ai_system && $VENV/bin/python -m uvicorn ui_server:APP --host 0.0.0.0 --port 8080 --log-level warning" >> "$LOG/ui_server.log" 2>&1 &
wait_port 8080 "UI Server" 15

# ═══════════════════════════════════════════════════════════════════════════════
# RESUMEN
# ═══════════════════════════════════════════════════════════════════════════════
echo ""; log "═══════════════════════════════════════════════════"
log "  RESUMEN FINAL"; log "═══════════════════════════════════════════════════"
for svc in "API:8005" "YOLO:8002" "Qwen:8004" "Whisper:8008" "Voxtral:8010" "ComfyUI:8006" "AudioLDM2:8009" "UI:8080"; do
    IFS=':' read -r name port <<< "$svc"
    printf "  %-14s :$port  " "$name"
    ss -tlnp 2>/dev/null | grep -q ":$port " && echo -e "${G}✅${NC}" || echo -e "${R}❌${NC}"
done
echo ""
log "RAM: $(free -m | awk '/^Mem:/{print $7}')MB libre"
nvidia-smi --query-gpu=index,memory.used,memory.free --format=csv,noheader 2>/dev/null
echo ""
log "Comandos: --status  --stop  --flux"
log "═══════════════════════════════════════════════════"
