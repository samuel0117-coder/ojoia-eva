#!/bin/bash
# 2026-09-01: desactiva el auto-reboot de unattended-upgrades.
# Los updates siguen descargándose/instalándose, pero el reboot se hace MANUALMENTE.
# Ejecutar con: sudo bash /opt/ojoia/code/scripts/disable_unattended_reboot.sh
set -e

CONF=/etc/apt/apt.conf.d/51no-automatic-reboot
sudo tee "$CONF" >/dev/null <<'INNER'
// OjoIA: deshabilitar el auto-reboot de unattended-upgrades
// (los updates siguen aplicándose, pero el reboot se hace manualmente).
// Esto evita reinicios sorpresa que tumban el servidor sin avisar.
Unattended-Upgrade::Automatic-Reboot "false";
Unattended-Upgrade::Automatic-Reboot-Time "now";
INNER

# Re-activar el servicio si estaba desactivado, y reiniciar el timer
sudo systemctl enable --now apt-daily-upgrade.timer apt-daily.timer

echo "✅ Auto-reboot desactivado. Timer re-activado."
echo "   Updates siguen aplicándose automáticamente (cambios de seguridad)."
echo "   Cuando quieras reiniciar (kernel nuevo, etc.): sudo reboot"
