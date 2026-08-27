import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def build_camera_config(
    user_id: str,
    session: dict,
    selected_rule_ids: list,
    storage_root: Path,
    rule_templates_module,
) -> dict:
    camera_id = session.get("camera_id") or f"cam_{user_id[:8]}_{int(__import__('time').time())}"
    zone = session.get("zone", "sin zona")
    owner = session.get("owner", "dueño")
    first = owner.split()[0] if owner else "dueño"
    biz = session.get("biz", "negocio")
    biz_type = session.get("biz_type", "negocio")
    schedule = session.get("schedule", {"open": "08:00", "close": "22:00"})
    rules_es = session.get("rules", [])

    rules_en = []
    for rid in selected_rule_ids:
        tpl = rule_templates_module.get_rule_template(rid)
        rules_en.append(tpl["question"])

    while len(rules_en) < 3:
        rules_en.append("Is there any person in this area?")

    system_prompt = (
        f"Security camera at a {biz_type} in {zone}, Dominican Republic. "
        f"Owner: {first}. Business: {biz}. "
        f"Hours: {schedule.get('open','08:00')}-{schedule.get('close','22:00')}. "
    )
    if rules_es:
        system_prompt += f"Main concerns: {'; '.join(rules_es[:3])}."

    config = {
        "camera_id": camera_id,
        "name": f"Cámara {zone}",
        "zone": zone,
        "conversation_context": f"Cámara de seguridad en {zone} de un {biz_type} en República Dominicana.",
        "scanner_question": rules_en[0] if rules_en else "Is there any person in this area?",
        "rules": rules_en[:3],
        "system_prompt": system_prompt,
        "yolo_triggers": ["person"],
        "schedule": {
            "open": schedule.get("open", "08:00"),
            "close": schedule.get("close", "22:00"),
        },
        "night_mode": True,
        "grid_size": 12,
        "frame_interval_s": 3,
        "cooldown_min": 5,
        "active": True,
        "_rules_es": rules_es[:3],
    }

    return config


def save_camera_config(user_id: str, config: dict, storage_root: Path):
    camera_id = config.get("camera_id", "cam_unknown")
    cam_dir = storage_root / "users" / user_id / "cameras" / camera_id
    cam_dir.mkdir(parents=True, exist_ok=True)
    (cam_dir / "sessions").mkdir(exist_ok=True)
    (cam_dir / "frames").mkdir(exist_ok=True)
    (cam_dir / "camera.json").write_text(
        __import__("json").dumps(config, indent=2, ensure_ascii=False)
    )

    user_path = storage_root / "users" / user_id / "user.json"
    if user_path.exists():
        ud = __import__("json").loads(user_path.read_text())
        cams = ud.get("cameras", [])
        ids = [c.get("camera_id") for c in cams]
        if camera_id not in ids:
            cams.append({
                "camera_id": camera_id,
                "name": config.get("name", camera_id),
                "zone": config.get("zone", ""),
                "active": True,
            })
            ud["cameras"] = cams
            user_path.write_text(__import__("json").dumps(ud, indent=2, ensure_ascii=False))

    logger.info(f"📷 camera.json guardado: {camera_id} → {cam_dir}")
    return cam_dir
