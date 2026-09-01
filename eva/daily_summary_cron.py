#!/usr/bin/env python3
"""
eva/daily_summary_cron.py - Genera resumen diario y envía push notification.
Se ejecuta a las 8:00 AM para enviar el resumen del día anterior.
"""
import os
import asyncio
import json
import logging
from datetime import date, timedelta
from pathlib import Path

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

STORAGE_ROOT = Path("/home/sam/storage")


async def send_fcm_notification(user_id: str, title: str, body: str):
    """Envía notificación push via FCM."""
    try:
        import httpx
        # Cargar Firebase credentials
        cred_path = Path(os.environ.get("FIREBASE_KEY_PATH", "/opt/ojoia/config/firebase-key.json"))
        if not cred_path.exists():
            logger.warning("Firebase key not found, skipping push")
            return
        
        # Obtener access token
        from google.oauth2 import service_account
        from google.auth.transport.requests import Request
        
        credentials = service_account.Credentials.from_service_account_file(
            str(cred_path),
            scopes=["https://www.googleapis.com/auth/firebase.messaging"]
        )
        credentials.refresh(Request())
        access_token = credentials.token
        
        # Cargar tokens FCM del usuario
        user_file = STORAGE_ROOT / "users" / user_id / "user.json"
        if not user_file.exists():
            logger.warning(f"User file not found: {user_id}")
            return
        
        with open(user_file) as f:
            user_data = json.load(f)
        
        fcm_tokens = user_data.get("fcm_tokens", [])
        if not fcm_tokens:
            logger.warning(f"No FCM tokens for user: {user_id}")
            return
        
        # Enviar notificación a cada token
        for token in fcm_tokens[:3]:  # Máximo 3 tokens
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(
                    "https://fcm.googleapis.com/v1/projects/ojoia-67216/messages:send",
                    headers={
                        "Authorization": f"Bearer {access_token}",
                        "Content-Type": "application/json"
                    },
                    json={
                        "message": {
                            "token": token,
                            "notification": {
                                "title": title,
                                "body": body
                            },
                            "data": {
                                "type": "daily_summary",
                                "date": (date.today() - timedelta(days=1)).isoformat()
                            },
                            "webpush": {
                                "fcm_options": {
                                    "link": "https://ojoia.com.do"
                                }
                            }
                        }
                    }
                )
                if resp.status_code == 200:
                    logger.info(f"Push sent to {user_id}")
                else:
                    logger.error(f"Push failed: {resp.status_code} {resp.text}")
    except Exception as e:
        logger.error(f"Error sending push: {e}")


async def generate_and_notify():
    """Genera resúmenes para todos los usuarios activos y envía notificaciones."""
    from eva.daily_summary import generate_daily_summary
    
    users_dir = STORAGE_ROOT / "users"
    if not users_dir.exists():
        return
    
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    
    for user_dir in users_dir.iterdir():
        if not user_dir.is_dir():
            continue
        
        user_id = user_dir.name
        user_file = user_dir / "user.json"
        
        if not user_file.exists():
            continue
        
        try:
            with open(user_file) as f:
                user_data = json.load(f)
            
            # Solo usuarios activos con cámaras
            cameras = user_data.get("cameras", [])
            active_cameras = [c for c in cameras if c.get("active")]
            if not active_cameras:
                continue
            
            # Generar resumen
            logger.info(f"Generating summary for {user_id}")
            summary = await generate_daily_summary(user_id, yesterday)
            
            if not summary or summary.get("totals", {}).get("events", 0) == 0:
                continue
            
            # Crear mensaje de notificación
            t = summary.get("totals", {})
            p = summary.get("people", {})
            biz_name = user_data.get("business_name", "tu negocio")
            
            # RD-3 (2026-09-01): Reporte Matutino de 6 bloques (30s de lectura).
            # Filosofía (investigación 2026): cada número debe mover una
            # decisión; máximo 6, sin ruido POS.
            tm = summary.get("time", {})
            cmp_ = summary.get("comparison", {})
            cams = summary.get("cameras", {}).get("health", {})
            fecha_humana = date.fromisoformat(yesterday).strftime("%a %d %b")

            title = f"🌅 Así estuvo tu negocio ayer ({fecha_humana})"
            L = []
            # 1) Personas + comparación vs MISMO DÍA semana previa
            persons = p.get("total_persons", 0)
            delta_w = cmp_.get("delta_week")
            delta_txt = ""
            if delta_w is not None:
                if delta_w > 0:
                    delta_txt = f" (▲{delta_w} vs {cmp_.get('week_before_date','')[-5:]})"
                elif delta_w < 0:
                    delta_txt = f" (▼{abs(delta_w)} vs {cmp_.get('week_before_date','')[-5:]})"
            L.append(f"👥 {persons} personas detectadas{delta_txt}")
            # 2) Hora pico (staffing)
            peak = tm.get("peak_hour")
            if peak is not None:
                L.append(f"⏰ Hora pico: {peak}:00–{int(peak)+1}:00")
            # 3) Alertas del día
            alerts = t.get("alerts", 0)
            if alerts > 0:
                det = summary.get("notable_events", [])
                primera = (det[0].get("description", "") or "")[:45] if det else ""
                L.append(f"🚨 {alerts} alerta{'s' if alerts != 1 else ''}"
                         + (f" — primera: {primera}…" if primera else ""))
            else:
                L.append("🚨 0 alertas — día tranquilo ✅")
            # 4) Cobertura (honestidad: si hubo hueco, decirlo)
            total_cams = len(cams)
            ok_cams = sum(1 for c in cams.values() if c.get("status") in ("online", "stale"))
            if total_cams:
                L.append(f"📷 {'✅ todas' if ok_cams == total_cams else f'{ok_cams}/{total_cams}'} las cámaras activas")
            # 5) Tendencia
            tr = cmp_.get("trend_direction")
            if tr == "up":
                L.append("📈 3 días con tráfico subiendo")
            elif tr == "down":
                L.append("📉 3 días con tráfico bajando")
            # 6) CTA
            L.append("👀 Abre la app para el detalle completo")

            body = "\n".join(L)
            await send_fcm_notification(user_id, title, body)
            
        except Exception as e:
            logger.error(f"Error processing {user_id}: {e}")


if __name__ == "__main__":
    asyncio.run(generate_and_notify())
