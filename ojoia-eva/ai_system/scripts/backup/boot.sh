#!/bin/bash
# /home/sam/ai_system/scripts/boot.sh — Arranque escalonado v6
# GPU 0: Qwen only
# GPU 1: Whisper + SDXL
# GPU 2: ComfyUI + Voxtral + AudioLDM2

LOG="/home/sam/ai_system/logs"
mkdir -p "$LOG"

G='\033[0;32m'; R='\033[0;31m'; Y='\033[1;33m'; NC='\033[0m'
log()  { echo -e "${G}[$(date +%H:%M:%S)] $1${NC}"; }
warn() { echo -e "${Y}[$(date +%H:%M:%S)] $1${NC}"; }

wait_port() {
    local port=$1 name=$2 max=${3:-30}
    for i in $(seq 1 $max); do
        sleep 1
        if ss -tlnp 2>/dev/null | grep -q ":$port "; then
            log "  $name ✅ ($((i))s)"
            return 0
        fi
    done
    warn "  $name ❌ timeout"
    return 1
}

# ═══════════════════════════════════════════════════════════════════════════
# FASE 0: Cloudflare Tunnel (debe estar antes que todo)
# ═══════════════════════════════════════════════════════════════════════════
echo ""; log "0/7 Cloudflare Tunnel"
# Asegurar que no haya instancias previas
pkill -f "cloudflared tunnel" 2>/dev/null || true
sleep 2
# Iniciar túnel con systemd (Restart=always se mantiene activo)
systemctl --user start tunnel.service 2>/dev/null || true
# Esperar a que cloudflared esté corriendo
for i in $(seq 1 30); do
    sleep 1
    if pgrep -f "cloudflared tunnel" >/dev/null 2>&1; then
        log "  Tunnel ✅ (${i}s)"
        break
    fi
done
log "⏸️  15s estabilización DNS..."; sleep 15

# ═══════════════════════════════════════════════════════════════════════════
# FASE 1: API Eva (CPU)
# ═══════════════════════════════════════════════════════════════════════════
# FASE 1: API Eva (CPU)
# ═══════════════════════════════════════════════════════════════════════════
echo ""; log "1/6 API Eva (CPU)"
nohup bash -c "/home/sam/ai_system/venv/bin/python -u /home/sam/ai_system/api_eva.py" >> "$LOG/api_eva.log" 2>&1 &
wait_port 8005 "API Eva" 10
sleep 5

# ═══════════════════════════════════════════════════════════════════════════
# FASE 2: YOLO (CPU)
# ═══════════════════════════════════════════════════════════════════════════
echo ""; log "2/6 YOLO (CPU)"
nohup bash -c "cd /home/sam/ai_system && CUDA_VISIBLE_DEVICES='' /home/sam/ai_system/venv/bin/python -u yolo_server.py" >> "$LOG/yolo.log" 2>&1 &
wait_port 8002 "YOLO" 10
sleep 5

# ═══════════════════════════════════════════════════════════════════════════
# FASE 3: SGLang/Qwen (GPU 0) — PESADO
# ═══════════════════════════════════════════════════════════════════════════
echo ""; log "3/6 SGLang/Qwen (GPU 0) — cargando (~60-90s)"
nohup bash -c "cd /home/sam/ai_system && CUDA_VISIBLE_DEVICES=0 /home/sam/ai_system/venv/bin/sglang serve --model-path /opt/ai_models/Qwen2.5-VL-7B-Instruct-AWQ --host 0.0.0.0 --port 8004 --dtype float16 --mem-fraction-static 0.45 --context-length 2048 --max-running-requests 4 --chunked-prefill-size 512 --trust-remote-code --disable-cuda-graph --skip-server-warmup --log-level warning" >> "$LOG/sglang.log" 2>&1 &

for i in $(seq 1 24); do
    sleep 5
    if curl -sf http://localhost:8004/v1/models >/dev/null 2>&1; then
        log "  Qwen ✅ ($((i*5))s)"
        break
    fi
    [ $((i % 6)) -eq 0 ] && log "  cargando... ($((i*5))s) GPU0: $(nvidia-smi -i 0 --query-gpu=memory.used --format=noheader 2>/dev/null)"
done
sleep 10

# ═══════════════════════════════════════════════════════════════════════════
# FASE 4: Whisper + SDXL (GPU 1)
# ═══════════════════════════════════════════════════════════════════════════
echo ""; log "4/6 Whisper + SDXL (GPU 1)"

nohup bash -c "cd /home/sam/ai_system && CUDA_VISIBLE_DEVICES=1 HF_HOME=/home/sam/.cache/hf_models /home/sam/ai_system/venv/bin/python -u whisper_server.py" >> "$LOG/whisper.log" 2>&1 &

nohup bash -c "
cd /home/sam/ai_system
source venv/bin/activate
export CUDA_VISIBLE_DEVICES=1
export HF_HOME=/home/sam/.cache/hf_models
export TORCH_COMPILE_DISABLE=1
python -u -c '
import torch, time
from diffusers import StableDiffusionXLPipeline
print(\"[SDXL] Cargando en GPU 1...\")
pipe = StableDiffusionXLPipeline.from_pretrained(
    \"stabilityai/stable-diffusion-xl-base-1.0\",
    torch_dtype=torch.float16,
    use_safetensors=True,
    variant=\"fp16\"
)
pipe.to(\"cuda\")
vram = torch.cuda.memory_allocated()/1024**3
print(f\"[SDXL] listo | VRAM: {vram:.1f}GB\")
while True: time.sleep(9999)
'" >> "$LOG/sdxl_boot.log" 2>&1 &

wait_port 8008 "Whisper" 15
log "⏸️  60s SDXL cargando en background..."; sleep 60

# ═══════════════════════════════════════════════════════════════════════════
# FASE 5: ComfyUI + Voxtral + AudioLDM2 (GPU 2)
# ═══════════════════════════════════════════════════════════════════════════
echo ""; log "5/6 ComfyUI + Voxtral + AudioLDM2 (GPU 2)"

setsid env CUDA_VISIBLE_DEVICES=2 HF_HOME=/home/sam/.cache/hf_models TORCH_COMPILE_DISABLE=1 /home/sam/ai_system/venv/bin/python /home/sam/ai_system/ComfyUI/main.py --listen 0.0.0.0 --port 8006 --disable-cuda-malloc >> "$LOG/comfyui.log" 2>&1 &
setsid env CUDA_VISIBLE_DEVICES=2 /home/sam/ai_system/venv/bin/python /home/sam/ai_system/voxtral/src/serve.py --port 8010 --no-compile --flow-steps 3 --model-dir voxtral/models/original >> "$LOG/voxtral.log" 2>&1 &
nohup bash -c "cd /home/sam/ai_system && CUDA_VISIBLE_DEVICES=2 HF_HOME=/home/sam/.cache/hf_models TORCH_COMPILE_DISABLE=1 /home/sam/ai_system/venv/bin/python audioldm2_server.py" >> "$LOG/audioldm2.log" 2>&1 &

wait_port 8006 "ComfyUI" 60
wait_port 8010 "Voxtral" 90
wait_port 8009 "AudioLDM2" 20
sleep 10

# ═══════════════════════════════════════════════════════════════════════════
# FASE 6: UI Server (HTTP :8080)
# ═══════════════════════════════════════════════════════════════════════════
echo ""; log "6/6 UI Server (HTTP :8080)"
nohup bash -c "cd /home/sam/ai_system && /home/sam/ai_system/venv/bin/python -m uvicorn ui_server:APP --host 0.0.0.0 --port 8080 --log-level warning" >> "$LOG/ui_server.log" 2>&1 &
wait_port 8080 "UI Server" 15

# ═══════════════════════════════════════════════════════════════════════════
# RESUMEN
# ═══════════════════════════════════════════════════════════════════════════
echo ""; log "═══════════════════════════════════════════"
log "  BOOT COMPLETO v5"
log "═══════════════════════════════════════════"
for svc in "API:8005" "YOLO:8002" "Qwen:8004" "Whisper:8008" "ComfyUI:8006" "Voxtral:8010" "AudioLDM2:8009" "UI:8080"; do
    IFS=':' read -r name port <<< "$svc"
    printf "  %-12s :$port  " "$name"
    ss -tlnp 2>/dev/null | grep -q ":$port " && echo -e "${G}✅${NC}" || echo -e "${R}❌${NC}"
done
echo ""
log "RAM: $(free -m | awk '/^Mem:/{print $7}')MB libres"
nvidia-smi --query-gpu=index,memory.used,memory.free --format=csv,noheader 2>/dev/null
echo ""
log "GPU 0: Qwen | GPU 1: Whisper+SDXL | GPU 2: ComfyUI+Voxtral+AudioLDM2"
