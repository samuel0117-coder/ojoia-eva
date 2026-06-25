"""eva/camera_builder.py — OjoIA Eva v2

Construye, normaliza y guarda camera.json desde la configuración de vigilancia.
"""
import json
import time
import logging
from pathlib import Path
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)


VIGILANCE_PROFILES = {
    "caja_restaurante": {
        "normal_state": "cajero atendiendo, clientes comprando, productos y caja registradora en su lugar",
        "authorized_people": "cajero, administrador y empleados autorizados",
        "important_objects": "dinero, caja registradora, facturas, productos, puerta",
        "alert_behaviors": [
            "persona no autorizada detrás de caja",
            "mano entrando por encima de la caja",
            "caja abierta sin cajero cerca",
            "dinero o efectivo siendo manipulado por persona no autorizada",
            "cajero o cliente metiendo dinero o productos en bolsillo o funda de forma sospechosa",
            "humo, fuego o incendio",
            "cámara tapada, movida u obstruida"
        ],
        "ignore_behaviors": [
            "misma escena repetida",
            "falta de variación visual",
            "falta de movimiento",
            "ausencia de personas",
            "cliente esperando",
            "cajero trabajando normalmente",
            "persona pasando cerca de caja",
            "escena tranquila",
            "posible mala cobertura"
        ]
    },
    "entrada": {
        "normal_state": "clientes entrando y saliendo durante horario normal",
        "authorized_people": "clientes y empleados autorizados",
        "important_objects": "puerta, acceso, personas",
        "alert_behaviors": [
            "persona entrando fuera de horario",
            "persona forcejeando en la puerta",
            "puerta abierta por tiempo prolongado",
            "cámara tapada, movida u obstruida"
        ],
        "ignore_behaviors": [
            "clientes entrando normalmente",
            "puerta abriéndose brevemente",
            "ausencia de personas",
            "escena repetida"
        ]
    },
    "almacen": {
        "normal_state": "productos ordenados y empleados autorizados moviendo inventario",
        "authorized_people": "empleados y administrador",
        "important_objects": "productos, estantes, inventario, puerta",
        "alert_behaviors": [
            "persona no autorizada en almacén",
            "productos siendo sacados sin actividad normal",
            "presencia prolongada fuera de horario",
            "cámara tapada, movida u obstruida"
        ],
        "ignore_behaviors": [
            "productos quietos",
            "escena repetida",
            "ausencia de personas",
            "empleado autorizado organizando inventario"
        ]
    },
    "finca": {
        "normal_state": "animales, patio, corral o zona exterior sin presencia no autorizada",
        "authorized_people": "dueño, empleados y personas autorizadas",
        "important_objects": "animales, portones, galpón, herramientas",
        "alert_behaviors": [
            "persona no autorizada en la finca",
            "animal fuera del área esperada",
            "portón abierto fuera de horario",
            "cámara tapada, movida u obstruida"
        ],
        "ignore_behaviors": [
            "animales quietos",
            "escena repetida",
            "ausencia de personas",
            "actividad normal del campo"
        ]
    }
}


def _profile_key(zone: str, business_type: str) -> str:
    z = (zone or "").lower()
    b = (business_type or "").lower()
    if any(w in z for w in ["caja", "registr", "mostrador", "punto de venta"]):
        return "caja_restaurante"
    if any(w in z for w in ["almac", "bodega", "deposito", "depósito"]):
        return "almacen"
    if any(w in z for w in ["entrada", "puerta", "acceso"]):
        return "entrada"
    if any(w in z for w in ["finca", "corral", "patio", "campo", "granja"]) or any(w in b for w in ["finca", "agricultura", "granja", "campo"]):
        return "finca"
    if any(w in b for w in ["restaurant", "restaurante", "bar", "comedor", "cafeteria", "cafetería"]):
        return "caja_restaurante"
    return "entrada"


def _profile(zone: str, business_type: str) -> dict:
    return VIGILANCE_PROFILES.get(_profile_key(zone, business_type), VIGILANCE_PROFILES["entrada"])


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


def build_vigilance_prompt(config: Dict[str, Any], mode: str = "normal") -> str:
    zone = config.get("zone") or "zona principal"
    business_name = config.get("business_name") or "el negocio"
    business_type = config.get("business_type") or "negocio"
    schedule = config.get("schedule") or {"open": "08:00", "close": "22:00"}
    vigilance = config.get("vigilance") if isinstance(config.get("vigilance"), dict) else {}
    profile_cfg = _profile(zone, business_type)

    normal_mode = vigilance.get("normal_mode") or vigilance.get("normal") or {}
    sentinel_mode = vigilance.get("sentinel_mode") or vigilance.get("sentinel") or {}
    active_mode = sentinel_mode if mode == "sentinel" else normal_mode

    concern = vigilance.get("concern") or "seguridad general"
    forbidden = vigilance.get("forbidden_events") or "actividad sospechosa o no autorizada"
    normal_state = active_mode.get("normal_state") or vigilance.get("normal_state") or profile_cfg["normal_state"]
    authorized = active_mode.get("authorized_people") or vigilance.get("authorized_people") or profile_cfg["authorized_people"]
    important = active_mode.get("important_objects") or vigilance.get("important_objects") or profile_cfg["important_objects"]
    alert_behaviors = active_mode.get("alert_behaviors") or vigilance.get("alert_behaviors") or profile_cfg["alert_behaviors"]
    ignore_behaviors = active_mode.get("ignore_behaviors") or vigilance.get("ignore_behaviors") or profile_cfg["ignore_behaviors"]
    sensitivity = active_mode.get("sensitivity") or "alta"

    alert_text = "\n".join(f"- {item}" for item in _clean_list(alert_behaviors)) or "- actividad sospechosa o no autorizada"
    ignore_text = "\n".join(f"- {item}" for item in _clean_list(ignore_behaviors)) or "- ninguna"

    if mode == "sentinel":
        return (
            f"Eres Eva, vigilante de seguridad de {business_name} ({business_type}) en República Dominicana.\n"
            f"Cámara: {zone}. Modo CENTINELA activo. Horario normal: {schedule.get('open', '08:00')} a {schedule.get('close', '22:00')}.\n\n"
            f"Estás fuera del horario de operación. En modo centinela, cualquier persona visible en la zona es una alerta crítica.\n"
            f"Objetos importantes: {important}. Personas autorizadas: {authorized}.\n"
            f"Nunca debe pasar: {forbidden}.\n\n"
            "No analices frame por frame. Responde SOLO JSON válido con violation, mode, summary, details, anomalias, importance, evidence.\n"
            "Incluso si no hay presencia ni riesgo visible, llena details con lo que sí puedes ver o con 'no hay presencia visible' para alimentar el diario.\n"
            "Si detectas persona, presencia, movimiento sospechoso, humo, fuego o cámara obstruida, genera alerta inmediata."
        )

    return (
        f"Eres Eva, vigilante de seguridad de {business_name} ({business_type}) en República Dominicana.\n"
        f"Cámara: {zone}. Modo NORMAL activo. Horario normal: {schedule.get('open', '08:00')} a {schedule.get('close', '22:00')}.\n"
        f"Preocupación principal: {concern}. Sensibilidad: {sensitivity}.\n"
        f"Estado normal esperado: {normal_state}.\n"
        f"Personas autorizadas: {authorized}.\n"
        f"Objetos importantes: {important}.\n"
        f"Nunca debe pasar: {forbidden}.\n\n"
        "Durante modo normal, NO alertes por:\n"
        f"{ignore_text}\n\n"
        "Alerta solo si ves una acción concreta y observable:\n"
        f"{alert_text}\n\n"
        "INSTRUCCIONES CRÍTICAS PARA EL DIARIO DE EVENTOS:\n"
        "- Este análisis se guarda en un diario que Eva lee después para responder preguntas del dueño.\n"
        "- SIEMPRE describe personas visibles: ubicación (izquierda/centro/derecha/fondo), ropa (color, tipo: camisa, polocher, pantalón, gorra, vestido), acción (atendiendo, esperando, caminando, empaquetando, manipulando dinero, haciendo factura, cargando productos).\n"
        "- NUNCA digas 'sin personas' o 'actividad normal' sin detallar qué ves. Si hay una persona, describe aunque sea 'persona de pie en el centro'.\n"
        "- NUNCA uses frases genéricas como 'escena tranquila', 'escena repetitiva', 'sin actividad sospechosa' como summary. El summary debe decir QUÉ ves.\n"
        "- SIEMPRO incluye en objects_visible los objetos concretos: caja registradora, productos, platos, fundas, dinero, computadora, teléfono, puerta, mostrador.\n"
        "- SIEMPRE incluye en actions_visible las acciones concretas: atendiendo cliente, empaquetando producto, pasando dinero, manipulando caja, esperando, caminando.\n"
        "- Usa clothing_visible con colores y tipos específicos: 'polocher azul', 'camisa blanca', 'gorra roja', 'pantalón negro'.\n"
        f"- Ejemplo de summary bueno: 'Cajero con polocher azul atiende cliente que entrega billete de 2000 pesos. Dos platos empacados en funda sobre el mostrador.'\n"
        f"- Ejemplo de summary malo: 'Actividad normal en caja. No se observa nada sospechoso.'\n"
        "Responde SOLO JSON válido con: violation, mode, summary, details, anomalias, importance, evidence."
    )


def normalize_camera_vigilance_config(config: Dict[str, Any]) -> Dict[str, Any]:
    config = dict(config or {})
    zone = config.get("zone") or "zona principal"
    business_type = config.get("business_type") or "negocio"
    schedule = config.get("schedule") or {"open": "08:00", "close": "22:00"}
    profile_cfg = _profile(zone, business_type)
    vigilance = config.get("vigilance") if isinstance(config.get("vigilance"), dict) else {}
    context = config.get("conversation_context") if isinstance(config.get("conversation_context"), dict) else {}

    normal_mode = vigilance.get("normal_mode") or vigilance.get("normal") or {}
    sentinel_mode = vigilance.get("sentinel_mode") or vigilance.get("sentinel") or {}

    normalized = {
        "enabled": bool(vigilance.get("enabled", True)),
        "profile": vigilance.get("profile") or _profile_key(zone, business_type),
        "concern": vigilance.get("concern") or context.get("concern") or "seguridad general",
        "forbidden_events": vigilance.get("forbidden_events") or context.get("forbidden_events") or "actividad sospechosa o no autorizada",
        "normal_state": vigilance.get("normal_state") or context.get("normal_state") or profile_cfg["normal_state"],
        "authorized_people": vigilance.get("authorized_people") or context.get("authorized_people") or profile_cfg["authorized_people"],
        "important_objects": vigilance.get("important_objects") or context.get("important_objects") or profile_cfg["important_objects"],
        "alert_behaviors": _clean_list(vigilance.get("alert_behaviors") or normal_mode.get("alert_behaviors") or profile_cfg["alert_behaviors"]),
        "ignore_behaviors": _clean_list(vigilance.get("ignore_behaviors") or normal_mode.get("ignore_behaviors") or profile_cfg["ignore_behaviors"]),
        "normal_mode": _merge_dict({
            "enabled": True,
            "sensitivity": "alta",
            "grid_size": config.get("grid_size", 12),
            "cooldown_min": config.get("cooldown_min", 5),
            "yolo_triggers": config.get("yolo_triggers") or ["person"],
            "min_importance_to_alert": "alta",
            "normal_state": profile_cfg["normal_state"],
            "authorized_people": profile_cfg["authorized_people"],
            "important_objects": profile_cfg["important_objects"],
            "alert_behaviors": profile_cfg["alert_behaviors"],
            "ignore_behaviors": profile_cfg["ignore_behaviors"]
        }, normal_mode),
        "sentinel_mode": _merge_dict({
            "enabled": True,
            "sensitivity": "critica",
            "direct_alert_on_person": True,
            "cooldown_min": config.get("cooldown_min", 5),
            "yolo_triggers": ["person"],
            "ignore_small_objects": True
        }, sentinel_mode),
        "grace_minutes": int(vigilance.get("grace_minutes", 15))
    }
    config["vigilance"] = normalized
    stale_markers = [
        "Si todo está normal, responde:", "summary='actividad normal en {zone}'",
        "details={{'persons': numero_visible}}", "escena tranquila",
        "Ninguna persona autorizada", "con ninguna actividad sospechosa",
        "sin actividad sospechosa", "ninguna persona", "personas adicionales",
        "sin personas", "escena repetitiva", "no se observa ninguna",
        "no hay actividad", "actividad normal en",
    ]
    system_prompt = str(config.get("system_prompt", "") or "")
    is_stale = (
        not system_prompt
        or "Persona desnuda" in system_prompt
        or any(marker in system_prompt for marker in stale_markers)
    )
    if is_stale:
        config["system_prompt"] = build_vigilance_prompt(config, "normal")
        try:
            import logging
            logging.info(f"Regenerado system_prompt para {config.get('camera_id', '?')} zona={config.get('zone', '?')}")
        except Exception:
            pass
    return config


def build_camera_config(session: Dict[str, Any]) -> Dict[str, Any]:
    camera_id  = session.get("camera_id") or f"cam_{int(time.time())}"
    zone       = session.get("zone", "zona")
    biz_type   = session.get("business_type", "negocio")
    biz_name   = session.get("business_name", "negocio")
    schedule   = session.get("schedule", {"open": "08:00", "close": "22:00"})
    concern    = session.get("concern", "")

    system_prompt = session.get("system_prompt", "")
    if not system_prompt:
        system_prompt = (
            f"Eres un vigilante de seguridad vigilando {zone} de {biz_name} ({biz_type}). "
            f"Horario: {schedule.get('open','08:00')}-{schedule.get('close','22:00')}. "
            f"Preocupación: {concern or 'seguridad general'}. "
            "Responde SOLO JSON con summary, details, anomalias e importancia."
        )

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
        "camera_id":           camera_id,
        "name":                f"Cámara {zone}",
        "zone":               zone,
        "business_type":      biz_type,
        "business_name":      biz_name,
        "conversation_context": concern[:120] if concern else f"Cámara en {zone}",
        "system_prompt":      system_prompt,
        "yolo_triggers":      yolo_triggers,
        "schedule": {
            "open":  schedule.get("open", "08:00"),
            "close": schedule.get("close", "22:00"),
        },
        "grid_size":          grid_size,
        "frame_interval_s":   3,
        "cooldown_min":       cooldown_min,
        "active":             True,
        "configured_at":      int(time.time()),
    }
    return normalize_camera_vigilance_config(raw)


def save_camera_config(user_id: str, config: Dict[str, Any], storage_root: Path) -> bool:
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
        ud["vigilance_prompt"] = config.get("system_prompt", "")
        ud["vigilance_context"] = config.get("vigilance", {})

        user_path.write_text(json.dumps(ud, indent=2, ensure_ascii=False))

        logger.info(f"camera.json guardado: {camera_id} en {cam_dir}")
        return True

    except Exception as e:
        logger.error(f"Error guardando camera config: {e}")
        return False
