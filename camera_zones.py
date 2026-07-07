"""
camera_zones.py — Sistema de zonas de interés (ROI) para cámaras.

Cada cámara can have multiple zones (áreas de interés).
Zona: {id, name, type, coords, description, color, created_at}
Coords son relativas 0-1 sobre la imagen: {x, y, w, h}
Se guardan en camera.json bajo el key "zones".
"""
import json
import time
import uuid
from pathlib import Path
from typing import List, Dict, Any

STORAGE_ROOT = Path("/home/sam/storage")


def get_camera_zones(user_id: str, camera_id: str) -> List[Dict[str, Any]]:
    """Lee las zonas de una cámara desde camera.json."""
    try:
        cam_file = STORAGE_ROOT / "users" / user_id / "cameras" / camera_id / "camera.json"
        if cam_file.exists():
            with open(cam_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            zones = data.get("zones", [])
            if isinstance(zones, list):
                return zones
    except Exception:
        pass
    return []


def save_camera_zones(user_id: str, camera_id: str, zones: List[Dict[str, Any]]) -> bool:
    """Guarda las zonas de una cámara en camera.json."""
    try:
        cam_file = STORAGE_ROOT / "users" / user_id / "cameras" / camera_id / "camera.json"
        if not cam_file.exists():
            return False
        with open(cam_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
        data["zones"] = zones
        with open(cam_file, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return True
    except Exception:
        return False


def add_or_update_zone(user_id: str, camera_id: str, zone: Dict[str, Any]) -> bool:
    """Agrega o actualiza una zona."""
    try:
        zones = get_camera_zones(user_id, camera_id)
        existing = next((z for z in zones if z.get("id") == zone.get("id")), None)
        if existing:
            existing.update(zone)
            existing["updated_at"] = time.time()
        else:
            zone.setdefault("id", str(uuid.uuid4())[:8])
            zone.setdefault("created_at", time.time())
            zones.append(zone)
        return save_camera_zones(user_id, camera_id, zones)
    except Exception:
        return False


def delete_zone(user_id: str, camera_id: str, zone_id: str) -> bool:
    """Elimina una zona."""
    try:
        zones = get_camera_zones(user_id, camera_id)
        zones = [z for z in zones if z.get("id") != zone_id]
        return save_camera_zones(user_id, camera_id, zones)
    except Exception:
        return False


def get_zone_types() -> List[Dict[str, str]]:
    """Obtiene todos los tipos de zona posibles."""
    return [
        {"id": "entrance", "name": "Entrada", "icon": "🚪"},
        {"id": "cashier", "name": "Caja / Cobro", "icon": "💰"},
        {"id": "register", "name": "Caja registradora", "icon": "🧾"},
        {"id": "kitchen", "name": "Cocina", "icon": "🍳"},
        {"id": "dining", "name": "Comedorined area", "icon": "🍽️"},
        {"id": "inventory", "name": "Inventario / Almacén", "icon": "📦"},
        {"id": "counter", "name": "Mostrador", "icon": "🛍️"},
        {"id": "hall", "name": "Sala / Hall", "icon": "🏠"},
        {"id": "parking", "name": "Parqueo", "icon": "🚗"},
        {"id": "restricted", "name": "Área restringida", "icon": "🚫"},
        {"id": "office", "name": "Oficina", "icon": "💼"},
        {"id": "storage", "name": "Bodega", "icon": "📦"},
        {"id": "hallway", "name": "Pasillo", "icon": "🚶"},
        {"id": "production", "name": "Área de producción", "icon": "🏭"},
        {"id": "other", "name": "Otra", "icon": "📍"},
    ]
