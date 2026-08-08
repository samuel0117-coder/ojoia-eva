#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════
# B6 — Aplicar parches systemd (Restart=always + StartLimitBurst=5)
# Bloque B (estabilidad) del plan de modernizacion. Correr con sudo.
#
# Uso (desde una terminal interactiva del usuario sam):
#     sudo bash /opt/ojoia/docs/systemd/apply_systemd_b6.sh
# ═══════════════════════════════════════════════════════════════════════════
set -e

SRC_DIR="/opt/ojoia/docs/systemd"

# Unidades system-level a parchear (las 4 que hoy tienen Restart=on-failure)
UNITS_SYSTEM=(
  api-eva.service
  yolo-server.service
  whisper.service
  qwen.service
)

echo "=== B6: parcheando systemd con Restart=always + StartLimitBurst=5 ==="
echo

# 1) respaldar las unidades actuales
BACKUP_DIR="/etc/systemd/system/backup-b6-$(date +%Y%m%d_%H%M%S)"
mkdir -p "$BACKUP_DIR"
for u in "${UNITS_SYSTEM[@]}"; do
  if [ -f "/etc/systemd/system/$u" ]; then
    cp "/etc/systemd/system/$u" "$BACKUP_DIR/$u"
    echo "[backup] $u -> $BACKUP_DIR/$u"
  fi
done
echo

# 2) copiar las nuevas versiones
for u in "${UNITS_SYSTEM[@]}"; do
  cp "$SRC_DIR/$u" "/etc/systemd/system/$u"
  echo "[patch] /etc/systemd/system/$u"
done
echo

# 3) recargar systemd
systemctl daemon-reload
echo "[ok] daemon-reload"
echo

# 4) reiniciar cada servicio para aplicar la nueva conf
for u in "${UNITS_SYSTEM[@]}"; do
  base="${u%.service}"
  echo -n "[restart] $base: "
  if systemctl restart "$base"; then
    sleep 2
    echo "active=$(systemctl is-active "$base")  Restart=$(systemctl show "$base" --property=Restart --value)  Burst=$(systemctl show "$base" --property=StartLimitBurst --value)"
  else
    echo "FAILED (revisar journalctl -u $base)"
  fi
done

echo
echo "=== B6 completo ==="
echo "Backup en: $BACKUP_DIR"
echo
echo "Verificar:"
echo "  systemctl show api-eva yolo-server whisper qwen --property=Restart,StartLimitBurst,StartLimitIntervalSec"
echo "  journalctl -u yolo-server -n 20 --since '2 min ago'"
