#!/bin/bash
# Start all GPU 2 services
set -e

echo "=== GPU 2 Service Manager ==="
echo ""

# Kill any existing processes on GPU 2
echo "[1] Killing existing processes..."
pkill -f "voxtral" 2>/dev/null || true
pkill -f "audioldm2" 2>/dev/null || true
pkill -f "flux_server" 2>/dev/null || true
sleep 2

# Release VRAM
nvidia-smi -i 2 -r 2>/dev/null || true
sleep 1

echo "[2] Starting ComfyUI..."
systemctl --user start comfyui.service
sleep 8

echo "[3] Starting Voxtral..."
systemctl --user start voxtral.service
sleep 8

# Opcional: arrancar AudioLDM2 solo bajo demanda (VRAM)
# systemctl --user start audioldm2.service

echo ""
echo "=== STATUS ==="
echo "ComfyUI: $(systemctl --user is-active comfyui.service)"
echo "Voxtral: $(systemctl --user is-active voxtral.service)"
echo "AudioLDM2: $(systemctl --user is-active audioldm2.service 2>/dev/null || echo 'stopped')"
echo ""
echo "GPU 2 Memory: $(nvidia-smi -i 2 --query-gpu=memory.used --format=csv,noheader)"
echo ""
echo "All services started."
