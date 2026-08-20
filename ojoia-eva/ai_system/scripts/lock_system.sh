#!/bin/bash
# lock_system.sh — Candado de seguridad del sistema AI
# Ejecutar: sudo bash /home/sam/ai_system/scripts/lock_system.sh

echo "=== BLINDAJE DEL SISTEMA AI ==="
echo ""

# ── Candado 1: Servicios systemd (root only) ──
echo "[1/4] Protegiendo servicios systemd..."
for svc in /etc/systemd/system/{tunnel,api-eva,yolo,qwen,whisper,sdxl,comfyui,voxtral,audioldm2,ui-server,project-server,ai-arranque}.service; do
    chown root:root "$svc" 2>/dev/null
    chmod 644 "$svc"
done
echo "  ✅ 12 servicios protegidos (root:root 644)"

# ── Candado 2: Scripts de arranque (root only, sam puede ejecutar pero no editar) ──
echo "[2/4] Protegiendo scripts de arranque..."
for f in /home/sam/ai_system/scripts/{boot_system.sh,gpu2_manager.sh,gpu2_offload.sh,switch_sdxl.sh,arranque.sh}; do
    chown root:root "$f" 2>/dev/null
    chmod 755 "$f"
done
echo "  ✅ 5 scripts protegidos (root:root 755)"

# ── Candado 3: Servidores Python (root only, sam puede ejecutar pero no editar) ──
echo "[3/4] Protegiendo servidores Python..."
for f in /home/sam/ai_system/{api_eva.py,orchestrator.py,gateway_resize.py,sdxl_server.py,whisper_server.py,yolo_server.py,audioldm2_server.py,ui_server.py,project_server.py}; do
    chown root:root "$f" 2>/dev/null
    chmod 644 "$f"
done
chown root:root /home/sam/ai_system/voxtral/src/serve.py 2>/dev/null
chmod 644 /home/sam/ai_system/voxtral/src/serve.py
echo "  ✅ 10 servidores protegidos (root:root 644)"

# ── Candado 4: Modelos (root only para escritura, sam puede leer/ejecutar) ──
echo "[4/4] Protegiendo modelos..."
# Los modelos son datos, no código — sam necesita leerlos pero no modificarlos
chmod -R a-w /home/sam/ai_system/models/ 2>/dev/null
chmod -R a-w /home/sam/ai_system/ComfyUI/models/ 2>/dev/null
chmod -R a-w /home/sam/ai_system/voxtral/models/ 2>/dev/null
# Devolver ownership a sam para que pueda leer
chown -R sam:sam /home/sam/ai_system/models/ 2>/dev/null
chown -R sam:sam /home/sam/ai_system/ComfyUI/models/ 2>/dev/null
chown -R sam:sam /home/sam/ai_system/voxtral/models/ 2>/dev/null
# Pero sin escritura
chmod -R u+r,go+r,ugo-w /home/sam/ai_system/models/ 2>/dev/null
chmod -R u+r,go+r,ugo-w /home/sam/ai_system/ComfyUI/models/ 2>/dev/null
chmod -R u+r,go+r,ugo-w /home/sam/ai_system/voxtral/models/ 2>/dev/null
echo "  ✅ Modelos protegidos (sam puede leer, no escribir)"

echo ""
echo "═══════════════════════════════════════════════════"
echo "  BLINDAJE COMPLETO"
echo "═══════════════════════════════════════════════════"
echo ""
echo "Qué está protegido:"
echo "  • Servicios systemd → solo root puede modificar"
echo "  • Scripts de arranque → solo root puede editar"
echo "  • Servidores Python → solo root puede editar"
echo "  • Modelos → solo root puede escribir (sam puede leer)"
echo ""
echo "Qué puede hacer sam:"
echo "  • sudo systemctl start|stop|restart <servicio>  → REQUIERE SUDO"
echo "  • Leer logs, ver estado"
echo "  • Usar las APIs (generar imágenes, audio, etc.)"
echo ""
echo "Qué NO puede hacer sam (sin sudo):"
echo "  • Editar servicios systemd"
echo "  • Modificar scripts de arranque"
echo "  • Cambiar código de los servidores"
echo "  • Sobrescribir modelos"
echo ""
echo "Para desblindar (emergencia):"
echo "  sudo bash /home/sam/ai_system/scripts/unlock_system.sh"
echo "═══════════════════════════════════════════════════"
