#!/bin/bash
# /home/sam/disk_monitor.sh
# Monitor de espacio en disco - alerta y protege el sistema
# Se ejecuta cada 5 minutos via cron

LOG="/home/sam/disk_monitor.log"
ALERT_GB=20      # Alerta cuando queden menos de 20GB
CRITICAL_GB=10   # Crítico cuando queden menos de 10GB

# Obtener espacio libre en GB (partición raíz)
FREE_GB=$(df -BG / | awk 'NR==2 {print $4}' | tr -d 'G')
USED_GB=$(df -BG / | awk 'NR==2 {print $3}' | tr -d 'G')
TOTAL_GB=$(df -BG / | awk 'NR==2 {print $2}' | tr -d 'G')
PERCENT=$(df / | awk 'NR==2 {print $5}' | tr -d '%')

now() { date -Iseconds; }

# Log actual
echo "[$(now)] Disco: ${USED_GB}G/${TOTAL_GB}G usados (${PERCENT}%), libres: ${FREE_GB}G" >> "$LOG"

# ═══════════════════════════════════════════════════
# ALERTA: Menos de 20GB libres
# ═══════════════════════════════════════════════════
if [ "$FREE_GB" -lt "$ALERT_GB" ]; then
    echo "[$(now)] ⚠️ ALERTA: Solo ${FREE_GB}G libres (umbral: ${ALERT_GB}G)" >> "$LOG"
    
    # Limpiar logs viejos
    find /var/log -name "*.log" -mtime +30 -delete 2>/dev/null
    find /home/sam -name "*.log" -mtime +7 -delete 2>/dev/null
    
    # Limpiar cache de pip
    pip cache purge 2>/dev/null
    
    # Limpiar temporales
    rm -rf /tmp/* 2>/dev/null
    
    # Notificar (si hay FCM configurado)
    echo "[$(now)] Limpieza automática ejecutada" >> "$LOG"
fi

# ═══════════════════════════════════════════════════
# CRÍTICO: Menos de 10GB libres
# ═══════════════════════════════════════════════════
if [ "$FREE_GB" -lt "$CRITICAL_GB" ]; then
    echo "[$(now)] 🚨 CRÍTICO: Solo ${FREE_GB}G libres. Deteniendo servicios no críticos..." >> "$LOG"
    
    # Detener servicios no críticos para liberar espacio
    systemctl stop comfyui.service 2>/dev/null
    systemctl --user stop movie_server.service 2>/dev/null
    systemctl --user stop cineia_studio_server.service 2>/dev/null
    systemctl --user stop whisper.service 2>/dev/null
    systemctl stop gpu1_image_server.service 2>/dev/null
    
    # Limpiar todo lo posible
    rm -rf /tmp/* 2>/dev/null
    rm -rf /home/sam/.cache/pip 2>/dev/null
    find /var/log -name "*.gz" -delete 2>/dev/null
    find /var/log -name "*.old" -delete 2>/dev/null
    
    echo "[$(now)] Servicios no críticos detenidos. Espacio liberado." >> "$LOG"
fi
