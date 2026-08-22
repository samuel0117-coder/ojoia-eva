#!/usr/bin/env bash
# deploy.sh — Deploy del backend multi-nodo
# Uso: ./deploy.sh [ojoia|cineia]
set -euo pipefail

NODE="${1:-ojoia}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "=== Deploy nodo: $NODE ==="

case "$NODE" in
  ojoia)
    echo ">>> Actualizando servicios en OJOIA..."
    # Servicios que corren en ojoia
    for svc in qwen.service qwen9b.service qwen35b.service whisper.service yolo-server.service api-eva.service health-monitor.service service_bus.service megapanel.service; do
      if [ -f "/etc/systemd/system/$svc" ]; then
        sudo systemctl restart "$svc" 2>/dev/null && echo "  reiniciado: $svc" || echo "  no encontrado: $svc"
      fi
    done
    echo ">>> OJOIA deploy OK"
    ;;
  cineia)
    echo ">>> Actualizando agente en CINEIA..."
    # Servicios que corren en cineia
    for svc in comfyui.service movie_server.service cineia_studio_server.service post_server.service audio_server.service f5_tts_server.service gpu1-monitor.service cineia-agent.service; do
      if [ -f "/etc/systemd/system/$svc" ] || [ -f "$HOME/.config/systemd/user/$svc" ]; then
        systemctl --user restart "$svc" 2>/dev/null && echo "  reiniciado: $svc" || echo "  no encontrado: $svc"
      fi
    done
    echo ">>> CINEIA deploy OK"
    ;;
  *)
    echo "Uso: $0 [ojoia|cineia]"
    exit 1
    ;;
esac

echo "=== Deploy completado ==="
