#!/bin/bash
# ST-1: Montar el HDD 1TB como disco de storage de OjoIA
# Ejecutar con:  sudo bash /opt/ojoia/code/scripts/mount_hdd.sh
set -e

LV=/dev/mapper/ubuntu--vg-models     # 529GB libres para storage
MNT=/mnt/ojoia-hdd

# 1) activar el LV si está inactivo
sudo lvchange -ay "$LV" || true

# 2) punto de montaje + primer mount (solo primera vez)
sudo mkdir -p "$MNT"
if ! mountpoint -q "$MNT"; then
  if ! sudo mount "$LV" "$MNT" 2>/dev/null; then
    # LV nuevo sin filesystem → formatear (SOLO si nunca tuvo datos)
    echo "Formateando $LV (primera vez)..."
    sudo mkfs.ext4 -L OJOIA-HDD "$LV"
    sudo mount "$LV" "$MNT"
  fi
fi

# 3) permanente en fstab (idempotente)
if ! grep -q "ubuntu--vg-models" /etc/fstab; then
  echo "/dev/mapper/ubuntu--vg-models $MNT ext4 defaults,nofail 0 2" | sudo tee -a /etc/fstab
fi

# 4) estructura para OjoIA + permisos de sam
sudo mkdir -p "$MNT/ojoia-storage/users"
sudo chown -R sam:sam "$MNT/ojoia-storage"

df -h "$MNT"
echo "✅ HDD montado en $MNT — listo para ST-2 (config desde el panel)"
