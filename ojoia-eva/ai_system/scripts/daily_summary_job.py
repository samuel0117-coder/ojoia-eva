#!/usr/bin/env python3
"""
scripts/daily_summary_job.py — Resumen diario automático.

Se ejecuta cada mañana a las 6:00am via cron.
Para cada usuario activo:
1. Lee los eventos del día anterior
2. Genera un resumen con estadísticas y highlights de Qwen
3. Guarda en business.json → daily_summaries
4. (Futuro) Envía por WhatsApp/FCM

Uso:
  python3 scripts/daily_summary_job.py              # Procesar todos los usuarios
  python3 scripts/daily_summary_job.py <user_id>    # Procesar un usuario específico
  python3 scripts/daily_summary_job.py --date 2026-06-05  # Procesar fecha específica
"""

import json
import os
import sys
import time
import logging
from datetime import datetime, timedelta
from pathlib import Path

# Configurar path
SCRIPT_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(SCRIPT_DIR))

STORAGE_ROOT = Path("/home/sam/storage")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(STORAGE_ROOT / "daily_summary.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)


def load_business_json(user_id: str) -> dict:
    bp = STORAGE_ROOT / "users" / user_id / "business.json"
    if bp.exists():
        with open(bp) as f:
            return json.load(f)
    up = STORAGE_ROOT / "users" / user_id / "user.json"
    if up.exists():
        with open(up) as f:
            return json.load(f)
    return {}


def save_business_json(user_id: str, data: dict):
    bp = STORAGE_ROOT / "users" / user_id / "business.json"
    bp.parent.mkdir(parents=True, exist_ok=True)
    tmp = bp.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    tmp.replace(bp)


def resolve_user_events_dirs(user_id: str) -> list:
    base = STORAGE_ROOT / "users" / user_id
    dirs = []
    cameras_dir = base / "cameras"
    if cameras_dir.exists():
        for cam_id in cameras_dir.iterdir():
            events = cam_id / "events"
            if events.is_dir():
                dirs.append((cam_id.name, events))
    return dirs


def generate_daily_summary(user_id: str, date_str: str = None) -> dict:
    """Genera el resumen de un día para un usuario."""
    if not date_str:
        date_str = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

    business = load_business_json(user_id)
    if not business:
        logger.warning(f"Usuario {user_id} sin datos")
        return {}

    # Rango de fechas
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    start_ts = int(dt.replace(hour=0, minute=0, second=0).timestamp())
    end_ts = int(dt.replace(hour=23, minute=59, second=59).timestamp())

    total_events = 0
    total_persons = 0
    alerts = 0
    false_alarms = 0
    peak_hour = None
    peak_persons = 0
    hourly_persons = {}
    highlights = []
    cameras_data = {}

    for cam_id, events_dir in resolve_user_events_dirs(user_id):
        if cam_id == "_global":
            continue
        cam_name = business.get("cameras", {}).get(cam_id, {}).get("name", cam_id)
        cam_events = []
        cam_persons = 0
        cam_alerts = 0

        if not events_dir.exists():
            continue

        for fname in sorted(os.listdir(events_dir)):
            if not fname.endswith(".json"):
                continue
            try:
                with open(events_dir / fname) as f:
                    event = json.load(f)
            except Exception:
                continue

            ts = event.get("timestamp", 0)
            if ts < start_ts or ts > end_ts:
                continue

            total_events += 1
            qwen = event.get("qwen_analysis", {})
            persons = qwen.get("persons", event.get("yolo", {}).get("count", 0))
            total_persons += persons
            cam_persons += persons

            hour = datetime.fromtimestamp(ts).strftime("%H:%M")
            hourly_persons[hour] = hourly_persons.get(hour, 0) + persons

            if persons > peak_persons:
                peak_persons = persons
                peak_hour = hour

            if event.get("event_type") in ("violation", "night_alert"):
                alerts += 1
                cam_alerts += 1

            if event.get("feedback", {}).get("is_false_alarm"):
                false_alarms += 1

            desc = qwen.get("description", event.get("description", ""))
            if desc:
                cam_events.append(f"{hour} — {desc}")

        if cam_events:
            cameras_data[cam_name] = {
                "events": cam_events,
                "total_persons": cam_persons,
                "alerts": cam_alerts
            }

    # Generar highlights (momentos clave del día)
    for cam_name, data in cameras_data.items():
        for evt in data["events"]:
            if "ALERTA" in evt.upper() or "anomalía" in evt.lower():
                highlights.append(f"⚠️ {cam_name}: {evt}")
        # Agregar el evento con más personas
        if data["events"]:
            highlights.append(f"📊 {cam_name}: {data['events'][0]}")

    summary = {
        "date": date_str,
        "total_events": total_events,
        "total_persons": total_persons,
        "alerts": alerts,
        "false_alarms": false_alarms,
        "peak_hour": peak_hour,
        "peak_persons": peak_persons,
        "cameras_data": cameras_data,
        "highlights": highlights[:10],
        "generated_at": datetime.now().isoformat()
    }

    # Guardar en business.json
    business.setdefault("daily_summaries", {})[date_str] = summary
    save_business_json(user_id, business)

    logger.info(f"✅ Resumen {date_str} para {user_id}: {total_events} eventos, {total_persons} personas, {alerts} alertas")
    return summary


def get_all_user_ids() -> list:
    """Obtiene todos los IDs de usuario."""
    users_dir = STORAGE_ROOT / "users"
    if not users_dir.exists():
        return []
    users = []
    for d in users_dir.iterdir():
        if d.is_dir() and (d / "user.json").exists():
            users.append(d.name)
    return users


def format_summary_message(summary: dict, business: dict) -> str:
    """Formatea el resumen como mensaje de texto."""
    lines = [
        f"📊 *Resumen del {summary['date']}* — {business.get('business_name', 'Tu negocio')}",
        "",
        f"📈 *General:*",
        f"  • {summary['total_events']} eventos",
        f"  • {summary['total_persons']} personas detectadas",
        f"  • {summary['alerts']} alertas",
    ]

    if summary.get("peak_hour"):
        lines.append(f"  • Pico: {summary['peak_hour']} ({summary['peak_persons']} personas)")

    if summary.get("false_alarms"):
        lines.append(f"  • {summary['false_alarms']} falsas alarmas")

    if summary.get("highlights"):
        lines.append("")
        lines.append("🔍 *Momentos clave:*")
        for h in summary["highlights"][:5]:
            lines.append(f"  • {h}")

    lines.append("")
    lines.append("Ver detalles: https://ojoia.com.do/")

    return "\n".join(lines)


if __name__ == "__main__":
    user_id = None
    date_str = None

    # Parsear argumentos
    for arg in sys.argv[1:]:
        if arg.startswith("--date="):
            date_str = arg.split("=")[1]
        elif arg == "--help":
            print(__doc__)
            sys.exit(0)
        elif not arg.startswith("--"):
            user_id = arg

    if not date_str:
        date_str = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

    logger.info(f"Generando resumen diario para {date_str}")

    if user_id:
        users = [user_id]
    else:
        users = get_all_user_ids()
        logger.info(f"Procesando {len(users)} usuarios")

    for uid in users:
        try:
            summary = generate_daily_summary(uid, date_str)
            if summary:
                business = load_business_json(uid)
                msg = format_summary_message(summary, business)
                logger.info(f"Mensaje generado para {uid}:\n{msg[:200]}...")
        except Exception as e:
            logger.error(f"Error procesando {uid}: {e}")

    logger.info("Resumen diario completado")
