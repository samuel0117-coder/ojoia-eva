#!/usr/bin/env bash
set -euo pipefail
cd /home/sam/ai_system
exec env \
  CUDA_VISIBLE_DEVICES=2 \
  /home/sam/ai_system/venv_wan/bin/python \
  /home/sam/ai_system/ComfyUI_wan/main.py \
  --listen 0.0.0.0 \
  --port 8006 \
  --cuda-device 2 \
  --fp16-vae \
  --preview-method none \
  --disable-auto-launch \
  --reserve-vram 8 \
  --disable-cuda-malloc \
  --disable-async-offload \
  > /tmp/comfyui_wan.log 2>&1
