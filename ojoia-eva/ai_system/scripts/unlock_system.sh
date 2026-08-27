#!/bin/bash
# unlock_system.sh — Desblindaje de emergencia
# Ejecutar: sudo bash /home/sam/ai_system/scripts/unlock_system.sh

echo "=== DESBLINDAJE DE EMERGENCIA ==="
echo ""

echo "[1/4] Devolviendo servicios systemd a sam..."
for svc in /etc/systemd/system/{tunnel,api-eva,yolo,qwen,whisper,sdxl,comfyui,voxtral,audioldm2,ui-server,project-server,ai-arranque}.service; do
    chown sam:sam "$svc" 2>/dev/null
    chmod 644 "$svc"
done

echo "[2/4] Devolviendo scripts a sam..."
for f in /home/sam/ai_system/scripts/{boot_system.sh,gpu2_manager.sh,gpu2_offload.sh,switch_sdxl.sh,arranque.sh}; do
    chown sam:sam "$f" 2>/dev/null
    chmod 755 "$f"
done

echo "[3/4] Devolviendo servidores a sam..."
for f in /home/sam/ai_system/{api_eva.py,orchestrator.py,gateway_resize.py,sdxl_server.py,whisper_server.py,yolo_server.py,audioldm2_server.py,ui_server.py,project_server.py}; do
    chown sam:sam "$f" 2>/dev/null
    chmod 644 "$f"
done
chown sam:sam /home/sam/ai_system/voxtral/src/serve.py 2>/dev/null
chmod 644 /home/sam/ai_system/voxtral/src/serve.py

echo "[4/4] Devolviendo permisos a modelos..."
chmod -R u+rw,go+r /home/sam/ai_system/models/ 2>/dev/null
chmod -R u+rw,go+r /home/sam/ai_system/ComfyUI/models/ 2>/dev/null
chmod -R u+rw,go+r /home/sam/ai_system/voxtral/models/ 2>/dev/null

echo ""
echo "✅ Sistema desblindado. sam puede editar todo."
echo "Para volver a blindar: sudo bash /home/sam/ai_system/scripts/lock_system.sh"
