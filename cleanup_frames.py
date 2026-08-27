#!/usr/bin/env python3
"""
cleanup_frames.py — Limpieza automatica de frames antiguos + retencion de eventos.

Ejecutar diariamente a las 3 AM (frecuencia configurable desde admin) via cron:
  0 3 * * * /home/sam/ai_system/venv/bin/python /opt/ojoia/code/cleanup_frames.py

Politica de retencion (B2 — configurable desde /admin/retention):
- Plan activo/trial: frames > 24h, eventos (metadata .json + jpg) > 7 días
- Plan gratuito/inactivo: frames > 45 min, eventos > 1 día
- latest_vigilance.jpg siempre se conserva (se sobreescribe, no acumula)

Los defaults viven en api_eva.py DEFAULT_RETENTION; aqui los replicamos
como fallback y los sobreescribimos con admin_config.json["retention"] si existe.
"""

import os
import sys
import time
import json
from pathlib import Path
from datetime import datetime

STORAGE_ROOT = Path("/home/sam/storage")
ADMIN_CONFIG = STORAGE_ROOT / "admin_config.json"
LOG_FILE = STORAGE_ROOT / "cleanup_frames.log"

# Defaults codificados (mismo valores que api_eva.DEFAULT_RETENTION).
DEFAULT_RETENTION = {
    "days_by_plan": {
        "active": 7,
        "trial": 7,
        "free": 1,
        "expired": 1,
    },
    "frames_hours_by_plan": {
        "active": 24,
        "trial": 24,
        "free": 0.75,
        "expired": 0.75,
    },
}


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


def load_retention_config() -> dict:
    """Lee retention de admin_config.json; fallback a DEFAULT_RETENTION.

    Estructura esperada en admin_config.json:
      "retention": {
        "days_by_plan": {"active": 7, "trial": 7, "free": 1, "expired": 1},
        "frames_hours_by_plan": {"active": 24, ...},
        "cleanup_cron": "0 3 * * *",
        "updated_at": <ts>
      }
    """
    cfg_saved = {}
    if ADMIN_CONFIG.exists():
        try:
            with open(ADMIN_CONFIG) as f:
                admin_cfg = json.load(f)
            cfg_saved = admin_cfg.get("retention", {}) or {}
        except Exception as e:
            log(f"Error leyendo admin_config.json: {e}; usando defaults")
    # merge defensivo: defaults + overrides validos
    days = dict(DEFAULT_RETENTION["days_by_plan"])
    for plan, val in (cfg_saved.get("days_by_plan") or {}).items():
        if plan in days:
            try:
                iv = int(val)
                if iv >= 1:
                    days[plan] = iv
            except (TypeError, ValueError):
                pass
    frames_h = dict(DEFAULT_RETENTION["frames_hours_by_plan"])
    for plan, val in (cfg_saved.get("frames_hours_by_plan") or {}).items():
        if plan in frames_h:
            try:
                fv = float(val)
                if 0 < fv <= 24 * 7:
                    frames_h[plan] = fv
            except (TypeError, ValueError):
                pass
    return {"days_by_plan": days, "frames_hours_by_plan": frames_h}


def cleanup_old_frames(retention: dict):
    """Limpia frames antiguos según política de retención."""
    log("=" * 60)
    log("Iniciando limpieza de frames antiguos")
    
    total_deleted = 0
    total_size_mb = 0
    frames_by_plan = retention["frames_hours_by_plan"]
    
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
        max_age_hours = frames_by_plan.get(user_plan, frames_by_plan.get("free", 0.75))
        max_age_seconds = max_age_hours * 3600
        cutoff_time = time.time() - max_age_seconds
        
        log(f"Usuario {user_id} ({user_plan}): frames max_age={max_age_hours}h")
        
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


def cleanup_old_events(retention: dict):
    """Limpia frames viejos dentro de events/ (conserva los .json de alerta).

    [Fix] El script original solo limpiaba cameras/<id>/frames/, omitiendo
    cameras/<id>/events/<evt>/frames/*.jpg — que es donde se acumulan los
    frames de vigilancia (puede superar 10GB y llenar el disco). Se aplica
    la misma política de retención (24h activo/trial, 45min gratis/inactivo).
    Solo se borran los .jpg; los .json de metadata de alerta se conservan.
    (NOTA: la metadata .json se borra en cleanup_old_event_metadata).
    """
    log("=" * 60)
    log("Iniciando limpieza de frames antiguos en events/")
    total_deleted = 0
    total_size_mb = 0
    empty_events_removed = 0
    frames_by_plan = retention["frames_hours_by_plan"]

    users_dir = STORAGE_ROOT / "users"
    if not users_dir.exists():
        return

    for user_dir in users_dir.iterdir():
        if not user_dir.is_dir():
            continue
        user_id = user_dir.name
        user_plan = get_user_plan(user_id)
        max_age_seconds = frames_by_plan.get(user_plan, 0.75) * 3600
        cutoff_time = time.time() - max_age_seconds

        cameras_dir = user_dir / "cameras"
        if not cameras_dir.exists():
            continue

        for cam_dir in cameras_dir.iterdir():
            if not cam_dir.is_dir():
                continue
            events_dir = cam_dir / "events"
            if not events_dir.is_dir():
                continue

            for event_dir in events_dir.iterdir():
                if not event_dir.is_dir():
                    continue
                frames_dir = event_dir / "frames"
                if not frames_dir.is_dir():
                    continue
                del_count = 0
                del_size = 0
                for frame_file in frames_dir.glob("*.jpg"):
                    try:
                        if frame_file.stat().st_mtime < cutoff_time:
                            sz = frame_file.stat().st_size
                            frame_file.unlink()
                            del_count += 1
                            del_size += sz
                    except Exception as e:
                        log(f"Error en evento {frame_file}: {e}")
                if del_count > 0:
                    log(f"  Evento {event_dir.name[:40]}: {del_count} frames borrados ({del_size/1024/1024:.2f} MB)")
                    total_deleted += del_count
                    total_size_mb += del_size / 1024 / 1024
                # Limpiar dirs vacíos (frames y evento) para no acumular basura
                try:
                    if frames_dir.is_dir() and not any(frames_dir.iterdir()):
                        frames_dir.rmdir()
                    if event_dir.is_dir() and not any(event_dir.iterdir()):
                        event_dir.rmdir()
                        empty_events_removed += 1
                except Exception:
                    pass

    log(f"Limpieza events/ completada: {total_deleted} frames borrados ({total_size_mb:.2f} MB liberados)")
    if empty_events_removed:
        log(f"  Directorios de eventos vacíos eliminados: {empty_events_removed}")
    log("=" * 60 + "\n")


def cleanup_old_event_metadata(retention: dict):
    """B2 — Limpia metadata de eventos (evt_*.json y <event_id>.jpg en events/)
    con edad > retention_days según plan.

    Antes: cleanup conservaba evt_*.json y las miniaturas <event_id>.jpg
    para siempre (en una cámara se acumularon 68k eventos). Ahora los borra
    según `days_by_plan` (configurable desde /admin/retention).

    Conserva SIEMPRE:
      - latest_vigilance.jpg (se sobreescribe en cada alerta, no acumula)
      - subdirs de eventos (events/<evt>/) — esos frames los maneja
        cleanup_old_events, no este.
    """
    log("=" * 60)
    log("Iniciando limpieza de metadata de eventos (B2)")
    total_deleted = 0
    total_size_mb = 0
    days_by_plan = retention["days_by_plan"]

    users_dir = STORAGE_ROOT / "users"
    if not users_dir.exists():
        return

    for user_dir in users_dir.iterdir():
        if not user_dir.is_dir():
            continue
        user_id = user_dir.name
        user_plan = get_user_plan(user_id)
        retention_days = days_by_plan.get(user_plan, days_by_plan.get("free", 1))
        cutoff_time = time.time() - (retention_days * 86400)
        log(f"Usuario {user_id} ({user_plan}): eventos max_age={retention_days}d")

        cameras_dir = user_dir / "cameras"
        if not cameras_dir.exists():
            continue

        for cam_dir in cameras_dir.iterdir():
            if not cam_dir.is_dir():
                continue
            events_dir = cam_dir / "events"
            if not events_dir.is_dir():
                continue

            cam_del = 0
            cam_size = 0
            for entry in events_dir.iterdir():
                # saltar subdirs — esos los maneja cleanup_old_events
                if entry.is_dir():
                    continue
                # latest_vigilance.jpg se conserva SIEMPRE (no acumula)
                if entry.name == "latest_vigilance.jpg":
                    continue
                try:
                    if entry.stat().st_mtime < cutoff_time:
                        sz = entry.stat().st_size
                        entry.unlink()
                        cam_del += 1
                        cam_size += sz
                except Exception as e:
                    log(f"Error borrando evento {entry}: {e}")
            if cam_del > 0:
                log(f"  Cámara {cam_dir.name}: {cam_del} archivos de evento borrados "
                    f"({cam_size/1024/1024:.2f} MB)")
                total_deleted += cam_del
                total_size_mb += cam_size / 1024 / 1024

    log(f"Limpieza metadata B2 completada: {total_deleted} archivos borrados "
        f"({total_size_mb:.2f} MB liberados)")
    log("=" * 60 + "\n")


if __name__ == "__main__":
    retention = load_retention_config()
    log(f"Config retention cargada: days_by_plan={retention['days_by_plan']} "
        f"frames_hours_by_plan={retention['frames_hours_by_plan']}")
    cleanup_old_frames(retention)
    cleanup_old_events(retention)
    cleanup_old_event_metadata(retention)