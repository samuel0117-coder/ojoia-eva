#!/usr/bin/env python3
"""
eva/daily_summary_cron.py - Genera resumen diario y envía push notification.
Se ejecuta a las 8:00 AM para enviar el resumen del día anterior.
"""
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
        cred_path = Path("/opt/ojoia/code/firebase-key.json")
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
            
            title = f"Resumen de ayer — {biz_name}"
            parts = [f"{t.get('events', 0)} análisis"]
            
            alerts = t.get("alerts", 0)
            if alerts > 0:
                parts.append(f"⚠️ {alerts} alerta{'s' if alerts != 1 else ''}")
            else:
                parts.append("sin alertas ✅")
            
            if p.get("clientes_estimado", 0) > 0:
                parts.append(f"~{p['clientes_estimado']} clientes")
            
            body = ", ".join(parts) + "."
            
            # Enviar push
            await send_fcm_notification(user_id, title, body)
            
        except Exception as e:
            logger.error(f"Error processing {user_id}: {e}")


if __name__ == "__main__":
    asyncio.run(generate_and_notify())
