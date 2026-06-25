#!/bin/bash
# /home/sam/ai_system/scripts/switch_sdxl.sh
# Cambia SDXL Turbo ↔ JuggernautXL en GPU 1
# Uso: bash switch_sdxl.sh [turbo|juggernaut]

MODE="${1:-turbo}"
LOG="/home/sam/ai_system/logs/sdxl_switch.log"

if [ "$MODE" = "juggernaut" ]; then
    CKPT="JuggernautXL_v10.safetensors"
    STEPS=30
    CFG=5.5
else
    CKPT="sd_xl_turbo_1.0_fp16.safetensors"
    STEPS=4
    CFG=1.5
fi

echo "[$(date +%H:%M:%S)] Cambiando SDXL a: $MODE ($CKPT)" | tee -a "$LOG"

# Reiniciar el servicio SDXL (systemd se encarga de matar el viejo y cargar el nuevo)
systemctl restart sdxl.service

# Esperar a que el puerto esté listo
for i in $(seq 1 60); do
    sleep 2
    if ss -tlnp 2>/dev/null | grep -q ":8011 "; then
        echo "[$(date +%H:%M:%S)] SDXL listo ($((i*2))s)" | tee -a "$LOG"
        break
    fi
done

# Verificar
GPU_USED=$(nvidia-smi -i 1 --query-gpu=memory.used --format=csv,noheader,nounits 2>/dev/null | tr -d ' ')
echo "[$(date +%H:%M:%S)] SDXL activo: GPU1=${GPU_USED}MB" | tee -a "$LOG"
echo ""
echo "✅ SDXL cambiado a: $CKPT"
echo "   GPU 1: ${GPU_USED}MB"
echo "   Steps: $STEPS | CFG: $CFG"
echo ""
echo "Nota: El modelo se carga desde sdxl_server.py según la configuración interna."
echo "Para cambiar el checkpoint editá sdxl_server.py o usá POST /api/sdxl/switch"
