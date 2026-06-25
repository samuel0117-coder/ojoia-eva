#!/bin/bash
# /home/sam/ai_system/scripts/gpu2_manager.sh
# Gestiona la carga/descarga de modelos en GPU 2
# Uso: bash gpu2_manager.sh [status|light|heavy|clean|restore]

ACTION="${1:-status}"
LOG="/home/sam/ai_system/logs/gpu2_manager.log"

log() { echo "[$(date +%H:%M:%S)] $1" | tee -a "$LOG"; }

get_gpu2_used() {
    nvidia-smi -i 2 --query-gpu=memory.used --format=noheader,nounits 2>/dev/null | tr -d ' '
}

case "$ACTION" in
    status)
        echo "=== GPU 2 Status ==="
        nvidia-smi -i 2 --query-gpu=memory.used,memory.free --format=csv,noheader,nounits
        echo ""
        echo "=== Procesos en GPU 2 ==="
        nvidia-smi -i 2 --query-compute-apps=pid,used_memory --format=csv,noheader 2>/dev/null | \
        while IFS=',' read pid mem; do
            cmd=$(cat /proc/$pid/cmdline 2>/dev/null | tr '\0' ' ' | head -c 60)
            echo "  PID=$pid MEM=$mem $cmd"
        done
        echo ""
        echo "=== Servicios ==="
        for svc in comfyui voxtral audioldm2; do
            status=$(systemctl is-active ${svc}.service 2>/dev/null || echo "inactive")
            echo "  $svc: $status"
        done
        ;;

    light)
        log "Cargando servicios ligeros en GPU 2..."
        systemctl start voxtral.service 2>/dev/null
        sleep 60
        systemctl start audioldm2.service 2>/dev/null
        sleep 10
        log "Servicios ligeros listos. VRAM: $(get_gpu2_used)MB"
        ;;

    heavy)
        log "Preparando GPU 2 para modelo grande..."
        systemctl stop voxtral.service 2>/dev/null
        sleep 2
        systemctl stop audioldm2.service 2>/dev/null
        sleep 2
        python3 -c "
import torch
if torch.cuda.is_available():
    torch.cuda.empty_cache()
    torch.cuda.synchronize()
    print('CUDA cache limpiado')
" 2>/dev/null
        log "GPU 2 lista para modelo grande. VRAM: $(get_gpu2_used)MB usados"
        log "Cuando termines, ejecutá: bash $0 restore"
        ;;

    restore)
        log "Restaurando servicios ligeros en GPU 2..."
        systemctl start voxtral.service 2>/dev/null
        sleep 5
        systemctl start audioldm2.service 2>/dev/null
        sleep 10
        log "Servicios restaurados. VRAM: $(get_gpu2_used)MB"
        ;;

    clean)
        log "Limpiando GPU 2 completamente..."
        systemctl stop voxtral.service 2>/dev/null
        systemctl stop audioldm2.service 2>/dev/null
        systemctl stop comfyui.service 2>/dev/null
        sleep 3
        python3 -c "
import torch
if torch.cuda.is_available():
    torch.cuda.empty_cache()
    print('CUDA cache limpiado')
" 2>/dev/null
        log "GPU 2 limpia. VRAM: $(get_gpu2_used)MB"
        ;;

    *)
        echo "Uso: $0 {status|light|heavy|clean|restore}"
        echo "  status  - Ver estado de GPU 2"
        echo "  light   - Cargar servicios ligeros (Voxtral + AudioLDM2)"
        echo "  heavy   - Preparar para modelo grande (para ligeros, limpia cache)"
        echo "  restore - Restaurar servicios ligeros después de modelo grande"
        echo "  clean   - Limpiar todo de GPU 2"
        ;;
esac
