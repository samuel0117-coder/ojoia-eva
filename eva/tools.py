"""
eva/tools.py — Herramientas que Eva puede invocar durante el chat.

Tools para configuración (SETUP mode):
- save_business_data: Guarda un dato del negocio extraído de la conversación
- save_camera_config: Guarda la configuración de una cámara
- get_latest_frame: Obtiene imagen reciente de una cámara
- analyze_frame: Analiza la imagen actual con Eva

Tools para consulta (OS mode):
- search_events: Busca eventos por query/fecha/cámara en el diario JSON rico
- get_activity_summary: Resume la actividad de un día
- find_anomalias: Encuentra actividad sospechosa por severidad
"""
import json
import time
import logging

logger = logging.getLogger(__name__)
import logging
import re
import time
from datetime import date as _date, timedelta
from pathlib import Path
from typing import Dict, List, Any
from eva.camera_builder import normalize_camera_vigilance_config, build_vigilance_prompt

logger = logging.getLogger(__name__)
STORAGE_ROOT = Path("/home/sam/storage")


# ═══════════════════════════════════════════════════════════════
# TOOLS PARA SETUP
# ═══════════════════════════════════════════════════════════════

async def tool_save_business_data(user_id: str, field: str, value: str) -> dict:
    """Guarda un dato del negocio."""
    try:
        uf = STORAGE_ROOT / "users" / user_id / "user.json"
        user_data = json.loads(uf.read_text()) if uf.exists() else {
            "user_id": user_id, "owner": {}, "business_name": "",
            "business_type": "", "schedule": {"open": "07:00", "close": "19:00"},
            "main_concerns": [], "cameras": {},
        }
        if field == "business_name":
            user_data["business_name"] = value
        elif field == "business_type":
            user_data["business_type"] = value
        elif field == "owner_name":
            user_data.setdefault("owner", {})["name"] = value
        elif field == "concern":
            user_data.setdefault("main_concerns", []).append(value)
        elif field == "schedule_open":
            user_data.setdefault("schedule", {})["open"] = value
        elif field == "schedule_close":
            user_data.setdefault("schedule", {})["close"] = value
        else:
            user_data[field] = value
        tmp = uf.with_suffix(".tmp")
        tmp.write_text(json.dumps(user_data, indent=2, ensure_ascii=False))
        tmp.replace(uf)
        return {"success": True, "field": field, "value": value}
    except Exception as e:
        return {"success": False, "error": str(e)}


async def tool_save_camera_config(user_id: str, camera_id: str, **kwargs) -> dict:
    """Guarda la configuración de una cámara."""
    try:
        cam_dir = STORAGE_ROOT / "users" / user_id / "cameras" / camera_id
        cam_dir.mkdir(parents=True, exist_ok=True)
        cam_file = cam_dir / "camera.json"
        config = json.loads(cam_file.read_text()) if cam_file.exists() else {}
        config.update(kwargs)
        cam_file.write_text(json.dumps(config, indent=2, ensure_ascii=False))
        return {"success": True, "camera_id": camera_id}
    except Exception as e:
        return {"success": False, "error": str(e)}


def _load_camera_config(user_id: str, camera_id: str) -> dict:
    cam_file = STORAGE_ROOT / "users" / user_id / "cameras" / camera_id / "camera.json"
    if cam_file.exists():
        return json.loads(cam_file.read_text())
    return {"camera_id": camera_id, "zone": "zona principal", "schedule": {"open": "08:00", "close": "22:00"}}


def _save_camera_config(user_id: str, camera_id: str, config: dict):
    cam_file = STORAGE_ROOT / "users" / user_id / "cameras" / camera_id / "camera.json"
    cam_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = cam_file.with_suffix(".tmp")
    tmp.write_text(json.dumps(config, indent=2, ensure_ascii=False))
    tmp.replace(cam_file)


def _merge_config(base: dict, incoming: dict) -> dict:
    result = dict(base or {})
    for key, value in (incoming or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge_config(result[key], value)
        else:
            result[key] = value
    return result


def _parse_config_value(value):
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return json.loads(text)
        except Exception:
            return text
    return value


async def tool_get_vigilance_config(user_id: str, camera_id: str = "") -> dict:
    """Obtiene configuración de protección de una cámara."""
    try:
        if not camera_id:
            cams = sorted((STORAGE_ROOT / "users" / user_id / "cameras").iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)
            camera_id = cams[0].name if cams else ""
        config = normalize_camera_vigilance_config(_load_camera_config(user_id, camera_id))
        mode = "sentinel" if _is_vigilance_mode(config.get("schedule", {}), config.get("vigilance", {})) else "normal"
        return {
            "success": True,
            "camera_id": camera_id,
            "mode": mode,
            "system_prompt": config.get("system_prompt", ""),
            "vigilance": config.get("vigilance", {}),
            "schedule": config.get("schedule", {}),
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


async def tool_update_vigilance_config(user_id: str, camera_id: str, vigilance: dict = None,
                                       schedule: dict = None, mode: str = None,
                                       system_prompt: str = None) -> dict:
    """Actualiza configuración de protección y regenera el prompt."""
    try:
        if not camera_id:
            cams = sorted((STORAGE_ROOT / "users" / user_id / "cameras").iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)
            camera_id = cams[0].name if cams else ""
        if not camera_id:
            return {"success": False, "error": "No hay cámaras configuradas"}
        config = normalize_camera_vigilance_config(_load_camera_config(user_id, camera_id))
        if schedule:
            config["schedule"] = _merge_config(config.get("schedule", {}), schedule)
        if vigilance:
            config["vigilance"] = _merge_config(config.get("vigilance", {}), vigilance)
        current_mode = mode or ("sentinel" if _is_vigilance_mode(config.get("schedule", {}), config.get("vigilance", {})) else "normal")
        config["system_prompt"] = system_prompt if system_prompt else build_vigilance_prompt(config, current_mode)
        _save_camera_config(user_id, camera_id, config)
        return {
            "success": True,
            "camera_id": camera_id,
            "mode": current_mode,
            "system_prompt": config.get("system_prompt", ""),
            "vigilance": config.get("vigilance", {}),
            "schedule": config.get("schedule", {}),
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


def _is_vigilance_mode(schedule: dict, vigilance: dict) -> bool:
    try:
        from datetime import datetime, timedelta
        now = datetime.now()
        open_h, open_m = map(int, (schedule or {}).get("open", "08:00").split(":"))
        close_h, close_m = map(int, (schedule or {}).get("close", "22:00").split(":"))
        grace = int((vigilance or {}).get("grace_minutes", 15))
        close_dt = datetime(now.year, now.month, now.day, close_h, close_m) + timedelta(minutes=grace)
        open_dt = datetime(now.year, now.month, now.day, open_h, open_m)
        return now < open_dt or now >= close_dt
    except Exception:
        return False


async def tool_get_latest_frame(user_id: str, camera_id: str = "") -> dict:
    """Obtiene la imagen más reciente de una cámara."""
    base = STORAGE_ROOT / "users" / user_id / "cameras"
    if not base.exists():
        return {"has_frame": False}
    dirs = [base / camera_id] if camera_id else sorted(base.iterdir(), key=lambda d: d.stat().st_mtime if d.is_dir() else 0, reverse=True)
    for d in dirs:
        if not d.is_dir():
            continue
        latest = d / "latest_vigilance.jpg" if (d / "latest_vigilance.jpg").exists() else None
        if not latest:
            jpgs = sorted(d.glob("**/*.jpg"), key=lambda p: p.stat().st_mtime, reverse=True)
            latest = jpgs[0] if jpgs else None
        if latest:
            return {"has_frame": True, "frame_path": str(latest), "camera_id": d.name}
    return {"has_frame": False}


async def tool_analyze_frame(user_id: str, camera_id: str = "", prompt: str = "") -> dict:
    """Analiza una imagen con Eva."""
    try:
        import httpx, base64
        frame_info = await tool_get_latest_frame(user_id, camera_id)
        if not frame_info.get("has_frame"):
            return {"success": False, "error": "No hay imagen disponible"}
        with open(frame_info["frame_path"], "rb") as f:
            img_b64 = base64.b64encode(f.read()).decode()
        if not prompt:
            prompt = "Describe detalladamente lo que ves en esta imagen de seguridad."
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                "http://localhost:8004/v1/chat/completions",
                json={"model": "qwen", "messages": [{"role": "user", "content": [
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"}},
                    {"type": "text", "text": prompt}
                ]}], "max_tokens": 300},
            )
            resp.raise_for_status()
            analysis = resp.json()["choices"][0]["message"]["content"]
        return {"success": True, "analysis": analysis, "camera_id": frame_info.get("camera_id", camera_id)}
    except Exception as e:
        return {"success": False, "error": str(e)}


# ═══════════════════════════════════════════════════════════════
# TOOLS PARA FACE ID
# ═══════════════════════════════════════════════════════════════

async def tool_identify_face(user_id: str, camera_id: str = "") -> dict:
    """Identifica quién aparece en el frame actual de una cámara."""
    try:
        import httpx, base64
        frame_info = await tool_get_latest_frame(user_id, camera_id)
        if not frame_info.get("has_frame"):
            return {"success": False, "error": "No hay imagen disponible"}
        with open(frame_info["frame_path"], "rb") as f:
            frame_b64 = base64.b64encode(f.read()).decode()
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                "http://localhost:8005/api/identity/identify-frame",
                json={"user_id": user_id, "frame_b64": frame_b64, "threshold": 0.45},
            )
            resp.raise_for_status()
            data = resp.json()
        identified = data.get("identified", [])
        if identified:
            person = identified[0]
            return {
                "success": True,
                "identified": True,
                "person_name": person.get("person_name", "desconocido"),
                "person_id": person.get("person_id", ""),
                "confidence": person.get("confidence", 0),
                "message": f"Vi a {person.get('person_name', 'alguien')} (confianza: {person.get('confidence', 0):.0%})",
            }
        return {
            "success": True,
            "identified": False,
            "message": "Vi a una persona pero no está registrada como empleado.",
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


async def tool_list_employees(user_id: str) -> dict:
    """Lista los empleados registrados."""
    try:
        employees_file = STORAGE_ROOT / "users" / user_id / "business" / "employees.json"
        if employees_file.exists():
            data = json.loads(employees_file.read_text())
            employees = list(data.get("by_id", {}).values())
            if employees:
                names = ", ".join(e.get("name", "?") for e in employees)
                roles = ", ".join(set(e.get("role", "?") for e in employees))
                return {
                    "success": True,
                    "count": len(employees),
                    "employees": employees,
                    "message": f"Hay {len(employees)} empleado(s) registrado(s): {names}. Roles: {roles}.",
                }
        return {
            "success": True,
            "count": 0,
            "employees": [],
            "message": "No hay empleados registrados aún. Puedes registrar uno desde el chat con 'registrar empleado'.",
        }
    except Exception as e:
        return {"success": False, "error": str(e)}

def _iter_events(user_id: str, camera_id: str = None, date_filter: str = None):
    """Iterador sobre eventos del diario con filtros."""
    base = STORAGE_ROOT / "users" / user_id / "cameras"
    if not base.exists():
        return
    cam_dirs = [base / camera_id] if camera_id and (base / camera_id).exists() else base.iterdir()
    for cam_dir in cam_dirs:
        if not cam_dir.is_dir():
            continue
        events_dir = cam_dir / "events"
        if not events_dir.exists():
            continue
        for evt_file in sorted(events_dir.glob("*.json"), key=lambda p: (json.loads(p.read_text()).get("timestamp", 0) if p.name.endswith(".json") else 0), reverse=True):
            try:
                evt = json.loads(evt_file.read_text())
                if date_filter:
                    evt_date = evt.get("datetime", "")[:10]
                    evt_ts = int(evt.get("timestamp", 0) or 0)
                    if date_filter == "today" and evt_date != _date.today().isoformat():
                        continue
                    if date_filter == "yesterday" and evt_date != (_date.today() - timedelta(days=1)).isoformat():
                        continue
                    if date_filter == "recent" and evt_ts < int(time.time()) - 24 * 60 * 60:
                        continue
                    if date_filter not in ("today", "yesterday", "recent") and evt_date != date_filter:
                        continue
                yield evt, cam_dir.name
            except Exception:
                pass


def _parse_json_text(value):
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return {}
    text = value.strip()
    if not text:
        return {}
    text = re.sub(r"\{[\s]*\.\.\.[\s]*\}", "{}", text)
    text = re.sub(r"\[[\s]*\.\.\.[\s]*\]", "[]", text)
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        pass
    match = re.search(r"\{[\s\S]*\}", text)
    if match:
        try:
            parsed = json.loads(match.group())
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            pass
    for key in ("summary", "description"):
        key_match = re.search(rf'"{key}"\s*:\s*"((?:\\.|[^"\\])*)"', text)
        if key_match:
            try:
                return {key: json.loads('"' + key_match.group(1) + '"')}
            except Exception:
                return {key: key_match.group(1)}
    return {}


def _event_qwen(evt: dict) -> dict:
    q = evt.get("qwen", {}) if isinstance(evt.get("qwen"), dict) else {}
    qj = evt.get("qwen_json", {}) if isinstance(evt.get("qwen_json"), dict) else {}
    parsed = _parse_json_text(evt.get("summary"))
    parsed_desc = _parse_json_text(evt.get("description"))
    merged = {}
    for source in (parsed_desc, parsed, q, qj):
        if isinstance(source, dict):
            merged.update(source)
    if not merged and evt.get("event_type") in ("vigilance_alert", "night_alert"):
        classes = evt.get("yolo_classes") or []
        if isinstance(classes, str):
            classes = [c.strip() for c in classes.split(",") if c.strip()]
        count = evt.get("yolo_count") or len(classes) or 0
        summary = evt.get("description") or f"Modo centinela: {count} objeto(s) detectado(s): {', '.join(classes)}"
        merged.update({
            "summary": summary,
            "description": summary,
            "violation": True,
            "importance": "alta",
            "importancia": "alta",
            "mode": "centinela",
            "details": {"persons": count},
            "anomalias": [{"tipo": "objeto en centinela", "descripcion": summary, "severidad": "alta"}],
        })
    for key in ("summary", "description"):
        if isinstance(merged.get(key), str):
            nested = _parse_json_text(merged[key])
            if nested.get("summary"):
                merged[key] = nested["summary"]
            elif nested.get("description"):
                merged[key] = nested["description"]
    if "importance" in merged and "importancia" not in merged:
        merged["importancia"] = merged["importance"]
    return merged


def _enrich_description_from_metadata(evt: dict, desc: str, qj: dict) -> str:
    metadata = evt.get("metadata", {}) if isinstance(evt.get("metadata"), dict) else {}
    yolo_classes = metadata.get("yolo_classes") or evt.get("yolo_classes") or []
    if isinstance(yolo_classes, str):
        yolo_classes = [c.strip() for c in yolo_classes.split(",") if c.strip()]
    total_yolo = int(metadata.get("total_yolo_objects") or evt.get("total_yolo_objects") or 0 or 0)
    person_count = sum(1 for c in yolo_classes if str(c).lower() == "person") if isinstance(yolo_classes, list) else 0
    if not person_count and total_yolo > 0:
        person_count = max(1, total_yolo)
    qj_details = qj.get("details") if isinstance(qj.get("details"), dict) else {}
    fallback_details = metadata.get("qwen_details") if isinstance(metadata.get("qwen_details"), dict) else {}
    if not qj_details and isinstance(fallback_details, dict):
        qj_details = fallback_details
    generic_markers = ["escena tranquila", "escena repetitiva", "sin personas", "ninguna persona",
                       "personas adicionales", "sin actividad sospechosa", "con ninguna actividad"]
    is_generic = any(m in desc.lower() for m in generic_markers)
    parts = []
    if person_count > 0 and not qj_details.get("persons_description"):
        parts.append(f"YOLO detectó {person_count} persona(s) en el grid; Qwen no distinguió más detalles visibles.")
    if qj_details.get("persons_description"):
        parts.append(str(qj_details["persons_description"]))
    if qj_details.get("scene_context"):
        parts.append(str(qj_details["scene_context"]))
    for key in ("actions_visible", "objects_visible", "clothing_visible"):
        v = qj_details.get(key)
        if isinstance(v, list) and v:
            parts.append(", ".join(str(x) for x in v))
        elif v:
            parts.append(str(v))
    tags = qj.get("search_tags") or metadata.get("qwen_search_tags") or []
    if tags:
        parts.append(", ".join(str(t) for t in tags))
    detail_text = " ".join(parts).strip()
    if is_generic and detail_text:
        return f"{detail_text} {desc}".strip()
    return " ".join([x for x in [desc, detail_text] if x]).strip() or "Sin descripción"


def _event_description_simple(evt: dict) -> str:
    qj = _event_qwen(evt)
    desc = qj.get("summary") or qj.get("description") or evt.get("description") or evt.get("summary") or ""
    if isinstance(desc, dict):
        desc = json.dumps(desc, ensure_ascii=False)
    desc = str(desc).strip()
    return _enrich_description_from_metadata(evt, desc, qj)


def _event_description(evt: dict) -> str:
    qj = _event_qwen(evt)
    desc = qj.get("summary") or qj.get("description") or evt.get("description") or evt.get("summary") or ""
    if isinstance(desc, dict):
        desc = json.dumps(desc, ensure_ascii=False)
    desc = str(desc).strip()
    try:
        from orchestrator import _description_detail_parts, _is_generic_qwen_summary
    except Exception:
        return _enrich_description_from_metadata(evt, desc, qj)
    desc = qj.get("summary") or qj.get("description") or evt.get("description") or evt.get("summary") or ""
    detail_parts = _description_detail_parts(qj, evt)
    metadata = evt.get("metadata", {}) if isinstance(evt.get("metadata"), dict) else {}
    yolo_classes = metadata.get("yolo_classes") or evt.get("yolo_classes") or []
    if isinstance(yolo_classes, str):
        yolo_classes = [c.strip() for c in yolo_classes.split(",") if c.strip()]
    total_yolo = int(metadata.get("total_yolo_objects") or evt.get("total_yolo_objects") or evt.get("yolo_count") or 0 or 0)
    person_count = sum(1 for c in yolo_classes if str(c).lower() == "person") if isinstance(yolo_classes, list) else 0
    if not person_count and total_yolo > 0:
        person_count = max(1, min(total_yolo, 16))
    vision = qj.get("vision", {}) if isinstance(qj.get("vision"), dict) else {}
    v_persons = vision.get("persons", []) if isinstance(vision.get("persons"), list) else []
    v_scene = vision.get("scene", "")
    v_objects = vision.get("objects", []) if isinstance(vision.get("objects"), list) else []
    if person_count > 0 and not any("persona" in p.lower() for p in detail_parts):
        if v_persons:
            person_descs = []
            for p in v_persons:
                location = p.get("location", "")
                clothing = p.get("clothing", [])
                actions_list = p.get("acciones", [])
                parts = []
                if clothing:
                    parts.append(" con ".join(str(c) for c in clothing))
                if location:
                    parts.append(location)
                if actions_list:
                    parts.append(" y ".join(str(a) for a in actions_list))
                if parts:
                    person_descs.append("Persona" + " ".join(parts))
            if person_descs:
                detail_parts.insert(0, ". ".join(person_descs))
            elif v_scene:
                detail_parts.insert(0, v_scene)
        elif v_scene and not detail_parts:
            detail_parts.insert(0, v_scene)
    if isinstance(desc, dict):
        desc = json.dumps(desc, ensure_ascii=False)
    desc = str(desc).strip()
    detail_text = " ".join(detail_parts).strip()
    if _is_generic_qwen_summary(desc) and detail_text:
        enriched = f"{detail_text} {desc}".strip()
    else:
        enriched = " ".join([x for x in [desc, detail_text] if x]).strip()
    return enriched or "Sin descripción"


def _event_yolo(evt: dict) -> dict:
    meta = evt.get("metadata", {}) if isinstance(evt.get("metadata"), dict) else {}
    y = evt.get("yolo", {}) if isinstance(evt.get("yolo"), dict) else {}
    classes = meta.get("yolo_classes") or evt.get("yolo_classes") or y.get("classes") or []
    if isinstance(classes, str):
        classes = [c.strip() for c in classes.split(",") if c.strip()]
    return {"count": evt.get("total_yolo_objects") or evt.get("yolo_count") or y.get("count") or 0, "classes": classes}


def _event_is_alert(evt: dict) -> bool:
    """True si el evento tiene attention_hits (nuevo sistema) o es legacy violation."""
    if evt.get("attention_hits"):
        return True
    qjson = _event_qwen(evt)
    importancia = str(qjson.get("importancia", "")).lower()
    anomalias = qjson.get("anomalias", []) if isinstance(qjson.get("anomalias"), list) else []
    high_severity = any(str(a.get("severidad") if isinstance(a, dict) else a).lower() in ("alta", "critica", "crítica") for a in anomalias)
    return evt.get("event_type") in ("violation", "vigilance_alert", "night_alert") or bool(qjson.get("violation")) or importancia in ("alta", "critica") or high_severity


def _attach_event_package(evt: dict, user_id: str, camera_id: str):
    folder = STORAGE_ROOT / "users" / user_id / "cameras" / camera_id / "events" / evt.get("event_id", "")
    mp4 = folder / f"{evt.get('event_id', '')}.mp4"
    if mp4.exists() and not evt.get("video_file"):
        evt["video_file"] = mp4.name
    frames_dir = folder / "frames"
    if frames_dir.exists() and not evt.get("frames"):
        frames = []
        for fp in sorted(frames_dir.glob("frame_*.jpg")):
            try:
                frames.append({"timestamp": evt.get("timestamp", 0), "datetime": evt.get("datetime", ""), "file": fp.name, "size": fp.stat().st_size, "index": len(frames)})
            except Exception:
                pass
        if frames:
            evt["frames"] = frames
            evt["frames_count"] = len(frames)
            evt["clip_type"] = "event_package"
    return evt


def _event_persons(evt: dict) -> int:
    qj = _event_qwen(evt)
    details = qj.get("details", {}) if isinstance(qj.get("details"), dict) else {}
    for key in ("persons", "person_count", "persons_visible", "personas", "personas_contadas", "people_count"):
        try:
            value = details.get(key)
            if value is None:
                value = qj.get(key)
            if value is not None and int(value) > 0:
                return int(value)
        except Exception:
            pass
    # ── Preferencia: tracker IDs únicos (datos reales basados en tracking) ──
    metadata = evt.get("metadata", {}) if isinstance(evt.get("metadata"), dict) else {}
    pt = metadata.get("person_tracking") if isinstance(metadata, dict) else None
    if isinstance(pt, dict) and pt.get("unique_persons"):
        return int(pt.get("unique_persons"))
    yolo_classes = metadata.get("yolo_classes") or evt.get("yolo_classes") or []
    if isinstance(yolo_classes, str):
        yolo_classes = [c.strip() for c in yolo_classes.split(",") if c.strip()]
    if isinstance(yolo_classes, list):
        person_count = sum(1 for c in yolo_classes if str(c).lower() == "person")
        total_yolo = int(metadata.get("total_yolo_objects") or evt.get("total_yolo_objects") or evt.get("yolo_count") or 0 or 0)
        return max(person_count, 1) if person_count == 0 and total_yolo > 0 else person_count
    return _event_yolo(evt).get("classes", []).count("person")


async def tool_search_events(user_id: str, query: str = "", date: str = None,
                              camera_id: str = None, limit: int = 10,
                              person_class: str = None,   # P4: hombre|mujer|nino|anciano
                              clothing: str = None,        # P4: "rojo", "verde", "camisa blanca"
                              min_persons: int = None,      # P4: >= N personas
                              max_persons: int = None,      # P4: <= N personas
                              activity: str = None,        # P4: "trabajando","hablando","entrando"
                              importance: str = None) -> dict:
    """
    Busca eventos en el diario con filtros semánticos (P4).

    Filtros disponibles:
      query        — palabras sueltas (compatibilidad vieja)
      person_class — hombre | mujer | nino | anciano
      clothing     — color o prenda (rojo, verde, camisa, jean...)
      min_persons  — >= N personas en el evento
      max_persons  — <= N
      activity     — verbo/acción (trabajando, hablando, entrando)
      importance   — normal | baja | media | alta | critica
      date         — today | yesterday | YYYY-MM-DD
      camera_id    — restringe a una cámara concreta
      limit        — tamaño de página

    Internamente combina las words de 'query' con sinónimos del filtro para
    hacer match contra summary+description+qwen_json.
    """
    # Normalizar filtros
    pc, cl, act, imp = (person_class or "").lower().strip(), (clothing or "").lower().strip(), (activity or "").lower().strip(), (importance or "").lower().strip()
    query_lower = (query or "").lower().strip()
    # Sinónimos para matching tolerante
    synonym_map = {
        "hombre": ["hombre", "masculino", "varón", "varon", "caballero", "chico"],
        "mujer": ["mujer", "femenino", "fémina", "chica", "señora", "senora", "dam a"],
        "nino": ["nino", "niño", "nena", "niña", "menor", "infante", "criatura", "bebe", "bebé"],
        "anciano": ["anciano", "mayor", "viejo", "abuelo", "abuela", "adulto mayor"],
        "alto": ["alto"], "critico": ["critico", "crítico", "alta", "critica"],
    }
    filter_words = []
    if pc:
        for syn in [pc] + synonym_map.get(pc, []):
            filter_words.append(syn)
    if cl:
        # clothing match partial — cualquier color/ prenda
        for token in cl.split():
            filter_words.append(token)
    if act:
        for token in act.split():
            filter_words.append(token)
    if query_lower:
        for w in query_lower.split():
            if len(w) >= 4: filter_words.append(w)

    results = []
    for evt, cam_name in _iter_events(user_id, camera_id, date):
        # Cobertura completa del searchable del evento
        qjson = evt.get("qwen_json", {}) if isinstance(evt.get("qwen_json"), dict) else {}
        parts = [
            evt.get("summary", "") or evt.get("description", ""),
            str(qjson.get("summary", "") or qjson.get("scene", "")),
            str(qjson.get("anomalias", "")),
            str((qjson.get("evidence") or [])),
        ]
        vision = qjson.get("vision") if isinstance(qjson.get("vision"), dict) else {}
        persons = vision.get("persons") if isinstance(vision.get("persons"), list) else []
        # Texto completo por persona (incluye classifications P3)
        persona_text = []
        for p in persons:
            if not isinstance(p, dict): continue
            persona_text.append(" ".join(str(p.get(k, "")) for k in ("desc", "gender_guess", "age_group", "clothing_top", "clothing_bottom")))
        parts.extend(persona_text)
        # Tags del qwen_details
        qd = qjson.get("details") if isinstance(qjson.get("details"), dict) else {}
        parts.extend([
            " ".join(qd.get("genders_visible") or []),
            " ".join(qd.get("ages_visible") or []),
            " ".join(qd.get("clothing_top_visible") or []),
            " ".join(qd.get("clothing_bottom_visible") or []),
        ])
        searchable = " ".join(parts).lower()

        # Filtrar por texto (palabras)
        if filter_words and not any(w in searchable for w in filter_words):
            continue

        # Filtros numéricos por count de personas
        pv = qd.get("persons_visible")
        if isinstance(pv, (int, float)):
            if min_persons is not None and pv < min_persons: continue
            if max_persons is not None and pv > max_persons: continue

        # Filtro por importancia exacta
        if imp:
            evt_imp = (qjson.get("importancia") or qjson.get("importance") or "").lower()
            if evt_imp != imp and not (imp == "alto" and evt_imp == "alta"):
                continue

        evt = _attach_event_package(evt, user_id, cam_name)
        qjson = _event_qwen(evt)
        results.append({
            "event_id": evt["event_id"],
            "datetime": evt.get("datetime", ""),
            "camera_name": cam_name,
            "event_type": evt.get("event_type", ""),
            "description": _event_description(evt),
            "summary": qjson.get("summary", _event_description(evt)),
            "qwen_json": qjson,
            "importancia": qjson.get("importancia", "baja"),
            "anomalias": qjson.get("anomalias", []),
            "persons": _event_persons(evt),
            "yolo": _event_yolo(evt),
            "frame_url": f"/api/event-frame/{evt['event_id']}?user_id={user_id}",
            "thumb_url": f"/api/event-thumb/{evt['event_id']}?user_id={user_id}",
        })
        if len(results) >= limit:
            break
    return {"found": len(results), "events": results, "filters_applied": {
        "person_class": pc or None,
        "clothing": cl or None,
        "min_persons": min_persons,
        "max_persons": max_persons,
        "activity": act or None,
        "importance": imp or None,
        "query": query or None,
    }}


async def tool_event_book(user_id: str, date: str = "today", camera_id: str = None,
                          group_by: str = "hour",
                          only_importance: str = None,
                          max_entries: int = 40) -> dict:
    """
    P5 — Indice cronologico navegable del libro de eventos.

    Devuelve una vista agrupada (por 'hour' | 'camera' | 'camera_hour') del
    periodo solicitado, optimizada para que el chat pueda 'explicar que
    paso en la camara X entre 10 y 14' sin enumerar 200 eventos.

    Params:
      group_by         hour | camera | camera_hour | ten_minute
      only_importance  normal | baja | media | alta | critica  (filtra)
      max_entries      limite duro de entradas devueltas

    Respuesta:
      {
        period, total_events, cameras,
        groups: [{label, events_count, earliest, latest, sample_ids, ...}],
        recent: [...ultimos N eventos crudos para profundizar]
      }
    """
    from collections import defaultdict
    bins = defaultdict(list)
    total = 0
    by_cam = defaultdict(int)
    cameras_set = set()
    important_filters = {"normal", "baja", "media", "alta", "critica"}

    def _bucket_key(dt_str: str, cam: str, gb: str) -> str:
        # dt_str format: 2026-07-10T13:25:31
        try:
            hh = dt_str[11:13]
            mm = dt_str[14:16]
        except Exception:
            return "?"
        if gb == "hour":
            return f"{dt_str[:10]} {hh}:00"
        if gb == "ten_minute":
            tens = (int(mm) // 10) * 10
            return f"{dt_str[:10]} {hh}:{tens:02d}"
        if gb == "camera":
            return cam
        if gb == "camera_hour":
            return f"{cam} | {dt_str[:10]} {hh}:00"
        return hh

    g = group_by if group_by in ("hour", "camera", "camera_hour", "ten_minute") else "hour"
    only_imp = (only_importance or "").lower().strip()
    if only_imp not in important_filters:
        only_imp = ""

    recent = []
    for evt, cam_name in _iter_events(user_id, camera_id, date):
        qjson = evt.get("qwen_json", {}) if isinstance(evt.get("qwen_json"), dict) else {}
        imp = (qjson.get("importancia") or qjson.get("importance") or "baja").lower()
        if only_imp and imp != only_imp and not (only_imp == "alto" and imp == "alta"):
            continue
        dt = evt.get("datetime") or ""
        cams_canonical = cam_name or "desconocida"
        cameras_set.add(cams_canonical)
        by_cam[cams_canonical] += 1
        bucket = _bucket_key(dt, cams_canonical, g)
        ev_short = {
            "event_id": evt.get("event_id", ""),
            "datetime": dt,
            "camera": cams_canonical,
            "importancia": imp,
            "anomalias": (qjson.get("anomalias") or [])[:3],
            "persons_visible": (qjson.get("details") or {}).get("persons_visible", 0),
            "summary": (qjson.get("summary") or evt.get("description") or "")[:160],
            "frame_url": f"/api/event-frame/{evt.get('event_id','')}?user_id={user_id}",
        }
        bins[bucket].append(ev_short)
        recent.append(ev_short)
        total += 1
        if len(recent) > 200: recent.pop(0)  # ventana de recientes acotada

    # Ordenar groups cronológicamente si el bucket incluye hora
    def _sort_key(label):
        # Extrae marca temporal si existe
        # formatos: "2026-07-10 HH:00", "cam | 2026-07-10 HH:00", "HH:00", "2026-07-10 HH:M0"
        for j in range(len(label) - 1, -1, -1):
            if label[j] == '|':
                tail = label[j+2:]; break
        else:
            tail = label
        return tail

    groups = []
    for label, evs in bins.items():
        evs_sorted = sorted(evs, key=lambda e: e["datetime"] or "")
        groups.append({
            "label": label,
            "events_count": len(evs_sorted),
            "earliest": evs_sorted[0]["datetime"] if evs_sorted else "",
            "latest": evs_sorted[-1]["datetime"] if evs_sorted else "",
            "importancia_max": _max_importance([e["importancia"] for e in evs_sorted]),
            "first_event_id": evs_sorted[0]["event_id"] if evs_sorted else "",
            "sample_ids": [e["event_id"] for e in evs_sorted[:3]]
        })
    groups.sort(key=lambda g: _sort_key(g["label"]))
    if len(groups) > max_entries:
        groups = groups[-max_entries:]  # recortamos a lo más reciente

    # recent: ultimos 12
    recent = recent[-12:]

    return {
        "success": True,
        "period": date or "today",
        "group_by": g,
        "only_importance": only_imp or None,
        "total_events": total,
        "cameras": sorted(cameras_set),
        "camera_counts": dict(by_cam),
        "groups": groups,
        "recent": recent
    }


def _max_importance(items):
    order = ["critica", "alta", "media", "normal", "baja"]
    for lvl in order:
        if any(i == lvl for i in items): return lvl
    return "baja"


async def tool_get_activity_summary(user_id: str, date: str = None, camera_id: str = None) -> dict:
    """Resume la actividad de un día desde el diario (enfoque descriptivo).

    NUEVO: No cuenta "violaciones" — cuenta observaciones, personas, transacciones.
    """
    events = []
    for evt, cam_name in _iter_events(user_id, camera_id, date or "today"):
        events.append(evt)

    if not events:
        return {"period": date or "today", "total_events": 0, "summary": "Sin eventos registrados."}

    total = len(events)
    last = events[0]

    persons_values = [_event_persons(e) for e in events if _event_persons(e) is not None]
    latest_yolo = _event_yolo(last)
    latest_qjson = _event_qwen(last)

    attention_events = [e for e in events if e.get("attention_hits")]
    normal_events = [e for e in events if not e.get("attention_hits")]

    total_platos = 0
    total_bebidas = 0
    total_fundas = 0
    total_clientes_estimado = 0
    for e in events:
        qwen = e.get("qwen_json", {}) if isinstance(e.get("qwen_json"), dict) else {}
        counts = qwen.get("counts", {}) if isinstance(qwen.get("counts"), dict) else {}
        total_platos += counts.get("platos_visibles", 0) or 0
        total_bebidas += counts.get("bebidas_visibles", 0) or 0
        total_fundas += counts.get("fundas_visibles", 0) or 0
        total_clientes_estimado += counts.get("clientes", 0) or 0

    # ── Personas únicas diarias: máximo de personas entre eventos (no suma) ──
    unique_persons_day = max(persons_values) if persons_values else 0

    summary_parts = [f"📊 Hoy se realizaron {total} análisis de seguridad."]
    if unique_persons_day > 0:
        summary_parts.append(f"👥 Se observaron hasta {unique_persons_day} persona(s) en la escena a la vez (según tracker).")
    if total_clientes_estimado > 0:
        summary_parts.append(f"🧑‍🤝‍🧑 Clientes observados: ~{total_clientes_estimado} (estimado).")
    if total_platos > 0:
        summary_parts.append(f"🍽️ Platos visibles en total: ~{total_platos}.")
    if total_bebidas > 0:
        summary_parts.append(f"🥤 Bebidas visibles: ~{total_bebidas}.")
    if total_fundas > 0:
        summary_parts.append(f"🛍️ Fundas utilizadas: ~{total_fundas}.")
    if attention_events:
        summary_parts.append(f"🔍 {len(attention_events)} evento(s) coincidieron con lo que me pediste vigilar.")

    last_summary = latest_qjson.get("summary") or last.get("summary", "Sin datos")
    summary_parts.append(f"📝 Último análisis: {last_summary[:150]}")

    notable_events = []
    for e in attention_events[:5]:
        qwen = e.get("qwen_json", {}) if isinstance(e.get("qwen_json"), dict) else {}
        notable_events.append({
            "event_id": e.get("event_id", ""),
            "datetime": e.get("datetime", ""),
            "timestamp": e.get("timestamp", 0),
            "camera_id": e.get("camera_id", ""),
            "camera_name": e.get("camera_name", ""),
            "description": e.get("description", "") or e.get("summary", ""),
            "summary": e.get("summary", "") or e.get("description", ""),
            "event_type": e.get("event_type", ""),
            "attention_hits": e.get("attention_hits", []),
            "qwen_analysis": e.get("qwen_analysis", {}),
            "qwen": qwen,
            "thumb_url": e.get("thumb_url", "") or (f"/api/event-thumb/{e['event_id']}?user_id={user_id}" if e.get("event_id") else ""),
            "frame_url": e.get("frame_url", "") or (f"/api/event-frame/{e['event_id']}?user_id={user_id}" if e.get("event_id") else ""),
            "video_file": e.get("video_file", ""),
            "persons": e.get("persons", 0),
        })
    if not notable_events:
        for e in events[:3]:
            qwen = e.get("qwen_json", {}) if isinstance(e.get("qwen_json"), dict) else {}
            notable_events.append({
                "event_id": e.get("event_id", ""),
                "datetime": e.get("datetime", ""),
                "timestamp": e.get("timestamp", 0),
                "camera_id": e.get("camera_id", ""),
                "camera_name": e.get("camera_name", ""),
                "description": e.get("description", "") or e.get("summary", ""),
                "summary": e.get("summary", "") or e.get("description", ""),
                "event_type": e.get("event_type", ""),
                "attention_hits": e.get("attention_hits", []),
                "qwen_analysis": e.get("qwen_analysis", {}),
                "qwen": qwen,
                "thumb_url": e.get("thumb_url", "") or (f"/api/event-thumb/{e['event_id']}?user_id={user_id}" if e.get("event_id") else ""),
                "frame_url": e.get("frame_url", "") or (f"/api/event-frame/{e['event_id']}?user_id={user_id}" if e.get("event_id") else ""),
                "video_file": e.get("video_file", ""),
                "persons": e.get("persons", 0),
            })

    return {
        "period": date or "today",
        "total_events": total,
        "attention_events": len(attention_events),
        "persons_total": unique_persons_day,
        "persons_analyses": len(persons_values),
        "counts_total": {
            "platos": total_platos,
            "bebidas": total_bebidas,
            "fundas": total_fundas,
            "clientes_estimado": total_clientes_estimado,
        },
        "last_yolo": latest_yolo,
        "last_summary": last_summary,
        "details": latest_qjson.get("details", {}),
        "notable_events": notable_events,
        "summary": "\n".join(summary_parts),
    }


async def tool_find_anomalies(user_id: str, min_severity: str = "media",
                               date: str = None, camera_id: str = None, limit: int = 10) -> dict:
    """Encuentra eventos con attention_hits (nuevo术语: observaciones relevantes)."""
    severity_order = {"baja": 0, "media": 1, "alta": 2, "critica": 3, "observacion": 0}
    min_level = severity_order.get(min_severity, 1)
    results = []
    for evt, cam_name in _iter_events(user_id, camera_id, date):
        attention_hits = evt.get("attention_hits", []) if isinstance(evt, dict) else []
        if attention_hits or _event_is_alert(evt):
            evt = _attach_event_package(evt, user_id, cam_name)
            results.append({
                "event_id": evt["event_id"],
                "datetime": evt.get("datetime", ""),
                "camera_name": cam_name,
                "tipo": "observacion",
                "descripcion": _event_description(evt),
                "attention_hits": attention_hits,
                "severidad": "observacion" if attention_hits else "alta",
                "anomaly": True,
                "frame_url": f"/api/event-frame/{evt['event_id']}?user_id={user_id}",
                "video_file": evt.get("video_file", ""),
                "frames": evt.get("frames", []),
            })
            if len(results) >= limit:
                break
            continue
        qjson = _event_qwen(evt)
        for anom in (qjson.get("anomalias", []) if isinstance(qjson.get("anomalias"), list) else []):
            if isinstance(anom, dict):
                sev = anom.get("severidad", "baja")
                if severity_order.get(sev, 0) >= min_level:
                    evt = _attach_event_package(evt, user_id, cam_name)
                    results.append({
                        "event_id": evt["event_id"],
                        "datetime": evt.get("datetime", ""),
                        "camera_name": cam_name,
                        "tipo": anom.get("tipo", ""),
                        "descripcion": anom.get("descripcion", ""),
                        "severidad": sev,
                        "anomaly": True,
                        "frame_url": f"/api/event-frame/{evt['event_id']}?user_id={user_id}",
                        "video_file": evt.get("video_file", ""),
                        "frames": evt.get("frames", []),
                    })
        if len(results) >= limit:
            break
    return {"found": len(results), "anomalies": results}


async def tool_latest_events(user_id: str, limit: int = 5,
                             date: str = None, camera_id: str = None) -> dict:
    """Lista los últimos análisis del diario."""
    events = []
    for evt, cam_name in _iter_events(user_id, camera_id, date or "today"):
        evt = _attach_event_package(evt, user_id, cam_name)
        qjson = _event_qwen(evt)
        events.append({
            "event_id": evt["event_id"],
            "datetime": evt.get("datetime", ""),
            "camera_name": cam_name,
            "event_type": evt.get("event_type", ""),
            "description": _event_description(evt),
            "summary": qjson.get("summary", _event_description(evt)),
            "importancia": qjson.get("importancia", "baja"),
            "anomaly": _event_is_alert(evt),
            "persons": _event_persons(evt),
            "yolo": _event_yolo(evt),
            "frame_url": f"/api/event-frame/{evt['event_id']}?user_id={user_id}",
        })
        if len(events) >= limit:
            break
    return {"found": len(events), "events": events}


async def tool_find_risks(user_id: str, date: str = None,
                          camera_id: str = None, limit: int = 10) -> dict:
    """Busca riesgos de incendio, humo o actividad sospechosa crítica."""
    risk_words = ("fuego", "humo", "incendio", "riesgo", "crítico", "critica", "alarma")
    results = []
    for evt, cam_name in _iter_events(user_id, camera_id, date):
        qjson = _event_qwen(evt)
        text = " ".join([
            _event_description(evt),
            str(qjson.get("details", "")),
            str(qjson.get("anomalias", "")),
            str(qjson.get("importancia", "")),
        ]).lower()
        if any(w in text for w in risk_words) or qjson.get("importancia") in ("alta", "critica") or _event_is_alert(evt):
            evt = _attach_event_package(evt, user_id, cam_name)
            results.append({
                "event_id": evt["event_id"],
                "datetime": evt.get("datetime", ""),
                "camera_name": cam_name,
                "description": _event_description(evt),
                "importancia": qjson.get("importancia", "baja"),
                "anomaly": True,
                "frame_url": f"/api/event-frame/{evt['event_id']}?user_id={user_id}",
            })
        if len(results) >= limit:
            break
    return {"found": len(results), "risks": results}


# ═══════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════

def load_business_json(user_id: str) -> dict:
    bp = STORAGE_ROOT / "users" / user_id / "user.json"
    if bp.exists():
        return json.loads(bp.read_text())
    return {
        "user_id": user_id, "owner": {}, "business_name": "",
        "business_type": "", "schedule": {"open": "07:00", "close": "19:00"},
        "main_concerns": [], "cameras": {},
    }


def save_business_json(user_id: str, data: dict):
    bp = STORAGE_ROOT / "users" / user_id / "user.json"
    bp.parent.mkdir(parents=True, exist_ok=True)
    tmp = bp.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    tmp.replace(bp)


async def tool_save_event(user_id: str, camera_id: str = "", summary: str = "",
                          importance: str = "baja") -> dict:
    """Guarda un evento de seguridad en el diario."""
    try:
        import uuid
        base = STORAGE_ROOT / "users" / user_id / "cameras"
        if not base.exists():
            return {"success": False, "error": "No hay cámaras configuradas"}
        if not camera_id:
            cams = sorted([d for d in base.iterdir() if d.is_dir()], key=lambda p: p.stat().st_mtime, reverse=True)
            if not cams:
                return {"success": False, "error": "No hay cámaras configuradas"}
            camera_id = cams[0].name
        cam_dir = base / camera_id
        events_dir = cam_dir / "events"
        events_dir.mkdir(parents=True, exist_ok=True)
        event_id = f"manual_{uuid.uuid4().hex[:12]}"
        from datetime import datetime
        now = datetime.now()
        evt = {
            "event_id": event_id,
            "timestamp": int(now.timestamp()),
            "datetime": now.strftime("%Y-%m-%d %H:%M"),
            "event_type": "manual_event",
            "summary": summary,
            "description": summary,
            "importance": importance,
            "camera_id": camera_id,
            "metadata": {}
        }
        evt_file = events_dir / f"{event_id}.json"
        evt_file.write_text(json.dumps(evt, indent=2, ensure_ascii=False))
        return {"success": True, "event_id": event_id, "message": f"Evento guardado: {summary}"}
    except Exception as e:
        return {"success": False, "error": str(e)}


async def tool_respond_directly(user_id: str, message: str = "") -> dict:
    """Respuesta directa sin consultar herramientas."""
    return {"success": True, "message": message, "tool": "respond_directly"}



async def tool_learn_from_feedback(event_id: str, is_real: bool, notes: str = None, user_id: str = None) -> dict:
    """Procesa feedback del usuario sobre un evento.

    - Si es falsa alarma: registra el false_alarm y guarda notas como contexto
    - Si es amenaza real: fortalece la atención en esa zona
    - Las notas del usuario se convierten en owner_notes para futuros análisis
    """
    try:
        import os, json
        event_file = None
        events_base = f"{STORAGE_ROOT}/users/{user_id}/cameras"
        if not os.path.exists(events_base):
            return {"success": False, "error": "Usuario no encontrado", "action": "none"}
        for cam_id in os.listdir(events_base):
            candidate = f"{events_base}/{cam_id}/events/{event_id}.json"
            if os.path.exists(candidate):
                event_file = candidate
                break
        if not event_file:
            return {"success": False, "error": "Evento no encontrado", "action": "none"}
        with open(event_file) as f:
            event_data = json.load(f)

        # Guardar feedback
        if "feedback" not in event_data:
            event_data["feedback"] = {}
        event_data["feedback"]["is_real"] = is_real
        event_data["feedback"]["user_id"] = user_id
        event_data["feedback"]["timestamp"] = int(time.time())
        if notes:
            event_data["feedback"]["notes"] = notes

        camera_id = event_data.get("camera_id", "")

        # ═══════════════════════════════════════════════════════════════════
        # FEEDBACK LOOP: Convertir notas del dueño en owner_notes
        # ═══════════════════════════════════════════════════════════════════
        if notes and camera_id:
            try:
                cam_file = f"{STORAGE_ROOT}/users/{user_id}/cameras/{camera_id}/camera.json"
                if os.path.exists(cam_file):
                    with open(cam_file) as f:
                        cam_cfg = json.load(f)
                    vigilance = cam_cfg.get("vigilance", {})
                    if not isinstance(vigilance, dict):
                        vigilance = {}

                    # Inicializar owner_notes si no existe
                    if "owner_notes" not in vigilance:
                        vigilance["owner_notes"] = []

                    # Agregar la nota del usuario (sin duplicar)
                    note_clean = notes.strip()
                    if note_clean and note_clean not in vigilance["owner_notes"]:
                        vigilance["owner_notes"].append(note_clean)
                        # Limitar a 20 notas para no inflar el prompt
                        vigilance["owner_notes"] = vigilance["owner_notes"][-20:]

                    cam_cfg["vigilance"] = vigilance
                    with open(cam_file, "w") as f:
                        json.dump(cam_cfg, f, indent=2, ensure_ascii=False)
                    logger.info(f"Owner note added for {camera_id}: {note_clean[:50]}")
            except Exception as e:
                logger.warning(f"Could not save owner_note: {e}")

        with open(event_file, "w") as f:
            json.dump(event_data, f, indent=2, ensure_ascii=False)

        # Registrar falsa alarma si aplica
        from orchestrator import register_false_alarm
        if not is_real and camera_id:
            register_false_alarm(user_id, camera_id)

        action = "false_alarm_registered" if not is_real else "feedback_recorded"
        return {"success": True, "action": action, "event_id": event_id, "note_saved": bool(notes)}
    except Exception as e:
        return {"success": False, "error": str(e), "action": "none"}



async def tool_count_people(user_id: str, camera_id: str = None, date: str = "today", start: float = None, end: float = None) -> dict:
    """Cuenta personas únicas detectadas por cámara usando tracker temporal.
    
    Lógica de tracker:
    - Lee eventos de vigilancia (vigilance_*.json) y eventos normales (evt_*.json)
    - Obtiene conteo de personas por evento
    - Agrupa eventos en "sesiones" separadas por gaps > 5 minutos
    - Suma el máximo de personas por sesión para estimar visitas únicas
    """
    try:
        from datetime import datetime, timedelta
        import time as _time
        
        user_dir = STORAGE_ROOT / "users" / user_id
        if not user_dir.exists():
            return {"success": True, "total_people": 0, "sessions": [], "message": "No hay cámaras"}
        
        # Resolver timestamps
        if date == "today":
            start_ts = start or datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
            end_ts = end or datetime.now().timestamp()
        elif date == "yesterday":
            yest = datetime.now() - timedelta(days=1)
            start_ts = start or yest.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
            end_ts = end or (yest + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        else:
            start_ts = start or (datetime.now() - timedelta(days=1)).timestamp()
            end_ts = end or datetime.now().timestamp()
        
        # Buscar cámaras
        cameras_dir = user_dir / "cameras"
        cameras = []
        if camera_id:
            if (cameras_dir / camera_id).exists():
                cameras = [camera_id]
        else:
            cameras = [p.name for p in cameras_dir.iterdir() if p.is_dir()]
        
        all_events = []
        for cam in cameras:
            events_dir = cameras_dir / cam / "events"
            if not events_dir.exists():
                continue
            
            # Eventos de vigilancia (modo centinela/vigilancia)
            for f in events_dir.glob("vigilance_*.json"):
                try:
                    data = json.loads(f.read_text())
                    ts = data.get("timestamp", 0) or data.get("timestamp_created", 0)
                    if start_ts <= ts <= end_ts:
                        count = data.get("yolo_count", 0) or 0
                        if count > 0:
                            all_events.append({"ts": ts, "count": count, "camera": cam, "id": f.stem})
                except:
                    continue
            
            # Eventos normales (evt_*.json)
            for f in events_dir.glob("evt_*.json"):
                try:
                    data = json.loads(f.read_text())
                    ts = data.get("timestamp", 0) or data.get("timestamp_created", 0) or data.get("datetime", 0)
                    if start_ts <= ts <= end_ts:
                        # P2: preferir metadata.person_tracking.unique_persons (más preciso)
                        metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
                        pt = metadata.get("person_tracking") if isinstance(metadata, dict) else None
                        if isinstance(pt, dict) and pt.get("unique_persons"):
                            count = int(pt["unique_persons"])
                        else:
                            count = (
                                data.get("persons")
                                or data.get("yolo_count", 0)
                                or data.get("qwen_json", {}).get("persons", 0)
                                or 0
                            )
                        if count > 0:
                            all_events.append({"ts": ts, "count": count, "camera": cam, "id": f.stem})
                except:
                    continue
        
        if not all_events:
            return {
                "success": True, 
                "total_people": 0, 
                "sessions": 0, 
                "events_count": 0,
                "message": f"No detecté personas en el período ({date})."
            }
        
        # Tracker: sesiones separadas por > 5 minutos
        all_events.sort(key=lambda x: x["ts"])
        GAP_SECONDS = 300  # 5 minutos
        sessions = []
        current_session = [all_events[0]]
        
        for event in all_events[1:]:
            if event["ts"] - current_session[-1]["ts"] > GAP_SECONDS:
                sessions.append(current_session)
                current_session = [event]
            else:
                current_session.append(event)
        sessions.append(current_session)
        
        # Sumar máximo de cada sesión
        total_people = sum(max(e["count"] for e in session) for session in sessions)
        
        # Peak info
        peak_event = max(all_events, key=lambda x: x["count"])
        peak_time = datetime.fromtimestamp(peak_event["ts"]).strftime("%H:%M")
        
        return {
            "success": True,
            "total_people": total_people,
            "sessions": len(sessions),
            "events_count": len(all_events),
            "peak_count": peak_event["count"],
            "peak_time": peak_time,
            "cameras": list(set(e["camera"] for e in all_events)),
            "tracker_version": "v1_time_based",
            "message": f"Detecté {total_people} persona(s) en {len(sessions)} visita(s) distinta(s)."
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


async def tool_is_open_hours(user_id: str, timestamp: float = None) -> dict:
    """Consulta si el negocio está abierto según schedule.json."""
    try:
        from datetime import datetime
        schedule_file = STORAGE_ROOT / "users" / user_id / "business" / "schedule.json"
        if not schedule_file.exists():
            return {"success": True, "is_open": False, "message": "No hay horario registrado."}
        
        sched = json.loads(schedule_file.read_text())
        ts = timestamp or datetime.now().timestamp()
        now = datetime.fromtimestamp(ts)
        weekday = now.strftime("%a")
        current_hour = now.strftime("%H:%M")
        date_str = now.strftime("%Y-%m-%d")
        
        holidays = sched.get("holidays", []) + ["2026-01-01", "2026-12-25"]
        if date_str in holidays:
            return {"success": True, "is_open": False, "message": "Hoy es festivo."}
        
        hours = sched.get("schedule", {}).get(weekday, "08:00–18:00")
        start, end = hours.split("–")
        start, end = start.strip(), end.strip()
        
        is_open = start <= current_hour < end if start <= end else (current_hour >= start or current_hour < end)
        
        return {
            "success": True,
            "is_open": is_open,
            "weekday": weekday,
            "current_hour": current_hour,
            "business_hours": hours,
            "message": f"El negocio está {'abierto' if is_open else 'cerrado'}. Horario hoy: {hours}."
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


TOOLS_REGISTRY = {
    "save_business_data": {
        "function": tool_save_business_data,
        "description": "Guarda un dato del negocio. Campos: business_name, business_type, owner_name, concern, schedule_open, schedule_close",
        "parameters": {"type": "object", "properties": {
            "field": {"type": "string"}, "value": {"type": "string"}
        }, "required": ["field", "value"]},
    },
    "save_camera_config": {
        "function": tool_save_camera_config,
        "description": "Guarda configuración de cámara",
        "parameters": {"type": "object", "properties": {
            "camera_id": {"type": "string"}, "zone": {"type": "string"},
        }, "required": ["camera_id"]},
    },
    "get_vigilance_config": {
        "function": tool_get_vigilance_config,
        "description": "Obtiene configuración de protección de una cámara: prompt, modo, horario, comportamientos y sensibilidad",
        "parameters": {"type": "object", "properties": {
            "camera_id": {"type": "string"},
        }},
    },
    "update_vigilance_config": {
        "function": tool_update_vigilance_config,
        "description": "Actualiza configuración de protección y regenera el prompt. Usa protección estructurada, no reglas sueltas.",
        "parameters": {"type": "object", "properties": {
            "camera_id": {"type": "string"},
            "vigilance": {"type": "object"},
            "schedule": {"type": "object"},
            "mode": {"type": "string", "description": "normal o sentinel"},
            "system_prompt": {"type": "string"},
        }},
    },
    "get_latest_frame": {
        "function": tool_get_latest_frame,
        "description": "Obtiene imagen más reciente de una cámara",
        "parameters": {"type": "object", "properties": {
            "camera_id": {"type": "string"},
        }},
    },
    "analyze_frame": {
        "function": tool_analyze_frame,
        "description": "Analiza una imagen con Eva",
        "parameters": {"type": "object", "properties": {
            "camera_id": {"type": "string"}, "prompt": {"type": "string"},
        }},
    },
    "search_events": {
        "function": tool_search_events,
        "description": "Busca eventos en el diario. Filtros: query, person_class=hombre|mujer|nino|anciano, clothing, min/max_persons, activity, importance, date, camera_id.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "Texto a buscar (vacío = todos)"},
            "date": {"type": "string", "description": "today, yesterday, o YYYY-MM-DD"},
            "camera_id": {"type": "string"},
            "limit": {"type": "integer"},
        }},
    },
    "get_activity_summary": {
        "function": tool_get_activity_summary,
        "description": "Resume la actividad de un día. '¿Cómo estuvo el día?' / '¿Cuántos eventos hubieron?'",
        "parameters": {"type": "object", "properties": {
            "date": {"type": "string", "description": "today, yesterday, o YYYY-MM-DD"},
            "camera_id": {"type": "string"},
        }},
    },
    "event_book": {
        "function": tool_event_book,
        "description": "Indice cronologico navegable y agrupable del libro. 'Que paso en la camara caja entre 10 y 14?' / 'Resumeme hoy por hora'.",
        "parameters": {"type": "object", "properties": {
            "date": {"type": "string", "description": "today, yesterday, o YYYY-MM-DD"},
            "camera_id": {"type": "string", "description": "Restringir a una camara"},
            "group_by": {"type": "string", "description": "hour | camera | camera_hour | ten_minute"},
            "only_importance": {"type": "string", "description": "normal|baja|media|alta|critica", "default": ""},
            "max_entries": {"type": "integer", "default": 40}
        }}
    },
    "find_anomalies": {
        "function": tool_find_anomalies,
        "description": "Busca actividad sospechosa por severidad. '¿Hubo alertas?' / '¿Viste algo raro?'",
        "parameters": {"type": "object", "properties": {
            "min_severity": {"type": "string", "description": "baja, media, alta, critica"},
            "date": {"type": "string"},
            "camera_id": {"type": "string"},
            "limit": {"type": "integer"},
        }},
    },
    "latest_events": {
        "function": tool_latest_events,
        "description": "Lista los últimos análisis guardados en el diario de eventos.",
        "parameters": {"type": "object", "properties": {
            "limit": {"type": "integer"},
            "date": {"type": "string"},
            "camera_id": {"type": "string"},
        }},
    },
    "find_risks": {
        "function": tool_find_risks,
        "description": "Busca riesgos de incendio, humo o alertas críticas en el diario.",
        "parameters": {"type": "object", "properties": {
            "date": {"type": "string"},
            "camera_id": {"type": "string"},
            "limit": {"type": "integer"},
        }},
    },
    "identify_face": {
        "function": tool_identify_face,
        "description": "Identifica quién aparece en el frame actual usando face recognition. '¿Quién está en cámara?' / '¿Es el cajero?'",
        "parameters": {"type": "object", "properties": {
            "camera_id": {"type": "string"},
        }},
    },
    "list_employees": {
        "function": tool_list_employees,
        "description": "Lista los empleados registrados con faceid. '¿Cuántos empleados hay?' / '¿Quién está registrado?'",
        "parameters": {"type": "object", "properties": {}},
    },
    "count_people": {
        "function": tool_count_people,
        "description": "Cuenta personas únicas detectadas por cámara. '¿Cuántas personas han venido hoy?' / '¿Cuánta gente?'",
        "parameters": {"type": "object", "properties": {
            "camera_id": {"type": "string", "description": "ID de cámara específica u omitir para todas"},
            "date": {"type": "string", "description": "today, yesterday"},
            "start": {"type": "number"},
            "end": {"type": "number"}
        }},
    },
    "is_open_hours": {
        "function": tool_is_open_hours,
        "description": "Consulta si el negocio está abierto según horario registrado. '¿Estamos abiertos?' / '¿Horario?'",
        "parameters": {"type": "object", "properties": {
            "timestamp": {"type": "number"}
        }},
    },
    "save_event": {
        "function": tool_save_event,
        "description": "Guarda un evento de seguridad en el diario.",
        "parameters": {"type": "object", "properties": {
            "camera_id": {"type": "string"},
            "summary": {"type": "string"},
            "importance": {"type": "string"}
        }},
    },
    "respond_directly": {
        "function": tool_respond_directly,
        "description": "Responde directamente sin consultar herramientas.",
        "parameters": {"type": "object", "properties": {
            "message": {"type": "string"}
        }, "required": ["message"]},
    },
    "learn_from_feedback": {
        "function": tool_learn_from_feedback,
        "description": "Registra feedback del usuario sobre un evento.",
        "parameters": {"type": "object", "properties": {
            "event_id": {"type": "string"},
            "is_real": {"type": "boolean"},
            "notes": {"type": "string"}
        }, "required": ["event_id", "is_real"]},
    },
}


OPENAI_TOOLS_SCHEMA = [
    {
        "type": "function",
        "function": {
            "name": "search_events",
            "description": "Busca eventos en el diario de seguridad. Busca por texto, fecha o cámara. Usa query vacío para listar todos los eventos del día.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Texto a buscar en eventos (vacío = todos)"},
                    "date": {"type": "string", "description": "today, yesterday, reciente, o YYYY-MM-DD"},
                    "camera_id": {"type": "string", "description": "ID de cámara específica (vacío = todas)"},
                    "limit": {"type": "integer", "description": "Máximo de resultados (1-10)"}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_activity_summary",
            "description": "Resume la actividad de un día: total de eventos, alertas, personas detectadas y último análisis.",
            "parameters": {
                "type": "object",
                "properties": {
                    "date": {"type": "string", "description": "today, yesterday, reciente, o YYYY-MM-DD"},
                    "camera_id": {"type": "string", "description": "ID de cámara específica (vacío = todas)"}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "find_anomalies",
            "description": "Busca actividad sospechosa, alertas o violaciones de seguridad por severidad mínima.",
            "parameters": {
                "type": "object",
                "properties": {
                    "min_severity": {"type": "string", "description": "baja, media, alta, critica"},
                    "date": {"type": "string", "description": "today, yesterday, reciente, o YYYY-MM-DD"},
                    "camera_id": {"type": "string", "description": "ID de cámara específica (vacío = todas)"},
                    "limit": {"type": "integer", "description": "Máximo de resultados (1-10)"}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "latest_events",
            "description": "Lista los últimos análisis guardados en el diario, ordenados por fecha descendente.",
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "description": "Cantidad de eventos (1-10)"},
                    "date": {"type": "string", "description": "today, yesterday, reciente, o YYYY-MM-DD"},
                    "camera_id": {"type": "string", "description": "ID de cámara específica (vacío = todas)"}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "find_risks",
            "description": "Busca riesgos de incendio, humo, fuego o alertas críticas en el diario.",
            "parameters": {
                "type": "object",
                "properties": {
                    "date": {"type": "string", "description": "today, yesterday, reciente, o YYYY-MM-DD"},
                    "camera_id": {"type": "string", "description": "ID de cámara específica (vacío = todas)"},
                    "limit": {"type": "integer", "description": "Máximo de resultados (1-10)"}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_vigilance_config",
            "description": "Lee la configuración actual de protección de una cámara: modo, sensibilidad, horarios, comportamientos.",
            "parameters": {
                "type": "object",
                "properties": {
                    "camera_id": {"type": "string", "description": "ID de cámara"}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "update_vigilance_config",
            "description": "Actualiza la configuración de protección: activar/desactivar centinela, sensibilidad, horario, alertar si, no alertar por.",
            "parameters": {
                "type": "object",
                "properties": {
                    "camera_id": {"type": "string", "description": "ID de cámara"},
                    "mode": {"type": "string", "description": "normal o sentinel"},
                    "schedule": {"type": "object", "description": "Horario: {open: 'HH:MM', close: 'HH:MM'}"},
                    "sensitivity": {"type": "string", "description": "sensibilidad: baja, media, alta, critica"},
                    "alert_behaviors": {"type": "array", "items": {"type": "string"}, "description": "Comportamientos que deben generar alerta"},
                    "ignore_behaviors": {"type": "array", "items": {"type": "string"}, "description": "Comportamientos que NO deben generar alerta"}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_latest_frame",
            "description": "Obtiene la imagen más reciente de una cámara. Útil para ver qué está pasando ahora.",
            "parameters": {
                "type": "object",
                "properties": {
                    "camera_id": {"type": "string", "description": "ID de cámara"}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "analyze_frame",
            "description": "Analiza el último frame con una pregunta específica sobre la escena.",
            "parameters": {
                "type": "object",
                "properties": {
                    "camera_id": {"type": "string", "description": "ID de cámara"},
                    "prompt": {"type": "string", "description": "Pregunta sobre la imagen"}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "identify_face",
            "description": "Identifica quién aparece en el frame actual usando reconocimiento facial.",
            "parameters": {
                "type": "object",
                "properties": {
                    "camera_id": {"type": "string", "description": "ID de cámara"}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_employees",
            "description": "Lista los empleados registrados con faceid.",
            "parameters": {
                "type": "object",
                "properties": {}
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "save_event",
            "description": "Guarda un evento de seguridad en el diario. Usa esta función cuando detectes algo importante que el usuario debe saber.",
            "parameters": {
                "type": "object",
                "properties": {
                    "camera_id": {"type": "string", "description": "ID de cámara"},
                    "summary": {"type": "string", "description": "Descripción corta del evento"},
                    "importance": {"type": "string", "description": "baja, media, alta, critica"}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "respond_directly",
            "description": "Responde directamente al usuario sin consultar herramientas. Usa esto para saludos, conversación general, o cuando no necesitas consultar el diario.",
            "parameters": {
                "type": "object",
                "properties": {
                    "message": {"type": "string", "description": "Tu respuesta directa al usuario"}
                },
                "required": ["message"]
             }
         }
     }
 ]


def resolve_user_events_dirs(user_id: str, camera_id: str = None) -> list:
    """Resuelve los directorios de eventos para un usuario/cámara."""
    base = STORAGE_ROOT / "users" / user_id / "cameras"
    if camera_id:
        events_dir = base / camera_id / "events"
        return [events_dir] if events_dir.exists() else []
    dirs = []
    if base.exists():
        for cam_dir in base.iterdir():
            if cam_dir.is_dir():
                events_dir = cam_dir / "events"
                if events_dir.exists():
                    dirs.append(events_dir)
    return dirs
