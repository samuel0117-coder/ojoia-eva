import json
import time
import logging
from datetime import date, timedelta, datetime
from pathlib import Path
from collections import defaultdict

logger = logging.getLogger(__name__)

STORAGE_ROOT = Path("/home/sam/storage")


def _iter_events(user_id, camera_id=None, date_filter=None):
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
        for evt_file in sorted(events_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
            try:
                evt = json.loads(evt_file.read_text())
                if date_filter:
                    evt_date = evt.get("datetime", "")[:10]
                    evt_ts = int(evt.get("timestamp", 0) or 0)
                    if date_filter == "today" and evt_date != date.today().isoformat():
                        continue
                    if date_filter == "yesterday" and evt_date != (date.today() - timedelta(days=1)).isoformat():
                        continue
                    if date_filter not in ("today", "yesterday") and evt_date != date_filter:
                        continue
                yield evt, cam_dir.name
            except Exception:
                pass


def _event_is_alert(evt):
    if evt.get("event_type") in ("vigilance_alert", "violation", "night_alert"):
        return True
    meta = evt.get("metadata", {})
    if isinstance(meta, dict) and meta.get("after_hours"):
        return True
    qj = evt.get("qwen_json", {})
    if isinstance(qj, dict) and (qj.get("violation") or qj.get("importancia") in ("alta", "critica")):
        return True
    if evt.get("attention_hits"):
        return True
    return False


def _event_persons(evt):
    yolo = evt.get("yolo", {})
    if isinstance(yolo, dict):
        return yolo.get("count", 0) or 0
    return 0


def _event_qwen(evt):
    qj = evt.get("qwen_json", {})
    return qj if isinstance(qj, dict) else {}


def _load_user_data(user_id):
    uf = STORAGE_ROOT / "users" / user_id / "user.json"
    if uf.exists():
        return json.loads(uf.read_text())
    return {}


def _load_camera_config(user_id, camera_id):
    cam_file = STORAGE_ROOT / "users" / user_id / "cameras" / camera_id / "camera.json"
    if cam_file.exists():
        try:
            return json.loads(cam_file.read_text())
        except Exception:
            pass
    return {}



async def _generate_ai_narrative(summary: dict) -> str:
    """Genera un párrafo narrativo con Qwen basado en los datos del resumen."""
    try:
        totals = summary.get("totals", {})
        people = summary.get("people", {})
        items = summary.get("items", {})
        time_info = summary.get("time", {})
        comp = summary.get("comparison", {})
        date_str = summary.get("date", "ayer")

        # Construir contexto para Qwen
        facts = f"""Fecha: {date_str}
Eventos totales: {totals.get('events', 0)}
Alertas: {totals.get('alerts', 0)}
Clientes estimados: {people.get('clientes_estimado', 0)}
Empleados: {people.get('empleados', 0)}
Platos visibles: {items.get('platos', 0)}
Bebidas visibles: {items.get('bebidas', 0)}
Hora pico: {time_info.get('peak_hour', 'N/A')}:00
Cobertura: {time_info.get('coverage_hours', 0)} horas
Cambio vs ayer: {comp.get('delta_events', 0)} eventos"""

        prompt = f"""Basándote en estos datos de seguridad del negocio, escribe UN párrafo narrativo (4-6 oraciones) en español dominicano, tono cercano pero profesional.

{facts}

Reglas:
- Empieza con "Ayer en tu negocio..."
- Menciona lo más relevante (pico de actividad, personas, items)
- Si hubo alertas, menciónalas naturalmente
- Compara con ayer solo si hay cambio significativo (>20%)
- Termina con una sugerencia práctica
- NO uses palabras como "violación", "anomalía", "sospechoso"
- Máximo 150 palabras"""

        import httpx
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                "http://localhost:8004/v1/chat/completions",
                json={
                    "model": "qwen",
                    "messages": [
                        {"role": "system", "content": "Eres Eva, asistente de seguridad. Narras hechos del negocio en español dominicano, tono cercano."},
                        {"role": "user", "content": prompt}
                    ],
                    "max_tokens": 200
                }
            )
            if resp.status_code == 200:
                return resp.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        logger.error(f"Error generando narrativa: {e}")
    return ""


async def generate_daily_summary(user_id, target_date=None):
    date_str = target_date or date.today().isoformat()
    yesterday_str = (date.fromisoformat(date_str) - timedelta(days=1)).isoformat()

    today_events = []
    for evt, cam_name in _iter_events(user_id, date_filter=date_str):
        evt["_cam_name"] = cam_name
        today_events.append(evt)

    yesterday_events = []
    for evt, cam_name in _iter_events(user_id, date_filter=yesterday_str):
        yesterday_events.append(evt)

    # RD-2 (2026-09-01): MISMO DÍA de la semana anterior — la comparación
    # que importa ("un lunes normal"), no el día anterior (domingo vs
    # lunes es ruido para un negocio).
    week_before_str = (date.fromisoformat(date_str) - timedelta(days=7)).isoformat()
    week_before_events = list(_iter_events(user_id, date_filter=week_before_str))
    # tendencia: 3 días hacia atrás
    trend_days = []
    for i in range(2, 5):
        d = (date.fromisoformat(date_str) - timedelta(days=i)).isoformat()
        trend_days.append({"date": d, "events": sum(1 for _ in _iter_events(user_id, date_filter=d))})

    if not today_events:
        return {
            "date": date_str,
            "user_id": user_id,
            "generated_at": int(time.time()),
            "totals": {"events": 0},
            "summary_text": "Sin eventos registrados."
        }

    total_events = len(today_events)

    type_counts = defaultdict(int)
    for e in today_events:
        type_counts[e.get("event_type", "unknown")] += 1

    alert_events = [e for e in today_events if _event_is_alert(e)]
    attention_events = [e for e in today_events if e.get("attention_hits")]
    after_hours_events = [e for e in today_events if isinstance(e.get("metadata"), dict) and e.get("metadata", {}).get("after_hours")]
    vigilance_events = [e for e in today_events if e.get("event_type") == "vigilance_alert"]

    severity_counts = defaultdict(int)
    for e in today_events:
        qj = _event_qwen(e)
        sev = str(qj.get("importancia", "baja")).lower()
        severity_counts[sev] += 1

    persons_values = [_event_persons(e) for e in today_events]
    persons_values = [p for p in persons_values if p is not None]
    max_persons = max(persons_values) if persons_values else 0
    avg_persons = round(sum(persons_values) / len(persons_values), 1) if persons_values else 0

    total_clientes = 0
    total_empleados = 0
    total_platos = 0
    total_bebidas = 0
    total_fundas = 0
    for e in today_events:
        qj = _event_qwen(e)
        counts = qj.get("counts", {}) if isinstance(qj.get("counts"), dict) else {}
        # Los counts también pueden estar en qwen_json.vision (formato del orchestrator)
        vision = qj.get("vision", {}) if isinstance(qj.get("vision"), dict) else {}
        vision_counts = vision.get("counts", {}) if isinstance(vision.get("counts"), dict) else {}
        meta = e.get("metadata", {}) if isinstance(e.get("metadata"), dict) else {}
        meta_counts = meta.get("counts", {}) if isinstance(meta.get("counts"), dict) else {}
        all_counts = {**meta_counts, **counts, **vision_counts}
        total_clientes += (all_counts.get("clientes") or 0)
        total_empleados += (all_counts.get("empleados") or 0)
        total_platos += (all_counts.get("platos_visibles") or 0)
        total_bebidas += (all_counts.get("bebidas_visibles") or 0)
        total_fundas += (all_counts.get("fundas_visibles") or 0)

    hourly = defaultdict(lambda: {"events": 0, "persons": 0, "alerts": 0})
    for e in today_events:
        ts = e.get("timestamp", 0)
        if ts:
            hour = datetime.fromtimestamp(ts).hour
            hourly[hour]["events"] += 1
            p = _event_persons(e)
            if p:
                hourly[hour]["persons"] += p
            if _event_is_alert(e):
                hourly[hour]["alerts"] += 1

    hourly_dict = {str(h): hourly[h] for h in sorted(hourly.keys())}

    peak_hour = None
    slowest_hours = []
    if hourly:
        peak_hour = max(hourly.keys(), key=lambda h: hourly[h]["events"])
        all_hours = set(range(24))
        active_hours = set(hourly.keys())
        slowest_hours = sorted(all_hours - active_hours)

    camera_breakdown = defaultdict(lambda: {"events": 0, "alerts": 0, "persons": 0, "zone": ""})
    for e in today_events:
        cam_id = e.get("camera_id", "unknown")
        camera_breakdown[cam_id]["events"] += 1
        camera_breakdown[cam_id]["zone"] = e.get("_cam_name", cam_id)
        if _event_is_alert(e):
            camera_breakdown[cam_id]["alerts"] += 1
        p = _event_persons(e)
        if p:
            camera_breakdown[cam_id]["persons"] += p

    camera_health = {}
    user_data = _load_user_data(user_id)
    now_ts = time.time()
    # base de eventos por cámara para los huecos
    events_by_cam = {}
    for e in today_events:
        events_by_cam.setdefault(e.get("camera_id", ""), []).append(e)
    for cam in user_data.get("cameras", []):
        cam_id = cam.get("camera_id", "")
        # RD-2b: latido REAL = mtime de latest_raw.jpg (el last_frame de
        # user.json está throttled y miente; igual que el fix del panel).
        raw = STORAGE_ROOT / "users" / user_id / "cameras" / cam_id / "frames" / "latest_raw.jpg"
        last_frame = raw.stat().st_mtime if raw.exists() else cam.get("last_frame", 0)
        # si el usuario vive en otro disco (migrado), buscar allí también
        if last_frame == 0 or (now_ts - last_frame) > 600:
            dm = user_data.get("disk_mount")
            if dm:
                raw2 = Path(dm) / "users" / user_id / "cameras" / cam_id / "frames" / "latest_raw.jpg"
                if raw2.exists():
                    last_frame = raw2.stat().st_mtime
        age_min = (now_ts - last_frame) / 60 if last_frame else 9999
        cam_events = events_by_cam.get(cam_id, [])
        status = "online" if age_min < 10 else ("stale" if age_min < 60 else "offline")
        # RD-2: huecos de cobertura del día — horas del día sin NINGÚN evento
        # de esta cámara (con eventos YOLO gate: sin personas no hay evt; un
        # hueco largo puede ser cámara caída O negocio vacío — se reporta
        # solo si supera 4h para no alarmar en días muertos).
        hours_seen = sorted({int((e.get("timestamp", 0) % 86400) / 3600)
                             for e in cam_events if e.get("timestamp")})
        gaps = []
        if hours_seen:
            for h in range(8, 21):  # jornada típica 8AM-9PM
                if h not in hours_seen:
                    gaps.append(h)
        cam_gaps = [h for h in gaps] if len(gaps) > 4 else []
        camera_health[cam_id] = {
            "name": cam.get("name", cam_id),
            "zone": cam.get("zone", ""),
            "active": cam.get("active", False),
            "last_frame_ago_min": round(age_min, 1),
            "events_today": len(cam_events),
            "status": status,
            "coverage_gaps_hours": cam_gaps,  # RD-2
        }

    timestamps = [e.get("timestamp", 0) for e in today_events if e.get("timestamp")]
    first_event = min(timestamps) if timestamps else 0
    last_event = max(timestamps) if timestamps else 0
    coverage_hours = round((last_event - first_event) / 3600, 1) if timestamps else 0

    yesterday_total = len(yesterday_events)
    yesterday_alerts = len([e for e in yesterday_events if _event_is_alert(e)])
    yesterday_persons = sum(_event_persons(e) for e in yesterday_events)
    delta_events = total_events - yesterday_total
    delta_alerts = len(alert_events) - yesterday_alerts

    notable = []
    seen = set()
    for e in alert_events + attention_events + today_events:
        eid = e.get("event_id", "")
        if eid in seen:
            continue
        seen.add(eid)
        notable.append({
            "event_id": eid,
            "datetime": e.get("datetime", ""),
            "timestamp": e.get("timestamp", 0),
            "camera_id": e.get("camera_id", ""),
            "event_type": e.get("event_type", ""),
            "description": e.get("description", ""),
            "attention_hits": e.get("attention_hits", []),
        })
        if len(notable) >= 5:
            break

    summary = {
        "date": date_str,
        "user_id": user_id,
        "generated_at": int(time.time()),
        "totals": {
            "events": total_events,
            "normal": type_counts.get("normal", 0),
            "violations": type_counts.get("violation", 0),
            "vigilance_alerts": len(vigilance_events),
            "attention_events": len(attention_events),
            "after_hours_events": len(after_hours_events),
            "alerts": len(alert_events),
        },
        "severity": dict(severity_counts),
        "people": {
            "total_persons": sum(persons_values),
            "max_persons": max_persons,
            "avg_persons": avg_persons,
            "clientes_estimado": total_clientes,
            "empleados": total_empleados,
        },
        "items": {
            "platos": total_platos,
            "bebidas": total_bebidas,
            "fundas": total_fundas,
        },
        "time": {
            "first_event": first_event,
            "last_event": last_event,
            "coverage_hours": coverage_hours,
            "hourly": hourly_dict,
            "peak_hour": peak_hour,
            "quiet_hours": slowest_hours,
        },
        "cameras": {
            "breakdown": dict(camera_breakdown),
            "health": camera_health,
        },
        "comparison": {
            "yesterday_date": yesterday_str,
            "yesterday_events": yesterday_total,
            "delta_events": delta_events,
            "yesterday_alerts": yesterday_alerts,
            "delta_alerts": delta_alerts,
            "yesterday_persons": yesterday_persons,
            # RD-2: mismo día semana anterior (la referencia real del negocio)
            "week_before_date": week_before_str,
            "week_before_events": len(week_before_events),
            "week_before_persons": sum(_event_persons(e) for e in week_before_events),
            "delta_week": total_events - len(week_before_events),
            # RD-2: tendencia simple de 3 días
            "trend_days": trend_days,
            "trend_direction": ("up" if len(trend_days) >= 2 and
                                trend_days[0]["events"] > trend_days[-1]["events"]
                                else "down" if len(trend_days) >= 2 else "flat"),
        },
        "notable_events": notable,
    }

    # Generar narrativa AI (no bloqueante - si falla, continúa)
    try:
        summary["ai_narrative"] = await _generate_ai_narrative(summary)
    except Exception:
        summary["ai_narrative"] = ""

    _persist_summary(user_id, date_str, summary)
    return summary


def _persist_summary(user_id, date_str, summary):
    # RD-4: guardar en el disco ACTUAL del usuario (migraciones) y en el
    # compat NVMe — patrón dual como user.json.
    bases = [STORAGE_ROOT / "users" / user_id]
    try:
        ud = _load_user_data(user_id)
        dm = ud.get("disk_mount")
        if dm and dm != str(STORAGE_ROOT) and not str(dm).endswith("/users"):
            bases.insert(0, Path(dm) / "users" / user_id)
    except Exception:
        pass
    for base in bases:
        summary_dir = base / "summaries"
        summary_dir.mkdir(parents=True, exist_ok=True)
        path = summary_dir / f"daily_{date_str}.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(summary, indent=2, ensure_ascii=False, default=str))
        tmp.replace(path)
    logger.info(f"Daily summary saved: {date_str} (user {user_id[:8]}…)")


def load_summary(user_id, date_str):
    path = STORAGE_ROOT / "users" / user_id / "summaries" / f"daily_{date_str}.json"
    if path.exists():
        return json.loads(path.read_text())
    return {}


def get_latest_summary(user_id):
    today = date.today().isoformat()
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    summary = load_summary(user_id, yesterday)
    if not summary:
        summary = load_summary(user_id, today)
    return summary
