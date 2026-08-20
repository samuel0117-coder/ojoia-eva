#!/bin/bash
# /home/sam/ai_system/start_simple.sh — Arranque simple de a uno
set -e

VENV="/home/sam/ai_system/venv"
LOG="/home/sam/ai_system/logs"
mkdir -p "$LOG"

echo "=== Matando procesos anteriores ==="
pkill -9 -f "whisper_server\|voxtral\|ComfyUI\|audioldm2\|api_eva\|yolo\|ai_server\|project_server\|sglang\|StableDiffusion\|uvicorn" 2>/dev/null || true
sleep 3
nvidia-smi --query-gpu=index,memory.used --format=csv,noheader,nounits
echo ""

echo "=== 1/6 API Eva (CPU) ==="
nohup $VENV/bin/python -u /home/sam/ai_system/api_eva.py >> $LOG/api_eva.log 2>&1 &
sleep 12 && curl -sf http://127.0.0.1:8005/health >/dev/null && echo "API ✅" || echo "API ❌"

echo "=== 2/6 Qwen (GPU 0) — 90s ==="
cd /home/sam/ai_system && CUDA_VISIBLE_DEVICES=0 nohup $VENV/bin/sglang serve \
  --model-path /opt/ai_models/Qwen2.5-VL-7B-Instruct-AWQ \
  --host 0.0.0.0 --port 8004 \
  --dtype float16 --mem-fraction-static 0.45 \
  --context-length 2048 --max-running-requests 4 \
  --chunked-prefill-size 512 --trust-remote-code \
  --disable-cuda-graph --skip-server-warmup --log-level warning \
  >> $LOG/sglang.log 2>&1 &
for i in $(seq 1 24); do sleep 5
    curl -sf http://127.0.0.1:8004/v1/models >/dev/null 2>&1 && echo "Qwen ✅ ($((i*5))s)" && break
done
nvidia-smi -i 0 --query-gpu=memory.used --format=noheader,nounits | xargs echo "GPU0:"

echo "=== 3/6 Whisper (GPU 1) ==="
CUDA_VISIBLE_DEVICES=1 HF_HOME=/home/sam/.cache/hf_models nohup $VENV/bin/python -u /home/sam/ai_system/whisper_server.py >> $LOG/whisper.log 2>&1 &
sleep 15 && curl -sf http://127.0.0.1:8008/health >/dev/null && echo "Whisper ✅" || echo "Whisper ❌"

echo "=== 4/6 ComfyUI (GPU 2) ==="
setsid env CUDA_VISIBLE_DEVICES=2 HF_HOME=/home/sam/.cache/hf_models TORCH_COMPILE_DISABLE=1 $VENV/bin/python /home/sam/ai_system/ComfyUI/main.py --listen 0.0.0.0 --port 8006 --disable-cuda-malloc >> $LOG/comfyui.log 2>&1 &
sleep 20 && ss -tlnp | grep -q :8006 && echo "ComfyUI ✅" || echo "ComfyUI ❌"

echo "=== 5/6 Voxtral (GPU 2) — 90s ==="
setsid env CUDA_VISIBLE_DEVICES=2 $VENV/bin/python /home/sam/ai_system/voxtral/src/serve.py --port 8010 --no-compile --flow-steps 3 --model-dir voxtral/models/original >> $LOG/voxtral.log 2>&1 &
for i in $(seq 1 18); do sleep 5
    ss -tlnp | grep -q :8010 && echo "Voxtral ✅ ($((i*5))s)" && break
done

echo "=== 6/6 Resto (AudioLDM2, YOLO, UI, Proxy, Projects) ==="
CUDA_VISIBLE_DEVICES=2 HF_HOME=/home/sam/.cache/hf_models TORCH_COMPILE_DISABLE=1 nohup $VENV/bin/python /home/sam/ai_system/audioldm2_server.py >> $LOG/audioldm2.log 2>&1 &
sleep 10 && ss -tlnp | grep -q :8009 && echo "AudioLDM2 ✅" || echo "AudioLDM2 ❌"

CUDA_VISIBLE_DEVICES='' nohup $VENV/bin/python -u /home/sam/ai_system/yolo_server.py >> $LOG/yolo.log 2>&1 &
sleep 8 && ss -tlnp | grep -q :8002 && echo "YOLO ✅" || echo "YOLO ❌"

nohup $VENV/bin/python -m uvicorn ui_server:APP --host 0.0.0.0 --port 8080 --log-level warning >> $LOG/ui_server.log 2>&1 &
sleep 8 && ss -tlnp | grep -q :8080 && echo "UI ✅" || echo "UI ❌"

nohup $VENV/bin/python -u /home/sam/ai_server.py >> $LOG/ai_server.log 2>&1 &
sleep 5 && ss -tlnp | grep -q :8090 && echo "Proxy ✅" || echo "Proxy ❌"

nohup $VENV/bin/python -m uvicorn project_server:app --host 127.0.0.1 --port 8012 >> $LOG/project_server.log 2>&1 &
sleep 5 && ss -tlnp | grep -q :8012 && echo "Projects ✅" || echo "Projects ❌"

echo ""
echo "=== RESUMEN ==="
for port in 8002 8004 8005 8006 8008 8009 8010 8012 8080 8090; do
    ss -tlnp 2>/dev/null | grep -q ":$port " && echo "  $port ✅" || echo "  $port ❌"
done
echo ""
nvidia-smi --query-gpu=index,memory.used,memory.free --format=csv,noheader,nounits
echo ""
free -h | grep "^Mem"
