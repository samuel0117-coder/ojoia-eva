#!/bin/bash
# /home/sam/ai_system/scripts/gpu2_offload.sh
# Gestiona recursos de GPU 2 para modelos pesados (Wan/Flux)
# Uso: bash gpu2_offload.sh [status|free|reclaim]

echo "=== GPU 2 Resource Manager ==="
echo ""

case "${1:-status}" in
    status)
        echo "GPU 2 memory:"
        nvidia-smi -i 2 --query-gpu=memory.used,memory.total --format=csv,noheader
        echo ""
        echo "GPU 2 procesos:"
        nvidia-smi -i 2 --query-compute-apps=pid,name,used_memory --format=csv,noheader 2>/dev/null || echo "  Ninguno"
        echo ""
        echo "Servicios:"
        for svc in comfyui voxtral audioldm2; do
            status=$(systemctl is-active ${svc}.service 2>/dev/null || echo "inactive")
            echo "  $svc: $status"
        done
        ;;

    free)
        echo "Liberando GPU 2 para modelo pesado (Wan/Flux)..."
        systemctl stop voxtral.service 2>/dev/null
        systemctl stop audioldm2.service 2>/dev/null
        sleep 3
        echo ""
        echo "GPU 2 después de limpiar:"
        nvidia-smi -i 2 --query-gpu=memory.used,memory.total --format=csv,noheader
        echo ""
        echo "GPU 0/1 sin tocar:"
        nvidia-smi -i 0,1 --query-gpu=index,memory.used --format=csv,noheader,nounits
        ;;

    reclaim)
        echo "Restaurando Voxtral + AudioLDM2 en GPU 2..."
        systemctl start voxtral.service
        systemctl start audioldm2.service
        sleep 5
        echo ""
        echo "GPU 2 memory:"
        nvidia-smi -i 2 --query-gpu=memory.used,memory.total --format=csv,noheader
        echo ""
        curl -sf -o /dev/null -w "Voxtral: HTTP %{http_code}\n" http://127.0.0.1:8010/health
        curl -sf -o /dev/null -w "AudioLDM2: HTTP %{http_code}\n" http://127.0.0.1:8009/health
        ;;

    *)
        echo "Uso: $0 {status|free|reclaim}"
        ;;
esac
