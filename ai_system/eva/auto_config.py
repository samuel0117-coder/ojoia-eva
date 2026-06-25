import json
import time
from pathlib import Path


def _load_profile(user_id: str, storage_root: Path) -> dict:
    path = storage_root / "users" / user_id / "user.json"
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            return {}
    return {}


def _default_rules(zone: str, business_type: str) -> list:
    zone_l = zone.lower()
    if any(w in zone_l for w in ["caja", "mostrador", "registr", "punto de venta"]):
        return [
            "Alerta si alguien mete la mano en bolsillos o bolsas cerca de la caja",
            "Alerta si una persona entra detrás del mostrador fuera del personal",
            "Alerta si hay movimiento en la caja fuera del horario normal"
        ]
    if any(w in zone_l for w in ["almac", "bodega", "depósito", "deposito"]):
        return [
            "Alerta si entra alguna persona al almacén fuera de horario",
            "Alerta si veo productos siendo movidos sin actividad normal",
            "Alerta si hay presencia prolongada en el almacén"
        ]
    if any(w in zone_l for w in ["corral", "patio", "finca", "campo"]):
        return [
            "Alerta si entra una persona al corral fuera de horario",
            "Alerta si veo animales fuera del área esperada",
            "Alerta si hay actividad inusual durante la noche"
        ]
    return [
        "Alerta si entra una persona a esta zona fuera de horario",
        "Alerta si hay movimiento prolongado o actividad inusual",
        "Alerta si alguien manipula objetos importantes en la zona"
    ]


def _scanner_question(zone: str) -> str:
    z = zone or "the main area"
    return f"Is there any unauthorized person, suspicious movement, or unusual activity in {z}?"


def _system_prompt(zone: str, business_name: str, business_type: str, concern: str) -> str:
    return (
        f"Security camera covering {zone or 'the main area'} at {business_name or 'the business'} "
        f"({business_type or 'business'}). Main concern: {concern or 'general security'}. "
        "Analyze the scene visually and alert only on observable risks: people in restricted zones, "
        "suspicious movement, unusual activity, objects being moved, or presence outside business hours. "
        "Return concise Spanish observations focused on real security risk."
    )


async def auto_generate_config(user_id: str, image_b64: str, camera_id: str, storage_root: Path) -> dict:
    profile = _load_profile(user_id, storage_root)
    zone = profile.get("zone") or "zona principal"
    business_name = profile.get("business_name") or "negocio"
    business_type = profile.get("business_type") or "negocio"
    concern = (profile.get("main_concerns") or [""])[0] if isinstance(profile.get("main_concerns"), list) else profile.get("main_concerns", "")
    schedule = profile.get("schedule") or {"open": "07:00", "close": "19:00"}
    rules_es = _default_rules(zone, business_type)
    return {
        "camera_id": camera_id or f"cam_{int(time.time())}",
        "name": f"Cámara {zone}",
        "zone": zone,
        "business_name": business_name,
        "business_type": business_type,
        "conversation_context": concern,
        "eva_message": f"Te dejo una configuración inicial para {zone}. Revísala y guarda si está bien.",
        "rules": [{"es": r, "en": r} for r in rules_es],
        "rules_es": rules_es,
        "scanner_question": _scanner_question(zone),
        "system_prompt": _system_prompt(zone, business_name, business_type, concern),
        "schedule": schedule,
        "yolo_triggers": ["person"],
        "grid_size": 12,
        "cooldown_min": 5,
        "active": True,
    }
