#!/bin/bash
# /home/sam/ai_system/scripts/start_arranque.sh
# ARRANQUE ESCALONADO ESTABLE v13
# Orden: Túnel → API → Proxy → YOLO → Qwen → Whisper → SDXL → ComfyUI → Voxtral → AudioLDM2 → UI → Projects
# Política: si un servicio crítico falla, el script para y avisa

VENV="/home/sam/ai_system/venv"
VENV_VOX="/home/sam/ai_system/voxtral/venv"
LOG="/home/sam/ai_system/logs"
mkdir -p "$LOG"

G='\033[0;32m'; R='\033[0;31m'; Y='\033[1;33m'; NC='\033[0m'
log()  { echo -e "${G}[$(date +%H:%M:%S)]${NC} $1"; }
warn() { echo -e "${Y}[$(date +%H:%M:%S)] ⚠️  $1${NC}"; }
fail() { echo -e "${R}[$(date +%H:%M:%S)] ❌ $1${NC}"; }

wait_port() {
    local port=$1 name=$2 max=${3:-30}
    for i in $(seq 1 $max); do
        sleep 2
        if ss -tlnp 2>/dev/null | grep -q ":$port "; then
            log "  $name ✅ ($((i*2))s)"
            return 0
        fi
    done
    fail "  $name ❌ timeout"
    return 1
}

wait_gpu_min() {
    local gpu=$1 min_mb=$2 max_wait=${3:-120}
    for i in $(seq 1 $((max_wait/5))); do
        sleep 5
        used=$(nvidia-smi -i $gpu --query-gpu=memory.used --format=noheader,nounits 2>/dev/null | tr -d ' ')
        if [ "$used" -ge "$min_mb" ] 2>/dev/null; then
            log "  GPU $gpu estable: ${used}MB"
            return 0
        fi
    done
    fail "  GPU $gpu no cargó (${used}MB < ${min_mb}MB)"
    return 1
}

echo ""
log "═══════════════════════════════════════════════════"
log "  ARRANQUE ESCALONADO v13"
log "═══════════════════════════════════════════════════"

# ── LIMPIEZA ───────────────────────────────────────────────────────────────────
log "Limpiando..."
pkill -9 -f "yolo_server\|whisper_server\|voxtral\|ComfyUI/main\|audioldm2\|api_eva\|ai_server\|project_server\|sglang\|sdxl_server\|StableDiffusion\|uvicorn" 2>/dev/null || true
sleep 3
nvidia-smi --query-gpu=index,memory.used --format=csv,noheader,nounits
echo ""

# ═══════════════════════════════════════════════════════════════════════════════
# 1. TÚNEL
# ═══════════════════════════════════════════════════════════════════════════════
log "1/12 Túnel Cloudflare"
pkill -f "cloudflared tunnel" 2>/dev/null || true
sleep 2
systemctl --user start tunnel.service 2>/dev/null || true
for i in $(seq 1 30); do
    sleep 1
    pgrep -f "cloudflared tunnel" >/dev/null 2>&1 && log "  Túnel ✅ (${i}s)" && break
done
log "⏸️  15s DNS..."; sleep 15

# ═══════════════════════════════════════════════════════════════════════════════
# 2. API EVA (CPU) — CRÍTICO
# ═══════════════════════════════════════════════════════════════════════════════
echo ""; log "2/12 API Eva (CPU)"
nohup $VENV/bin/python -u /home/sam/ai_system/api_eva.py >> $LOG/api_eva.log 2>&1 &
wait_port 8005 "API Eva" 15 || { fail "API Eva falló. Abortando."; exit 1; }
sleep 5

# ═══════════════════════════════════════════════════════════════════════════════
# 3. AI PROXY (CPU)
# ═══════════════════════════════════════════════════════════════════════════════
echo ""; log "3/12 AI Proxy (:8090)"
nohup $VENV/bin/python -u /home/sam/ai_server.py >> $LOG/ai_server.log 2>&1 &
wait_port 8090 "Proxy" 10 || warn "Proxy no crítico"

# ═══════════════════════════════════════════════════════════════════════════════
# 4. YOLO (CPU)
# ═══════════════════════════════════════════════════════════════════════════════
echo ""; log "4/12 YOLO (CPU)"
nohup bash -c "cd /home/sam/ai_system && CUDA_VISIBLE_DEVICES='' $VENV/bin/python -u yolo_server.py" >> $LOG/yolo.log 2>&1 &
wait_port 8002 "YOLO" 10 || warn "YOLO no crítico"
sleep 3

# ═══════════════════════════════════════════════════════════════════════════════
# 5. QWEN 7B (GPU 0) — CRÍTICO, PESADO
# ═══════════════════════════════════════════════════════════════════════════════
echo ""; log "5/12 Qwen 7B (GPU 0) — PESADO (~90s)"
cd /home/sam/ai_system && CUDA_VISIBLE_DEVICES=0 nohup $VENV/bin/sglang serve \
  --model-path /opt/ai_models/Qwen2.5-VL-7B-Instruct-AWQ \
  --host 0.0.0.0 --port 8004 \
  --dtype float16 --mem-fraction-static 0.40 \
  --context-length 2048 --max-running-requests 4 \
  --chunked-prefill-size 512 --trust-remote-code \
  --disable-cuda-graph --skip-server-warmup --log-level warning \
  >> $LOG/sglang.log 2>&1 &
wait_gpu_min 0 8000 180 || { fail "Qwen no cargó en GPU. Abortando."; exit 1; }
curl -sf http://127.0.0.1:8004/v1/models >/dev/null && log "  Qwen API ✅" || warn "Qwen API no responde"
sleep 10

# ═══════════════════════════════════════════════════════════════════════════════
# 6. WHISPER (GPU 1)
# ═══════════════════════════════════════════════════════════════════════════════
echo ""; log "6/12 Whisper (GPU 1)"
CUDA_VISIBLE_DEVICES=1 HF_HOME=/home/sam/.cache/hf_models \
  nohup $VENV/bin/python -u /home/sam/ai_system/whisper_server.py \
  >> $LOG/whisper.log 2>&1 &
wait_port 8008 "Whisper" 20 || { fail "Whisper falló. Abortando."; exit 1; }
sleep 5

# ═══════════════════════════════════════════════════════════════════════════════
# 7. SDXL TURBO (GPU 1) — Carga checkpoint local
# ═══════════════════════════════════════════════════════════════════════════════
echo ""; log "7/12 SDXL Turbo (GPU 1) — checkpoint local"
CUDA_VISIBLE_DEVICES=1 HF_HOME=/home/sam/.cache/hf_models \
  nohup $VENV/bin/python -u /home/sam/ai_system/sdxl_server.py \
  >> $LOG/sdxl.log 2>&1 &
wait_port 8011 "SDXL" 60 || warn "SDXL no crítico"
wait_gpu_min 1 7000 60 || warn "SDXL GPU baja"
sleep 5

# ═══════════════════════════════════════════════════════════════════════════════
# 8. COMFYUI (GPU 2) — Servidor base
# ═══════════════════════════════════════════════════════════════════════════════
echo ""; log "8/12 ComfyUI (GPU 2)"
setsid env CUDA_VISIBLE_DEVICES=2 HF_HOME=/home/sam/.cache/hf_models TORCH_COMPILE_DISABLE=1 \
  $VENV/bin/python /home/sam/ai_system/ComfyUI/main.py \
  --listen 0.0.0.0 --port 8006 --disable-cuda-malloc \
  >> $LOG/comfyui.log 2>&1 &
wait_port 8006 "ComfyUI" 60 || { fail "ComfyUI falló. Abortando."; exit 1; }
sleep 5

# ═══════════════════════════════════════════════════════════════════════════════
# 9. VOXTRAL (GPU 2) — PESADO, venv separado
# ═══════════════════════════════════════════════════════════════════════════════
echo ""; log "9/12 Voxtral (GPU 2) — PESADO (~90s)"
setsid env CUDA_VISIBLE_DEVICES=2 HF_HOME=/home/sam/.cache/hf_models \
  PYTORCH_CUDA_ALLOC_CONF=max_split_size_mb:256 \
  $VENV_VOX/bin/python /home/sam/ai_system/voxtral/src/serve.py \
  --port 8010 --no-compile --flow-steps 3 \
  >> $LOG/voxtral.log 2>&1 &
wait_gpu_min 2 3000 120 || warn "Voxtral GPU baja"
ss -tlnp | grep -q :8010 && log "  Voxtral API ✅" || warn "Voxtral API no responde"
sleep 10

# ═══════════════════════════════════════════════════════════════════════════════
# 10. AUDIODLDM2 (GPU 2) — Carga diferida
# ═══════════════════════════════════════════════════════════════════════════════
echo ""; log "10/12 AudioLDM2 (GPU 2, carga diferida)"
CUDA_VISIBLE_DEVICES=2 HF_HOME=/home/sam/.cache/hf_models TORCH_COMPILE_DISABLE=1 \
  nohup $VENV/bin/python /home/sam/ai_system/audioldm2_server.py \
  >> $LOG/audioldm2.log 2>&1 &
wait_port 8009 "AudioLDM2" 20 || warn "AudioLDM2 no crítico"
sleep 5

# ═══════════════════════════════════════════════════════════════════════════════
# 11. UI SERVER (CPU)
# ═══════════════════════════════════════════════════════════════════════════════
echo ""; log "11/12 UI Server (:8080)"
nohup bash -c "cd /home/sam/ai_system && $VENV/bin/python -m uvicorn ui_server:APP --host 0.0.0.0 --port 8080 --log-level warning" >> $LOG/ui_server.log 2>&1 &
wait_port 8080 "UI Server" 15 || warn "UI no crítico"

# ═══════════════════════════════════════════════════════════════════════════════
# 12. PROJECT SERVER (CPU)
# ═══════════════════════════════════════════════════════════════════════════════
echo ""; log "12/12 Project Server (:8012)"
cd /home/sam/ai_system && PYTHONPATH=/home/sam/ai_system \
  nohup $VENV/bin/python -m uvicorn project_server:app \
  --host 127.0.0.1 --port 8012 \
  >> $LOG/project_server.log 2>&1 &
wait_port 8012 "Projects" 10 || warn "Projects no crítico"

# ═══════════════════════════════════════════════════════════════════════════════
# RESUMEN
# ═══════════════════════════════════════════════════════════════════════════════
echo ""
log "═══════════════════════════════════════════════════"
log "  RESUMEN FINAL"
log "═══════════════════════════════════════════════════"
for svc in "API:8005" "Proxy:8090" "YOLO:8002" "Qwen:8004" "Whisper:8008" "SDXL:8011" "ComfyUI:8006" "Voxtral:8010" "AudioLDM2:8009" "UI:8080" "Projects:8012"; do
    IFS=':' read -r name port <<< "$svc"
    printf "  %-14s :$port  " "$name"
    ss -tlnp 2>/dev/null | grep -q ":$port " && echo -e "${G}✅${NC}" || echo -e "${R}❌${NC}"
done
echo ""
log "GPUs:"
nvidia-smi --query-gpu=index,memory.used,memory.free --format=csv,noheader,nounits 2>/dev/null | while IFS=',' read idx used free; do
    log "  GPU $idx: ${used}MB / ${free}MB libres"
done
echo ""
log "RAM: $(free -m | awk '/^Mem:/{print $7}')MB libres"
echo ""
log "Para detener: pkill -9 -f 'yolo_server\|whisper_server\|voxtral\|ComfyUI\|audioldm2\|api_eva\|ai_server\|project_server\|sglang\|sdxl_server\|uvicorn'"
log "═══════════════════════════════════════════════════"
