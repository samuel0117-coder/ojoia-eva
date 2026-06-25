"""
eva/tools.py — Herramientas que Eva puede invocar durante el chat.

Cada función es una "tool" que el LLM puede llamar cuando el usuario
hace una pregunta o pide una acción. Eva decide qué tool usar basándose
en el mensaje del usuario y el contexto del negocio.

Todas las tools leen/escriben en:
  - /storage/users/{uid}/business.json  (RAG principal)
  - /storage/users/{uid}/cameras/{cid}/events/{eid}.json  (eventos)
"""

import json
import os
import time
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Dict, List, Any

logger = logging.getLogger(__name__)

STORAGE_ROOT = Path("/home/sam/storage")


# ═══════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════

def load_business_json(user_id: str) -> dict:
    """Cargar business.json del usuario. Si no existe, crearlo desde user.json."""
    bp = STORAGE_ROOT / "users" / user_id / "business.json"
    if bp.exists():
        with open(bp) as f:
            return json.load(f)
    # Migrar desde user.json
    up = STORAGE_ROOT / "users" / user_id / "user.json"
    if up.exists():
        with open(up) as f:
            ud = json.load(f)
        return migrate_user_to_business(ud)
    return {}


def save_business_json(user_id: str, data: dict):
    """Guardar business.json atómicamente."""
    bp = STORAGE_ROOT / "users" / user_id / "business.json"
    bp.parent.mkdir(parents=True, exist_ok=True)
    tmp = bp.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    tmp.replace(bp)


def migrate_user_to_business(user_data: dict) -> dict:
    """Migrar user.json existente al nuevo formato business.json."""
    uid = user_data.get("user_id", "")
    cameras = {}
    for cam in user_data.get("cameras", []):
        cid = cam.get("camera_id", "")
        if not cid:
            continue
        cameras[cid] = {
            "name": cam.get("name", cid),
            "zone": cam.get("zone", ""),
            "active": cam.get("active", False),
            "rules": cam.get("rules", []),
            "rules_es": cam.get("rules_es", []),
            "system_prompt": cam.get("system_prompt", ""),
            "last_frame_ts": cam.get("last_frame", 0),
            "today_summary": {
                "date": datetime.now().strftime("%Y-%m-%d"),
                "total_events": 0,
                "total_persons": 0,
                "alerts": 0,
                "peak_hour": None,
                "peak_persons": 0,
                "qwen_descriptions": []
            }
        }

    return {
        "user_id": uid,
        "business_name": user_data.get("business_name", ""),
        "business_type": user_data.get("business_type", ""),
        "owner": {
            "name": user_data.get("name", ""),
            "phone": user_data.get("phone", ""),
            "email": user_data.get("email", "")
        },
        "schedule": user_data.get("schedule", {"open": "07:00", "close": "19:00"}),
        "main_concerns": user_data.get("main_concerns", []),
        "cameras": cameras,
        "people": {"known": [], "suspicious": []},
        "conversation_context": {
            "weaknesses": [],
            "agreed_rules": user_data.get("rules_es", []),
            "last_chat_summary": ""
        },
        "daily_summaries": {}
    }


def resolve_user_events_dirs(user_id: str) -> List[tuple]:
    """Devolver carpetas de eventos del usuario."""
    base = STORAGE_ROOT / "users" / user_id
    dirs = []
    cameras_dir = base / "cameras"
    if cameras_dir.exists():
        for cam_id in cameras_dir.iterdir():
            events = cam_id / "events"
            if events.is_dir():
                dirs.append((cam_id.name, events))
    legacy = base / "events"
    if legacy.is_dir():
        dirs.append(("_global", legacy))
    return dirs


def parse_date_range(date_str: str) -> tuple:
    """Convierte 'today', 'yesterday', 'this_week' a (start_ts, end_ts)."""
    now = datetime.now()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    today_end = now.replace(hour=23, minute=59, second=59, microsecond=0)

    if date_str == "today":
        return (int(today_start.timestamp()), int(today_end.timestamp()))
    elif date_str == "yesterday":
        yest = today_start - timedelta(days=1)
        yest_end = today_start - timedelta(seconds=1)
        return (int(yest.timestamp()), int(yest_end.timestamp()))
    elif date_str == "this_week":
        week_start = today_start - timedelta(days=now.weekday())
        return (int(week_start.timestamp()), int(today_end.timestamp()))
    elif date_str == "last_week":
        week_start = today_start - timedelta(days=now.weekday() + 7)
        week_end = week_start + timedelta(days=6, hours=23, minutes=59, seconds=59)
        return (int(week_start.timestamp()), int(week_end.timestamp()))
    else:
        try:
            dt = datetime.strptime(date_str, "%Y-%m-%d")
            end = dt.replace(hour=23, minute=59, second=59)
            return (int(dt.timestamp()), int(end.timestamp()))
        except ValueError:
            return (0, int(time.time()))


# ═══════════════════════════════════════════════════════════════
# TOOL 1: search_events — Buscar eventos por descripción/hora/cámara
# ═══════════════════════════════════════════════════════════════

async def tool_search_events(
    user_id: str,
    query: str = "",
    camera_id: str = None,
    date: str = None,
    event_type: str = None,
    limit: int = 10
) -> dict:
    """
    Busca eventos por descripción, persona, hora o cámara.
    
    Args:
        query: Búsqueda libre — "persona con gorra negra", "Pedro", "alertas"
        camera_id: ID de cámara o None para todas
        date: "today", "yesterday", "this_week", "2026-06-05"
        event_type: "violation", "normal", "night_alert", None=todos
        limit: Máximo de resultados
    
    Returns:
        {found: int, events: [{event_id, datetime, camera, description, persons, frame_url}]}
    """
    business = load_business_json(user_id)
    date_range = parse_date_range(date) if date else (0, int(time.time()))
    results = []
    query_lower = query.lower() if query else ""
    query_terms = [t for t in query_lower.split() if len(t) > 2] if query_lower else []

    cameras_to_search = [camera_id] if camera_id else list(business.get("cameras", {}).keys())

    for cam_id in cameras_to_search:
        if cam_id == "_global":
            continue
        events_dir = STORAGE_ROOT / "users" / user_id / "cameras" / cam_id / "events"
        if not events_dir.exists():
            continue

        cam_name = business.get("cameras", {}).get(cam_id, {}).get("name", cam_id)

        for fname in sorted(os.listdir(events_dir), reverse=True):
            if not fname.endswith(".json"):
                continue
            if len(results) >= limit * 3:  # Leemos más para filtrar
                break

            try:
                with open(events_dir / fname) as f:
                    event = json.load(f)
            except Exception:
                continue

            # Filtrar por fecha
            ts = event.get("timestamp", 0)
            if ts < date_range[0] or ts > date_range[1]:
                continue

            # Filtrar por tipo
            if event_type and event.get("event_type") != event_type:
                continue

            # Buscar coincidencia en descripción (compatible con formato viejo y nuevo)
            qwen = event.get("qwen_analysis", {})
            searchable_parts = [
                qwen.get("description", ""),
                " ".join([p.get("clothing", "") + " " + p.get("action", "")
                         for p in qwen.get("persons_details", [])]),
                event.get("description", ""),
                " ".join(event.get("yolo_classes", [])),
            ]
            # También buscar en el event_id para vigilance_
            eid = event.get("event_id", "")
            if eid.startswith("vigilance_"):
                searchable_parts.append("vigilance alerta centinela persona")
            searchable = " ".join(searchable_parts).lower()

            if query_terms:
                matches = sum(1 for t in query_terms if t in searchable)
            else:
                matches = 1  # Sin query = incluir todos
            if matches > 0:
                dt = datetime.fromtimestamp(ts)
                results.append({
                    "event_id": event.get("event_id", ""),
                    "datetime": event.get("datetime", dt.strftime("%Y-%m-%d %H:%M")),
                    "hour": event.get("hour", dt.strftime("%H:%M")),
                    "date": event.get("date", dt.strftime("%Y-%m-%d")),
                    "camera_id": cam_id,
                    "camera_name": cam_name,
                    "description": qwen.get("description", event.get("description", "")),
                    "persons": qwen.get("persons", event.get("yolo", {}).get("count", event.get("yolo_count", 0))),
                    "persons_details": qwen.get("persons_details", []),
                    "activity_level": qwen.get("activity_level", ""),
                    "anomaly": qwen.get("anomaly", False),
                    "event_type": event.get("event_type", "normal"),
                    "frame_url": f"/api/event-frame/{event.get('event_id', '')}?user_id={user_id}",
                    "relevance": matches / len(query_terms) if query_terms else 0
                })

    # Ordenar por relevancia
    results.sort(key=lambda x: x["relevance"], reverse=True)
    results = results[:limit]

    return {
        "found": len(results),
        "query": query,
        "events": results
    }


# ═══════════════════════════════════════════════════════════════
# TOOL 2: find_person — Buscar persona por descripción visual
# ═══════════════════════════════════════════════════════════════

async def tool_find_person(
    user_id: str,
    description: str,
    date: str = None,
    camera_id: str = None,
    limit: int = 10
) -> dict:
    """
    Busca una persona por descripción visual en los eventos.
    
    Args:
        description: "camisa blanca, gorra negra, delantal azul"
        date: "today", "yesterday", "this_week"
        camera_id: Cámara específica o None
    
    Returns:
        {found: int, person_matches: [{datetime, camera, description, frame_url}]}
    """
    # Reutilizar search_events pero enfocado en persons_details
    result = await tool_search_events(
        user_id=user_id,
        query=description,
        camera_id=camera_id,
        date=date,
        limit=limit
    )

    # Filtrar solo eventos donde se detectaron personas con esa descripción
    person_matches = []
    for evt in result.get("events", []):
        persons_details = evt.get("persons_details", [])
        desc_lower = description.lower()
        for person in persons_details:
            person_text = f"{person.get('clothing', '')} {person.get('action', '')} {person.get('role', '')}".lower()
            if any(term in person_text for term in desc_lower.split()):
                person_matches.append({
                    "datetime": evt["datetime"],
                    "hour": evt["hour"],
                    "camera_name": evt["camera_name"],
                    "person_description": f"{person.get('role', 'persona')} — {person.get('action', '')} — {person.get('clothing', '')}",
                    "all_persons": evt["persons"],
                    "frame_url": evt["frame_url"],
                    "event_type": evt["event_type"]
                })
                break

    # También buscar en people del business.json
    business = load_business_json(user_id)
    known_matches = []
    for person in business.get("people", {}).get("known", []):
        person_text = " ".join(person.get("visual_tags", []) + [person.get("name", "")] + [person.get("role", "")]).lower()
        if any(term in person_text for term in description.lower().split()):
            known_matches.append({
                "name": person.get("name", ""),
                "role": person.get("role", ""),
                "visual_tags": person.get("visual_tags", []),
                "patterns": person.get("patterns", {})
            })

    for person in business.get("people", {}).get("suspicious", []):
        person_text = " ".join(person.get("visual_tags", [])).lower()
        if any(term in person_text for term in description.lower().split()):
            known_matches.append({
                "name": person.get("id", "Desconocido"),
                "role": "sospechoso",
                "visual_tags": person.get("visual_tags", []),
                "incidents": person.get("incidents", 0),
                "notes": person.get("notes", "")
            })

    return {
        "found": len(person_matches),
        "description_searched": description,
        "event_matches": person_matches[:limit],
        "known_people_matches": known_matches
    }


# ═══════════════════════════════════════════════════════════════
# TOOL 3: get_daily_summary — Resumen de un día
# ═══════════════════════════════════════════════════════════════

async def tool_get_daily_summary(
    user_id: str,
    date: str = "today"
) -> dict:
    """
    Obtiene el resumen de un día específico.
    
    Args:
        date: "today", "yesterday", "2026-06-05"
    
    Returns:
        {date, total_events, total_persons, alerts, peak_hour, highlights}
    """
    business = load_business_json(user_id)

    # Determinar la fecha
    if date == "today":
        date_key = datetime.now().strftime("%Y-%m-%d")
    elif date == "yesterday":
        date_key = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    else:
        date_key = date

    # Buscar en daily_summaries guardados
    daily = business.get("daily_summaries", {}).get(date_key, {})

    # Si no hay resumen guardado, generarlo desde los eventos
    if not daily:
        date_range = parse_date_range(date_key)
        total_events = 0
        total_persons = 0
        alerts = 0
        peak_hour = None
        peak_persons = 0
        highlights = []
        cameras_data = {}

        for cam_id, cam_dir in resolve_user_events_dirs(user_id):
            if cam_id == "_global":
                continue
            cam_name = business.get("cameras", {}).get(cam_id, {}).get("name", cam_id)
            cam_events = []

            for fname in os.listdir(cam_dir):
                if not fname.endswith(".json"):
                    continue
                try:
                    with open(cam_dir / fname) as f:
                        event = json.load(f)
                except Exception:
                    continue

                ts = event.get("timestamp", 0)
                if ts < date_range[0] or ts > date_range[1]:
                    continue

                total_events += 1
                qwen = event.get("qwen_analysis", {})
                persons = qwen.get("persons", event.get("yolo", {}).get("count", 0))
                total_persons += persons

                if event.get("event_type") in ("violation", "night_alert"):
                    alerts += 1

                hour = datetime.fromtimestamp(ts).strftime("%H:%M")
                if persons > peak_persons:
                    peak_persons = persons
                    peak_hour = hour

                desc = qwen.get("description", event.get("description", ""))
            # Mejorar descripción de eventos de vigilancia
            if desc.startswith("Modo centinela:") and event.get("yolo_classes"):
                persons = sum(1 for c in event["yolo_classes"] if c == "person")
                objects = [c for c in event["yolo_classes"] if c != "person"]
                if persons > 0:
                    obj_str = f" + {', '.join(objects)}" if objects else ""
                    desc = f"Persona detectada{obj_str}"
                elif objects:
                    desc = f"Objetos: {', '.join(objects)}"
                if desc:
                    cam_events.append(f"{hour} — {desc}")

            if cam_events:
                cameras_data[cam_name] = cam_events

        daily = {
            "date": date_key,
            "total_events": total_events,
            "total_persons": total_persons,
            "alerts": alerts,
            "peak_hour": peak_hour,
            "peak_persons": peak_persons,
            "cameras_data": cameras_data,
            "generated_at": datetime.now().isoformat()
        }

    return daily


# ═══════════════════════════════════════════════════════════════
# TOOL 4: get_traffic_analysis — Análisis de tráfico de personas
# ═══════════════════════════════════════════════════════════════

async def tool_get_traffic_analysis(
    user_id: str,
    days: int = 7,
    camera_id: str = None,
    group_by: str = "hour"
) -> dict:
    """
    Analiza patrones de tráfico de personas.
    
    Args:
        days: Últimos N días
        camera_id: Cámara específica o None para todas
        group_by: "hour", "day", "weekday"
    
    Returns:
        {peak_hour, peak_persons, daily_avg, trend, hourly_breakdown}
    """
    business = load_business_json(user_id)
    date_range = (int((datetime.now() - timedelta(days=days)).timestamp()), int(time.time()))

    hourly_counts = {}
    daily_counts = {}
    total_persons = 0
    total_events = 0

    cameras_to_search = [camera_id] if camera_id else list(business.get("cameras", {}).keys())

    for cam_id in cameras_to_search:
        if cam_id == "_global":
            continue
        events_dir = STORAGE_ROOT / "users" / user_id / "cameras" / cam_id / "events"
        if not events_dir.exists():
            continue

        for fname in os.listdir(events_dir):
            if not fname.endswith(".json"):
                continue
            try:
                with open(events_dir / fname) as f:
                    event = json.load(f)
            except Exception:
                continue

            ts = event.get("timestamp", 0)
            if ts < date_range[0] or ts > date_range[1]:
                continue

            total_events += 1
            qwen = event.get("qwen_analysis", {})
            persons = qwen.get("persons", event.get("yolo", {}).get("count", event.get("yolo_count", 0)))
            # Para eventos de vigilancia, contar personas de yolo_classes
            if persons == 0 and event.get("event_type") == "vigilance_alert":
                yolo_classes = event.get("yolo_classes", [])
                persons = sum(1 for c in yolo_classes if c == "person")
            total_persons += persons

            dt = datetime.fromtimestamp(ts)
            hour_key = dt.strftime("%H:00")
            day_key = dt.strftime("%Y-%m-%d")
            weekday_key = dt.strftime("%A")

            hourly_counts[hour_key] = hourly_counts.get(hour_key, 0) + persons
            daily_counts[day_key] = daily_counts.get(day_key, 0) + persons

    # Encontrar pico
    peak_hour = max(hourly_counts, key=hourly_counts.get) if hourly_counts else None
    peak_persons = hourly_counts.get(peak_hour, 0) if peak_hour else 0

    # Promedio diario
    daily_avg = round(total_persons / max(len(daily_counts), 1))

    # Tendencia (comparar primera mitad vs segunda mitad)
    sorted_days = sorted(daily_counts.keys())
    if len(sorted_days) >= 2:
        mid = len(sorted_days) // 2
        first_half = sum(daily_counts[d] for d in sorted_days[:mid]) / max(mid, 1)
        second_half = sum(daily_counts[d] for d in sorted_days[mid:]) / max(len(sorted_days) - mid, 1)
        if second_half > first_half * 1.1:
            trend = "subiendo"
        elif second_half < first_half * 0.9:
            trend = "bajando"
        else:
            trend = "estable"
    else:
        trend = "sin datos suficientes"

    return {
        "period_days": days,
        "total_persons": total_persons,
        "total_events": total_events,
        "daily_avg": daily_avg,
        "peak_hour": peak_hour,
        "peak_persons": peak_persons,
        "trend": trend,
        "hourly_breakdown": dict(sorted(hourly_counts.items())),
        "daily_breakdown": dict(sorted(daily_counts.items()))
    }


# ═══════════════════════════════════════════════════════════════
# TOOL 5: get_business_summary — Resumen ejecutivo del negocio
# ═══════════════════════════════════════════════════════════════

async def tool_get_business_summary(
    user_id: str,
    period: str = "today"
) -> dict:
    """
    Genera un resumen ejecutivo del negocio.
    
    Args:
        period: "today", "yesterday", "week"
    
    Returns:
        {period, cameras_status, events_summary, alerts, suggestions}
    """
    business = load_business_json(user_id)
    now = time.time()

    # Estado de cámaras
    cameras_status = []
    for cam_id, cam in business.get("cameras", {}).items():
        last_frame = cam.get("last_frame_ts", 0)
        is_online = (now - last_frame) < 120 if last_frame else False
        cameras_status.append({
            "id": cam_id,
            "name": cam.get("name", cam_id),
            "online": is_online,
            "last_frame_ago": int(now - last_frame) if last_frame else None
        })

    # Resumen de eventos del período
    if period == "week":
        date_range = (int((datetime.now() - timedelta(days=7)).timestamp()), now)
    elif period == "yesterday":
        yest = datetime.now() - timedelta(days=1)
        start = int(yest.replace(hour=0, minute=0, second=0).timestamp())
        end = int(yest.replace(hour=23, minute=59, second=59).timestamp())
        date_range = (start, end)
    else:
        start = int(datetime.now().replace(hour=0, minute=0, second=0).timestamp())
        date_range = (start, now)

    total_events = 0
    total_persons = 0
    alerts = 0
    false_alarms = 0

    for cam_id, cam_dir in resolve_user_events_dirs(user_id):
        if cam_id == "_global":
            continue
        for fname in os.listdir(cam_dir):
            if not fname.endswith(".json"):
                continue
            try:
                with open(cam_dir / fname) as f:
                    event = json.load(f)
            except Exception:
                continue
            ts = event.get("timestamp", 0)
            if ts < date_range[0] or ts > date_range[1]:
                continue
            total_events += 1
            qwen = event.get("qwen_analysis", {})
            total_persons += qwen.get("persons", 0)
            if event.get("event_type") in ("violation", "night_alert"):
                alerts += 1

    online_cams = sum(1 for c in cameras_status if c["online"])

    return {
        "period": period,
        "business_name": business.get("business_name", ""),
        "cameras": {
            "total": len(cameras_status),
            "online": online_cams,
            "offline": len(cameras_status) - online_cams,
            "details": cameras_status
        },
        "events": {
            "total": total_events,
            "total_persons": total_persons,
            "alerts": alerts,
            "false_alarms": false_alarms
        },
        "main_concerns": business.get("main_concerns", []),
        "generated_at": datetime.now().isoformat()
    }


# ═══════════════════════════════════════════════════════════════
# TOOL 6: get_camera_frames — Frames recientes de una cámara
# ═══════════════════════════════════════════════════════════════

async def tool_get_camera_frames(
    user_id: str,
    camera_id: str,
    count: int = 5,
    time_range: str = "last_hour"
) -> dict:
    """
    Obtiene frames recientes de una cámara con análisis de Qwen.
    
    Args:
        camera_id: ID de la cámara
        count: Cantidad de frames (default: 5)
        time_range: "last_hour", "today", "yesterday"
    
    Returns:
        {camera_name, frames: [{event_id, datetime, description, persons, frame_url}]}
    """
    business = load_business_json(user_id)
    cam = business.get("cameras", {}).get(camera_id, {})
    cam_name = cam.get("name", camera_id)

    if time_range == "today":
        date_range = parse_date_range("today")
    elif time_range == "yesterday":
        date_range = parse_date_range("yesterday")
    else:
        date_range = (int(time.time()) - 3600, int(time.time()))

    events_dir = STORAGE_ROOT / "users" / user_id / "cameras" / camera_id / "events"
    if not events_dir.exists():
        return {"camera_name": cam_name, "frames": [], "error": "Cámara sin eventos"}

    frames = []
    for fname in sorted(os.listdir(events_dir), reverse=True):
        if not fname.endswith(".json"):
            continue
        if len(frames) >= count:
            break
        try:
            with open(events_dir / fname) as f:
                event = json.load(f)
        except Exception:
            continue

        ts = event.get("timestamp", 0)
        if ts < date_range[0] or ts > date_range[1]:
            continue

        qwen = event.get("qwen_analysis", {})
        dt = datetime.fromtimestamp(ts)
        frames.append({
            "event_id": event.get("event_id", ""),
            "datetime": dt.strftime("%Y-%m-%d %H:%M"),
            "hour": dt.strftime("%H:%M"),
            "description": qwen.get("description", event.get("description", "")),
            "persons": qwen.get("persons", event.get("yolo", {}).get("count", 0)),
            "activity_level": qwen.get("activity_level", ""),
            "anomaly": qwen.get("anomaly", False),
            "frame_url": f"/api/event-frame/{event.get('event_id', '')}?user_id={user_id}"
        })

    return {
        "camera_name": cam_name,
        "camera_id": camera_id,
        "frames": frames,
        "count": len(frames)
    }


# ═══════════════════════════════════════════════════════════════
# TOOL 7: update_business_context — Actualizar datos del negocio
# ═══════════════════════════════════════════════════════════════

async def tool_update_business_context(
    user_id: str,
    field: str,
    value: str
) -> dict:
    """
    Actualiza el contexto del negocio con nueva información.
    
    Args:
        field: "schedule_open", "schedule_close", "concerns", "employee_count", "note"
        value: Nuevo valor
    
    Returns:
        {success, field, new_value}
    """
    business = load_business_json(user_id)

    if field == "schedule_open":
        business.setdefault("schedule", {})["open"] = value
    elif field == "schedule_close":
        business.setdefault("schedule", {})["close"] = value
    elif field == "concerns":
        concerns = [c.strip() for c in value.split(",") if c.strip()]
        business["main_concerns"] = concerns
    elif field == "employee_count":
        business["employee_count"] = value
    elif field == "note":
        ctx = business.setdefault("conversation_context", {})
        notes = ctx.get("notes", [])
        notes.append({
            "text": value,
            "timestamp": datetime.now().isoformat()
        })
        ctx["notes"] = notes
    elif field == "add_person":
        people = business.setdefault("people", {}).setdefault("known", [])
        people.append({"name": value, "visual_tags": [], "role": "empleado"})
    elif field == "add_suspicious":
        people = business.setdefault("people", {}).setdefault("suspicious", [])
        people.append({"id": f"susp_{len(people)+1:03d}", "visual_tags": [value], "incidents": 0})
    else:
        return {"success": False, "error": f"Campo desconocido: {field}"}

    save_business_json(user_id, business)
    return {"success": True, "field": field, "value": value}


# ═══════════════════════════════════════════════════════════════
# TOOL 8: learn_from_feedback — Aprender del feedback del usuario
# ═══════════════════════════════════════════════════════════════

async def tool_learn_from_feedback(
    user_id: str,
    event_id: str,
    is_real: bool,
    notes: str = None
) -> dict:
    """
    Procesa feedback del usuario sobre alertas.
    
    Args:
        event_id: ID del evento
        is_real: True = alerta real, False = falsa alarma
        notes: Notas adicionales del usuario
    
    Returns:
        {success, action_taken}
    """
    # Buscar el evento
    for cam_id, events_dir in resolve_user_events_dirs(user_id):
        if cam_id == "_global":
            continue
        ef = events_dir / f"{event_id}.json"
        if ef.exists():
            with open(ef) as f:
                event = json.load(f)

            if not is_real:
                # Marcar como falsa alarma
                event["feedback"] = {
                    "is_false_alarm": True,
                    "notes": notes,
                    "timestamp": int(time.time())
                }
                with open(ef, "w") as f:
                    json.dump(event, f, indent=2)

                # Actualizar métricas de la cámara
                cam_file = STORAGE_ROOT / "users" / user_id / "cameras" / cam_id / "camera.json"
                if cam_file.exists():
                    with open(cam_file) as f:
                        cam = json.load(f)
                    metrics = cam.setdefault("metrics", {})
                    metrics["total_false_positives"] = metrics.get("total_false_positives", 0) + 1
                    with open(cam_file, "w") as f:
                        json.dump(cam, f, indent=2, ensure_ascii=False)

                return {"success": True, "action": "false_alarm_registered", "camera_id": cam_id}
            else:
                event["feedback"] = {
                    "is_confirmed": True,
                    "notes": notes,
                    "timestamp": int(time.time())
                }
                with open(ef, "w") as f:
                    json.dump(event, f, indent=2)
                return {"success": True, "action": "alert_confirmed", "camera_id": cam_id}

    return {"success": False, "error": "Evento no encontrado"}


# ═══════════════════════════════════════════════════════════════
# REGISTRY — Todas las tools disponibles para el LLM
# ═══════════════════════════════════════════════════════════════

TOOLS_REGISTRY = {
    "search_events": {
        "function": tool_search_events,
        "description": "Busca eventos por descripción, persona, hora o cámara. "
                       "Útil para: 'Busca persona con gorra negra', '¿Qué pasó ayer?', 'Alertas de la semana'",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Búsqueda: 'persona con gorra negra', 'Pedro', 'alertas'"},
                "camera_id": {"type": "string", "description": "ID de cámara o null para todas"},
                "date": {"type": "string", "description": "today, yesterday, this_week, 2026-06-05"},
                "event_type": {"type": "string", "description": "violation, normal, night_alert, null=todos"},
                "limit": {"type": "integer", "description": "Máximo resultados (default: 10)"}
            },
            "required": []
        }
    },
    "find_person": {
        "function": tool_find_person,
        "description": "Busca persona por descripción visual. "
                       "Útil para: 'Busca camisa blanca y gorra negra', '¿Viste a Pedro?'",
        "parameters": {
            "type": "object",
            "properties": {
                "description": {"type": "string", "description": "Descripción visual: 'camisa blanca, gorra negra'"},
                "date": {"type": "string", "description": "today, yesterday, this_week"},
                "camera_id": {"type": "string", "description": "Cámara específica o null"}
            },
            "required": ["description"]
        }
    },
    "get_daily_summary": {
        "function": tool_get_daily_summary,
        "description": "Resumen de un día. Útil para: '¿Cómo estuvo ayer?', 'Resumen de hoy'",
        "parameters": {
            "type": "object",
            "properties": {
                "date": {"type": "string", "description": "today, yesterday, 2026-06-05"}
            },
            "required": ["date"]
        }
    },
    "get_traffic_analysis": {
        "function": tool_get_traffic_analysis,
        "description": "Análisis de tráfico de personas. Útil para: '¿A qué hora hay más clientes?', 'Picos de tráfico'",
        "parameters": {
            "type": "object",
            "properties": {
                "days": {"type": "integer", "description": "Últimos N días (default: 7)"},
                "camera_id": {"type": "string", "description": "Cámara específica o null"},
                "group_by": {"type": "string", "description": "hour, day, weekday"}
            }
        }
    },
    "get_business_summary": {
        "function": tool_get_business_summary,
        "description": "Resumen ejecutivo del negocio. Útil para: '¿Cómo va el negocio?', 'Resumen de la semana'",
        "parameters": {
            "type": "object",
            "properties": {
                "period": {"type": "string", "description": "today, yesterday, week"}
            }
        }
    },
    "get_camera_frames": {
        "function": tool_get_camera_frames,
        "description": "Frames recientes de una cámara. Útil para: '¿Qué ves ahora?', 'Muéstrame la caja'",
        "parameters": {
            "type": "object",
            "properties": {
                "camera_id": {"type": "string", "description": "ID de la cámara"},
                "count": {"type": "integer", "description": "Cantidad de frames (default: 5)"},
                "time_range": {"type": "string", "description": "last_hour, today, yesterday"}
            },
            "required": ["camera_id"]
        }
    },
    "update_business_context": {
        "function": tool_update_business_context,
        "description": "Actualiza datos del negocio. Útil para: 'Ahora abro los domingos', 'Tengo 3 empleados'",
        "parameters": {
            "type": "object",
            "properties": {
                "field": {"type": "string", "description": "schedule_open, schedule_close, concerns, employee_count, note, add_person"},
                "value": {"type": "string", "description": "Nuevo valor"}
            },
            "required": ["field", "value"]
        }
    },
    "learn_from_feedback": {
        "function": tool_learn_from_feedback,
        "description": "Procesa feedback sobre alertas. Útil para: 'Eso fue falsa alarma', 'Sí, era real'",
        "parameters": {
            "type": "object",
            "properties": {
                "event_id": {"type": "string", "description": "ID del evento"},
                "is_real": {"type": "boolean", "description": "True=alerta real, False=falsa alarma"},
                "notes": {"type": "string", "description": "Notas adicionales"}
            },
            "required": ["event_id", "is_real"]
        }
    }
}
