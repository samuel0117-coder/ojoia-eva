#!/bin/bash
# Descargar modelos Wan 2.1 I2V 720P para ComfyUI
# Uso: bash download_wan.sh

set -e

BASE="https://huggingface.co/Comfy-Org/Wan_2.1_ComfyUI_repackaged/resolve/main/split_files"
DIR="/home/sam/ai_system/ComfyUI/models"

echo "=== Descargando Wan 2.1 I2V 720P (~38G total) ==="

# Limpiar descargas anteriores incompletas
rm -rf "$DIR/diffusion_models/.cache"
rm -f "$DIR/diffusion_models/wan2.1_*"
rm -f "$DIR/text_encoders/umt5_*"
rm -f "$DIR/vae/wan_2.1_*"
rm -f "$DIR/clip_vision/clip_vision_h*"

echo ""
echo "[1/5] UNet I2V (~13G)..."
wget -q --show-progress -O "$DIR/diffusion_models/wan2.1_i2v_720p_14B_fp8_scaled.safetensors" \
  "$BASE/diffusion_models/wan2.1_i2v_720p_14B_fp8_scaled.safetensors"

echo ""
echo "[2/5] UNet T2V (~13G)..."
wget -q --show-progress -O "$DIR/diffusion_models/wan2.1_t2v_720p_14B_fp8_scaled.safetensors" \
  "$BASE/diffusion_models/wan2.1_t2v_720p_14B_fp8_scaled.safetensors"

echo ""
echo "[3/5] Text Encoder (~9.5G)..."
wget -q --show-progress -O "$DIR/text_encoders/umt5_xxl_fp8_e4m3fn_scaled.safetensors" \
  "$BASE/text_encoders/umt5_xxl_fp8_e4m3fn_scaled.safetensors"

echo ""
echo "[4/5] VAE (~300M)..."
wget -q --show-progress -O "$DIR/vae/wan_2.1_vae.safetensors" \
  "$BASE/vae/wan_2.1_vae.safetensors"

echo ""
echo "[5/5] CLIP Vision (~2.5G)..."
wget -q --show-progress -O "$DIR/clip_vision/clip_vision_h.safetensors" \
  "$BASE/clip_vision/clip_vision_h.safetensors"

echo ""
echo "=== ¡Listo! ==="
ls -lh "$DIR/diffusion_models/wan2.1_*" "$DIR/text_encoders/umt5_*" "$DIR/vae/wan_2.1_*" "$DIR/clip_vision/clip_vision_h*"
echo ""
echo "Reinicia ComfyUI: systemctl --user restart comfyui.service"
