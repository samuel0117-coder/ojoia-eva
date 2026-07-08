#!/usr/bin/env python3
"""
cleanup_frames.py — Limpieza automática de frames antiguos.

Ejecutar diariamente a las 3 AM vía cron:
0 3 * * * /home/sam/ai_system/venv/bin/python /opt/ojoia/code/cleanup_frames.py

Política de retención:
- Plan activo/trial: frames > 24 horas
- Plan gratuito/inactivo: frames > 45 minutos
"""

import os
import sys
import time
import json
from pathlib import Path
from datetime import datetime

STORAGE_ROOT = Path("/home/sam/storage")
LOG_FILE = STORAGE_ROOT / "cleanup_frames.log"

def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


def get_user_plan(user_id: str) -> str:
    """Obtiene el plan del usuario: 'active', 'trial', 'free', 'expired'."""
    user_file = STORAGE_ROOT / "users" / user_id / "user.json"
    if not user_file.exists():
        return "free"
    
    try:
        with open(user_file) as f:
            user_data = json.load(f)
        
        plan = user_data.get("plan", "free")
        status = user_data.get("status", "active")
        plan_end = user_data.get("plan_end", 0) or 0
        
        if status == "trial":
            return "trial"
        elif status == "active" and plan_end and time.time() < plan_end:
            return "active"
        else:
            return "expired"
    except Exception as e:
        log(f"Error leyendo plan de {user_id}: {e}")
        return "free"


def cleanup_old_frames():
    """Limpia frames antiguos según política de retención."""
    log("=" * 60)
    log("Iniciando limpieza de frames antiguos")
    
    total_deleted = 0
    total_size_mb = 0
    
    # Recorrer todos los usuarios
    users_dir = STORAGE_ROOT / "users"
    if not users_dir.exists():
        log("No hay directorio de usuarios")
        return
    
    for user_dir in users_dir.iterdir():
        if not user_dir.is_dir():
            continue
        
        user_id = user_dir.name
        user_plan = get_user_plan(user_id)
        
        # Determinar antigüedad máxima según plan
        if user_plan in ("active", "trial"):
            max_age_hours = 24
        else:
            max_age_hours = 0.75  # 45 minutos
        
        max_age_seconds = max_age_hours * 3600
        cutoff_time = time.time() - max_age_seconds
        
        log(f"Usuario {user_id} ({user_plan}): max_age={max_age_hours}h")
        
        # Recorrer todas las cámaras del usuario
        cameras_dir = user_dir / "cameras"
        if not cameras_dir.exists():
            continue
        
        for cam_dir in cameras_dir.iterdir():
            if not cam_dir.is_dir():
                continue
            
            camera_id = cam_dir.name
            frames_dir = cam_dir / "frames"
            
            if not frames_dir.exists():
                continue
            
            # Contar y borrar frames antiguos
            deleted_count = 0
            deleted_size = 0
            
            for frame_file in frames_dir.glob("*.jpg"):
                try:
                    mtime = frame_file.stat().st_mtime
                    if mtime < cutoff_time:
                        file_size = frame_file.stat().st_size
                        frame_file.unlink()
                        deleted_count += 1
                        deleted_size += file_size
                except Exception as e:
                    log(f"Error procesando {frame_file}: {e}")
            
            if deleted_count > 0:
                log(f"  Cámara {camera_id}: {deleted_count} frames borrados ({deleted_size/1024/1024:.2f} MB)")
                total_deleted += deleted_count
                total_size_mb += deleted_size / 1024 / 1024
    
    log(f"Limpieza completada: {total_deleted} frames borrados ({total_size_mb:.2f} MB liberados)")
    log("=" * 60 + "\n")


if __name__ == "__main__":
    cleanup_old_frames()