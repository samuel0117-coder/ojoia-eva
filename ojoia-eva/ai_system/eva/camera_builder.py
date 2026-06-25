"""eva/camera_builder.py — OjoIA Eva v13
Construye y guarda camera.json desde la sesión de Eva.
Las reglas vienen de la conversación real, no de una base de datos predefinida.
"""
import json
import time
import logging
from pathlib import Path
from typing import Dict, Any

logger = logging.getLogger(__name__)


def build_camera_config(session: Dict[str, Any]) -> Dict[str, Any]:
    """
    Construir camera.json desde la sesión de Eva.
    
    Las reglas en inglés (scanner_question, rules_en) las genera Eva
    durante la conversación — son específicas a esta cámara.
    """
    camera_id  = session.get("camera_id") or f"cam_{int(time.time())}"
    zone       = session.get("zone", "zona")
    biz_type   = session.get("business_type", "negocio")
    biz_name   = session.get("business_name", "negocio")
    schedule   = session.get("schedule", {"open": "08:00", "close": "22:00"})
    concern    = session.get("concern", "")
    rules      = session.get("confirmed_rules", [])   # list of {"es":..., "en":...}
    image_desc = session.get("image_desc", "")

    # Extraer versiones en español e inglés
    rules_es = []
    rules_en = []
    for r in rules:
        if isinstance(r, dict):
            es = r.get("es", "")
            en = r.get("en", "")
            # Si no hay inglés, usar el español como fallback (el scanner traduce internamente)
            if not en and es:
                en = es  # El orquestador usa reglas_es directamente
            rules_es.append(es)
            rules_en.append(en if en else es)
        else:
            rules_es.append(str(r))
            rules_en.append(str(r))

    # Si hay menos de 1 regla en inglés, completar con genérica
    while len(rules_en) < 1:
        rules_en.append(f"Is there any suspicious activity in the {zone} area?")

    # Usar system_prompt y scanner_question de Qwen si están disponibles
    qwen_system_prompt = session.get("qwen_system_prompt", "")
    qwen_scanner_question = session.get("qwen_scanner_question", "")

    if qwen_system_prompt:
        system_prompt = qwen_system_prompt
    else:
        # Fallback: construir system_prompt manualmente
        rules_summary = "; ".join(rules_en[:3]) if rules_en else "monitor suspicious activity"
        system_prompt = (
            f"Security camera monitoring the {zone} area of a {biz_type} "
            f"in Dominican Republic. Business: {biz_name}. "
            f"Visual context: {image_desc[:100] if image_desc else 'indoor business area'}. "
            f"Business hours: {schedule.get('open','08:00')}-{schedule.get('close','22:00')}. "
            f"Main concern: {concern[:80] if concern else 'general security'}. "
            f"Alert rules: {rules_summary}. "
            f"Outside business hours: ANY person present = critical immediate alert."
        )

    if qwen_scanner_question:
        scanner_question = qwen_scanner_question
    else:
        scanner_question = rules_en[0] if rules_en else f"Is there any person in {zone} area?"

    # YOLO triggers — por ahora siempre person (V2 expandir)
    yolo_triggers = ["person"]
    # Para fincas u otras configuraciones el usuario puede tener animales
    if any(w in biz_type.lower() for w in ["finca","granja","agricultura","campo"]):
        yolo_triggers = ["person", "cow", "bird", "dog", "horse"]

    # Grid size según el negocio
    # Fincas y espacios abiertos → 8 frames (movimiento lento)
    # Negocios normales → 12 frames
    if any(w in biz_type.lower() for w in ["finca","granja","agricultura"]):
        grid_size = 8
        cooldown_min = 10
    else:
        grid_size = 12
        cooldown_min = 5

    return {
        "camera_id":          camera_id,
        "name":               f"Cámara {zone}",
        "zone":               zone,
        "business_type":      biz_type,
        "conversation_context": concern[:120] if concern else f"Cámara en {zone}",
        "scanner_question":   scanner_question,
        "rules":              rules_en[:3],
        "rules_es":           rules_es[:3],
        "system_prompt":      system_prompt,
        "yolo_triggers":      yolo_triggers,
        "schedule": {
            "open":  schedule.get("open", "08:00"),
            "close": schedule.get("close", "22:00"),
        },
        "night_mode":        True,   # siempre activo — fuera de horario = vigilante
        "grid_size":         grid_size,
        "frame_interval_s":  3,
        "cooldown_min":      cooldown_min,
        "active":            True,
        "configured_at":     int(time.time()),
    }


def save_camera_config(user_id: str, config: Dict[str, Any], storage_root: Path) -> bool:
    """
    Guardar camera.json en disco y actualizar user.json.
    Retorna True si guardó correctamente.
    """
    try:
        camera_id = config["camera_id"]

        # Carpetas de la cámara
        cam_dir = storage_root / "users" / user_id / "cameras" / camera_id
        cam_dir.mkdir(parents=True, exist_ok=True)
        (cam_dir / "sessions").mkdir(exist_ok=True)
        (cam_dir / "frames").mkdir(exist_ok=True)
        (cam_dir / "events").mkdir(exist_ok=True)

        # Guardar camera.json
        (cam_dir / "camera.json").write_text(
            json.dumps(config, indent=2, ensure_ascii=False)
        )

        # Actualizar user.json
        user_path = storage_root / "users" / user_id / "user.json"
        ud = {}
        if user_path.exists():
            ud = json.loads(user_path.read_text())

        cameras = ud.get("cameras", [])
        existing_ids = [c.get("camera_id") for c in cameras]

        cam_entry = {
            "camera_id": camera_id,
            "name":      config.get("name", camera_id),
            "zone":      config.get("zone", ""),
            "rules":     config.get("rules", []),
            "rules_es":  config.get("rules_es", []),
            "active":    True,
        }

        merge_updates = {k: v for k, v in cam_entry.items() if v not in (None, "", [])}

        if camera_id in existing_ids:
            cameras = [
                {**c, **merge_updates} if c.get("camera_id") == camera_id else c
                for c in cameras
            ]
        else:
            cameras.append(cam_entry)

        ud["cameras"] = cameras
        _sp = config.get("system_prompt", "")
        _ru = config.get("rules", [])
        if _sp:
            ud["vigilance_prompt"] = _sp
        if _ru:
            ud["vigilance_rules"] = _ru
        if config.get("scanner_question"):
            ud["scanner_question"] = config.get("scanner_question", "")
        if config.get("yolo_triggers"):
            ud["yolo_triggers"] = config.get("yolo_triggers", ["person"])

        user_path.write_text(json.dumps(ud, indent=2, ensure_ascii=False))

        logger.info(f"✅ camera.json guardado: {camera_id} en {cam_dir}")
        return True

    except Exception as e:
        logger.error(f"❌ Error guardando camera config: {e}")
        return False
