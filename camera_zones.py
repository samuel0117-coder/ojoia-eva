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

# ── F2.2: cache corto de zonas por cámara (evita JSON parse por frame) ──
_zone_assign_cache: dict = {}  # camera_key → (zones, cached_at)
_ZONE_CACHE_TTL = 30.0         # seg


def get_camera_zones_cached(user_id: str, camera_id: str) -> List[Dict[str, Any]]:
    """F2.2: get_camera_zones con cache de 30s (lectura por frame del worker)."""
    key = f"{user_id}_{camera_id}"
    now = time.time()
    cached = _zone_assign_cache.get(key)
    if cached and (now - cached[1]) < _ZONE_CACHE_TTL:
        return cached[0]
    zones = get_camera_zones(user_id, camera_id)
    _zone_assign_cache[key] = (zones, now)
    if len(_zone_assign_cache) > 512:
        _zone_assign_cache.clear()
    return zones


def get_camera_zones(user_id: str, camera_id: str) -> List[Dict[str, Any]]:
    """Lee las zonas de una cámara desde camera.json."""
    if not user_id or not camera_id:
        return []
    try:
        cam_file = STORAGE_ROOT / "users" / user_id / "cameras" / camera_id / "camera.json"
        if not cam_file.exists():
            return []
        with open(cam_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        zones = data.get("zones", [])
        return zones if isinstance(zones, list) else []
    except Exception:
        return []


def save_camera_zones(user_id: str, camera_id: str, zones: List[Dict[str, Any]]) -> bool:
    """Guarda las zonas de una cámara en camera.json."""
    if not user_id or not camera_id:
        return False
    try:
        cam_file = STORAGE_ROOT / "users" / user_id / "cameras" / camera_id / "camera.json"
        if not cam_file.exists():
            return False
        with open(cam_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        data["zones"] = zones
        with open(cam_file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return True
    except Exception:
        return False


def add_or_update_zone(user_id: str, camera_id: str, zone: Dict[str, Any]) -> Dict[str, Any]:
    """Agrega o actualiza una zona. Devuelve la zona normalizada con ID."""
    zone.setdefault("id", str(uuid.uuid4())[:8])
    zone.setdefault("parent_zone_id", None)
    zone.setdefault("created_at", time.time())
    zones = get_camera_zones(user_id, camera_id)
    existing = next((z for z in zones if z.get("id") == zone.get("id")), None)
    if existing:
        existing.update(zone)
        existing["updated_at"] = time.time()
    else:
        zones.append(zone)
    return zone if save_camera_zones(user_id, camera_id, zones) else {}


def delete_zone(user_id: str, camera_id: str, zone_id: str) -> bool:
    """Elimina una zona por su ID."""
    zones = get_camera_zones(user_id, camera_id)
    if not zones:
        return False
    zones = [z for z in zones if z.get("id") != zone_id]
    return save_camera_zones(user_id, camera_id, zones)


def get_zone_types() -> List[Dict[str, str]]:
    """Obtiene todos los tipos de zona posibles."""
    return [
        {"id": "entrance", "name": "Entrada", "icon": "🚪"},
        {"id": "cashier", "name": "Caja / Cobro", "icon": "💰"},
        {"id": "register", "name": "Caja registradora", "icon": "🧾"},
        {"id": "kitchen", "name": "Cocina", "icon": "🍳"},
        {"id": "dining", "name": "Comedor", "icon": "🍽️"},
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


# ── Mapa de objetos por turnos: objetos fijos del negocio ──
# Objeto: {id, name, class, x, y, r, parent_zone, color, created_at}
# A diferencia de las zonas (rectángulos {x,y,w,h}), un objeto es un PUNTO
# con radio: (x,y) = centro en coords relativas 0-1, r = radio 0-1.
# Se guardan en camera.json bajo el key "objects".

def get_camera_objects(user_id: str, camera_id: str) -> List[Dict[str, Any]]:
    """Lee los objetos fijos mapeados de una cámara desde camera.json."""
    if not user_id or not camera_id:
        return []
    try:
        cam_file = STORAGE_ROOT / "users" / user_id / "cameras" / camera_id / "camera.json"
        if not cam_file.exists():
            return []
        with open(cam_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        objects = data.get("objects", [])
        return objects if isinstance(objects, list) else []
    except Exception:
        return []


def save_camera_objects(user_id: str, camera_id: str, objects: List[Dict[str, Any]]) -> bool:
    """Guarda los objetos fijos de una cámara en camera.json."""
    if not user_id or not camera_id:
        return False
    try:
        cam_file = STORAGE_ROOT / "users" / user_id / "cameras" / camera_id / "camera.json"
        if not cam_file.exists():
            return False
        with open(cam_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        data["objects"] = objects
        with open(cam_file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return True
    except Exception:
        return False


def add_or_update_object(user_id: str, camera_id: str, obj: Dict[str, Any]) -> Dict[str, Any]:
    """Agrega o actualiza un objeto fijo (upsert por nombre). Devuelve el objeto normalizado."""
    name = str(obj.get("name", "")).strip()
    if not name:
        return {}
    obj["name"] = name
    obj.setdefault("id", str(uuid.uuid4())[:8])
    obj.setdefault("class", "other")
    obj.setdefault("parent_zone", None)
    obj.setdefault("created_at", time.time())
    # Clamp coords 0-1: el punto es el CENTRO del objeto
    for k in ("x", "y"):
        try:
            obj[k] = max(0.0, min(1.0, float(obj.get(k, 0.5))))
        except (TypeError, ValueError):
            obj[k] = 0.5
    try:
        obj["r"] = max(0.01, min(0.5, float(obj.get("r", 0.05))))
    except (TypeError, ValueError):
        obj["r"] = 0.05
    objects = get_camera_objects(user_id, camera_id)
    existing = next((o for o in objects if str(o.get("name", "")).strip() == name), None)
    if existing:
        # Preservar identidad del existente (id, created_at) en el update
        obj.pop("id", None)
        obj.pop("created_at", None)
        existing.update(obj)
        existing["updated_at"] = time.time()
        obj = existing
    else:
        objects.append(obj)
    return obj if save_camera_objects(user_id, camera_id, objects) else {}


def delete_object(user_id: str, camera_id: str, object_name: str) -> bool:
    """Elimina un objeto fijo por su nombre."""
    objects = get_camera_objects(user_id, camera_id)
    if not objects:
        return False
    target = str(object_name).strip()
    remaining = [o for o in objects if str(o.get("name", "")).strip() != target]
    if len(remaining) == len(objects):
        return False
    return save_camera_objects(user_id, camera_id, remaining)
