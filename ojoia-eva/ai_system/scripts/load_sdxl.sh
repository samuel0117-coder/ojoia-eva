#!/bin/bash
# Carga SDXL en GPU 1 bajo demanda
# Se llama desde la UI cuando se pide generar imagen

SDXL_CKPT="${1:-sd_xl_turbo_1.0_fp16.safetensors}"
LOG="/home/sam/ai_system/logs/sdxl_load.log"
VENV="/home/sam/ai_system/venv"

echo "[$(date)] Cargando $SDXL_CKPT en GPU 1..." >> "$LOG"

# Cargar via ComfyUI workflow
curl -s -X POST http://localhost:8006/prompt -H "Content-Type: application/json" \
  -d "{
    \"prompt\": {
      \"3\": {\"class_type\": \"CheckpointLoaderSimple\", \"inputs\": {\"ckpt_name\": \"$SDXL_CKPT\"}},
      \"4\": {\"class_type\": \"EmptyLatentImage\", \"inputs\": {\"width\": 512, \"height\": 512, \"batch_size\": 1}},
      \"5\": {\"class_type\": \"VAEDecode\", \"inputs\": {\"samples\": [\"3\", 0], \"vae\": [\"3\", 2]}}
    }
  }" >> "$LOG"

echo "[$(date)] SDXL cargado" >> "$LOG"
