"""eva/camera_builder.py — OjoIA Eva v2

Construye, normaliza y guarda camera.json desde la configuración de vigilancia.

NUEVA ARQUITECTURA (testigo puro):
- attention_phrases: frases de atención observacionales (lo que el dueño quiere vigilar)
- owner_notes: notas del dueño para contexto (ej: "tocarse bolsillo por teléfono es normal")
- No hay "alert_behaviors" ni "ignore_behaviors" — el sistema no juzga
"""
import json
import time
import logging
from pathlib import Path
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)


VIGILANCE_TEMPLATES = {
    "caja_restaurante": {
        "profile_key": "caja_restaurante",
        "default_attention_phrases": [
            "empleado se lleva la mano al bolsillo después de cobrar",
            "dinero entra a la caja y cajón se cierre después de cobrar",
            "cajero empaca platos",
            "cliente paga antes de recibir pedido",
        ],
        "witness_focus": "caja registradora, transacciones, platos empacados, interacción cliente-cajero",
        "business_description": "restaurante con caja registradora y despacho de platos",
    },
    "colmado": {
        "profile_key": "colmado",
        "default_attention_phrases": [
            "cliente llega y pide producto",
            "producto sale en funda o directo a las manos",
            "se intercambia dinero",
            "cajón se abre y se cierra",
        ],
        "witness_focus": "mostrador, productos, fundas, intercambio de dinero",
        "business_description": "colmado con mostrador y venta de productos",
    },
    "finca": {
        "profile_key": "finca",
        "default_attention_phrases": [
            "persona no autorizada en la zona",
            "animal fuera del área cercada",
            "portón abierto",
            "cantidad de animales visibles",
        ],
        "witness_focus": "animales, portones, personas en zona exterior",
        "business_description": "finca con animales y zona exterior",
    },
    "entrada": {
        "profile_key": "entrada",
        "default_attention_phrases": [
            "persona entrando",
            "persona saliendo",
            "puerta abierta por tiempo prolongado",
        ],
        "witness_focus": "puerta, acceso, personas entrando y saliendo",
        "business_description": "entrada o acceso principal",
    },
    "almacen": {
        "profile_key": "almacen",
        "default_attention_phrases": [
            "persona en almacén",
            "productos siendo movidos",
            "productos saliendo del almacén",
        ],
        "witness_focus": "productos, estantes, personas en almacén",
        "business_description": "almacén o bodega con inventario",
    },
}


def _profile_key(zone: str, business_type: str) -> str:
    z = (zone or "").lower()
    b = (business_type or "").lower()
    if any(w in z for w in ["caja", "registr", "mostrador", "punto de venta"]):
        return "caja_restaurante"
    if any(w in z for w in ["colmado", "tienda", "bodega"]):
        return "colmado"
    if any(w in z for w in ["almac", "bodega", "deposito", "depósito"]):
        return "almacen"
    if any(w in z for w in ["finca", "corral", "patio", "campo", "granja"]) or any(w in b for w in ["finca", "agricultura", "granja", "campo"]):
        return "finca"
    if any(w in z for w in ["entrada", "puerta", "acceso"]):
        return "entrada"
    if any(w in b for w in ["restaurant", "restaurante", "bar", "comedor", "cafeteria", "cafetería"]):
        return "caja_restaurante"
    if any(w in b for w in ["colmado", "tienda"]):
        return "colmado"
    return "entrada"


def _template(zone: str, business_type: str) -> dict:
    return VIGILANCE_TEMPLATES.get(_profile_key(zone, business_type), VIGILANCE_TEMPLATES["entrada"])


def _clean_list(value) -> list:
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    if isinstance(value, str):
        return [v.strip() for v in value.replace("\n", ",").split(",") if v.strip()]
    return []


def _merge_dict(base: dict, incoming: Optional[dict]) -> dict:
    result = dict(base or {})
    for key, value in (incoming or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge_dict(result[key], value)
        else:
            result[key] = value
    return result


def build_witness_prompt(config: Dict[str, Any]) -> str:
    """Construye el prompt de testigo puro para una cámara específica.

    Este prompt se usa en el sistema de chat de Eva, NO en el orquestador.
    El orquestador usa su propio prompt en _call_qwen_vision().
    """
    zone = config.get("zone") or "zona principal"
    business_name = config.get("business_name") or "el negocio"
    business_type = config.get("business_type") or "negocio"
    schedule = config.get("schedule") or {"open": "08:00", "close": "22:00"}
    attention_phrases = _clean_list(config.get("attention_phrases", []))
    owner_notes = _clean_list(config.get("owner_notes", []))

    tmpl = _template(zone, business_type)

    attention_text = ""
    if attention_phrases:
        attention_text = "\n".join(f"- {p}" for p in attention_phrases)

    notes_text = ""
    if owner_notes:
        notes_text = "\n".join(f"- {n}" for n in owner_notes)

    return (
        f"Eres un testigo observando \"{zone}\" en {business_name} ({tmpl['business_description']}).\n"
        f"Horario: {schedule.get('open', '08:00')} a {schedule.get('close', '22:00')}.\n\n"
        f"Observa: {tmpl['witness_focus']}.\n\n"
        f"Describe solo hechos visibles. Nunca juzgues si está bien o mal.\n"
    )


def normalize_camera_vigilance_config(config: Dict[str, Any]) -> Dict[str, Any]:
    """Normaliza camera.json con la nueva arquitectura de testigo puro."""
    config = dict(config or {})
    zone = config.get("zone") or "zona principal"
    business_type = config.get("business_type") or "negocio"
    schedule = config.get("schedule") or {"open": "08:00", "close": "22:00"}
    vigilance = config.get("vigilance") if isinstance(config.get("vigilance"), dict) else {}
    context = config.get("conversation_context") if isinstance(config.get("conversation_context"), dict) else {}

    tmpl = _template(zone, business_type)

    normal_mode = vigilance.get("normal_mode") or vigilance.get("normal") or {}
    sentinel_mode = vigilance.get("sentinel_mode") or vigilance.get("sentinel") or {}

    default_attention = tmpl["default_attention_phrases"]
    attention_phrases = _clean_list(
        vigilance.get("attention_phrases")
        or normal_mode.get("attention_phrases")
        or config.get("attention_phrases")
        or default_attention
    )
    owner_notes = _clean_list(
        vigilance.get("owner_notes")
        or normal_mode.get("owner_notes")
        or config.get("owner_notes")
    )

    normalized = {
        "enabled": bool(vigilance.get("enabled", True)),
        "profile": vigilance.get("profile") or tmpl["profile_key"],
        "zone_description": tmpl["business_description"],
        "concern": vigilance.get("concern") or context.get("concern") or "seguridad general",
        "normal_state": vigilance.get("normal_state") or context.get("normal_state") or tmpl["witness_focus"],
        "authorized_people": vigilance.get("authorized_people") or context.get("authorized_people") or "empleados autorizados",
        "important_objects": vigilance.get("important_objects") or context.get("important_objects") or "objetos visibles",
        "attention_phrases": attention_phrases,
        "owner_notes": owner_notes,
        "normal_mode": _merge_dict({
            "enabled": True,
            "grid_size": config.get("grid_size", 12),
            "cooldown_min": config.get("cooldown_min", 5),
            "yolo_triggers": config.get("yolo_triggers") or ["person"],
            "attention_phrases": attention_phrases,
            "owner_notes": owner_notes,
        }, normal_mode),
        "sentinel_mode": _merge_dict({
            "enabled": False,
            "cooldown_min": config.get("cooldown_min", 5),
            "yolo_triggers": ["person"],
        }, sentinel_mode),
        "zones": config.get("zones", []),
        "grace_minutes": int(vigilance.get("grace_minutes", 15)),
    }
    config["vigilance"] = normalized
    config["attention_phrases"] = attention_phrases
    config["owner_notes"] = owner_notes
    return config


def build_camera_config(session: Dict[str, Any]) -> Dict[str, Any]:
    """Construye camera.json desde datos de sesión."""
    camera_id = session.get("camera_id") or f"cam_{int(time.time())}"
    zone = session.get("zone", "zona")
    biz_type = session.get("business_type", "negocio")
    biz_name = session.get("business_name", "negocio")
    schedule = session.get("schedule", {"open": "08:00", "close": "22:00"})
    concern = session.get("concern", "")

    attention_phrases = session.get("attention_phrases", [])
    owner_notes = session.get("owner_notes", [])

    yolo_triggers = ["person"]
    if any(w in biz_type.lower() for w in ["finca", "granja", "agricultura", "campo"]):
        yolo_triggers = ["person", "cow", "bird", "dog", "horse"]

    if any(w in biz_type.lower() for w in ["finca", "granja", "agricultura"]):
        grid_size = 8
        cooldown_min = 10
    else:
        grid_size = 12
        cooldown_min = 5

    raw = {
        "camera_id": camera_id,
        "name": f"Cámara {zone}",
        "zone": zone,
        "business_type": biz_type,
        "business_name": biz_name,
        "conversation_context": concern[:120] if concern else f"Cámara en {zone}",
        "attention_phrases": attention_phrases,
        "owner_notes": owner_notes,
        "yolo_triggers": yolo_triggers,
        "schedule": {
            "open": schedule.get("open", "08:00"),
            "close": schedule.get("close", "22:00"),
        },
        "grid_size": grid_size,
        "frame_interval_s": 3,
        "cooldown_min": cooldown_min,
        "active": True,
        "configured_at": int(time.time()),
    }
    return normalize_camera_vigilance_config(raw)


def save_camera_config(user_id: str, config: Dict[str, Any], storage_root: Path) -> bool:
    """Guarda camera.json y actualiza user.json."""
    try:
        camera_id = config["camera_id"]
        config = normalize_camera_vigilance_config(config)
        cam_dir = storage_root / "users" / user_id / "cameras" / camera_id
        cam_dir.mkdir(parents=True, exist_ok=True)
        for sub in ["sessions", "frames", "events"]:
            (cam_dir / sub).mkdir(exist_ok=True)

        (cam_dir / "camera.json").write_text(
            json.dumps(config, indent=2, ensure_ascii=False)
        )

        user_path = storage_root / "users" / user_id / "user.json"
        ud = {}
        if user_path.exists():
            ud = json.loads(user_path.read_text())

        cameras = ud.get("cameras", [])
        if isinstance(cameras, dict):
            cameras[camera_id] = {
                "name": config.get("name", camera_id),
                "zone": config.get("zone", ""),
                "active": True,
            }
        else:
            cam_entry = {
                "camera_id": camera_id,
                "name": config.get("name", camera_id),
                "zone": config.get("zone", ""),
                "active": True,
            }
            existing_ids = [c.get("camera_id") for c in cameras]
            if camera_id in existing_ids:
                cameras = [{**c, **cam_entry} if c.get("camera_id") == camera_id else c for c in cameras]
            else:
                cameras.append(cam_entry)
        ud["cameras"] = cameras
        ud["vigilance_context"] = config.get("vigilance", {})

        user_path.write_text(json.dumps(ud, indent=2, ensure_ascii=False))

        logger.info(f"camera.json guardado: {camera_id} en {cam_dir}")
        return True

    except Exception as e:
        logger.error(f"Error guardando camera config: {e}")
        return False


# Alias para compatibilidad con api_eva.py viejo
build_vigilance_prompt = build_witness_prompt
