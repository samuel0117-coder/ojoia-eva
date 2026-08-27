"""
daily_report_prod.py - Versión optimizada para producción
- Botón de descarga directa (no link)
- Tracking de tiempo de entrega
- FCM push optimizado
"""

import json
import os
import time
import logging
from pathlib import Path
from datetime import datetime
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)
STORAGE_ROOT = Path("/home/sam/storage")

# Configuración de tiempos
PUSH_TIMEOUT_SECONDS = 30
TOAST_DURATION_MS = 15000  # 15 segundos visibles

async def send_daily_report_complete(
    user_id: str, 
    camera_id: Optional[str] = None, 
    date: str = "yesterday",
    track_timing: bool = True
) -> Dict[str, Any]:
    """
    Envío completo optimizado para producción:
    1. Genera reporte PDF/HTML
    2. Inyecta en chat con botón de descarga
    3. Envía push FCM (apunta al chat)
    4. Trackea tiempos de entrega
    
    Returns:
        {
            "success": bool,
            "chat_injected": bool,
            "push_sent": bool,
            "push_delivery_time_ms": int,
            "pdf_url": str,
            "message": str,
            "timing": {...}
        }
    """
    start_time = time.time()
    timing = {"start": start_time}
    
    try:
        # 1. Generar reporte
        from .daily_report import generate_daily_report_pdf
        t0 = time.time()
        report = await generate_daily_report_pdf(user_id, camera_id, date)
        timing["report_generation_ms"] = int((time.time() - t0) * 1000)
        
        if not report.get("success"):
            return {"success": False, "error": report.get("error"), "timing": timing}
        
        # 2. Preparar mensaje con BOTÓN DE DESCARGA
        business_name = report.get("business_name", "Tu negocio")
        summary = report.get("summary", {})
        pdf_url = report.get("pdf_url", "")
        pdf_path = report.get("pdf_path", "")
        
        # URL absoluta para producción
        pdf_url_abs = pdf_url if pdf_url.startswith("http") else f"https://ojoia.com.do{pdf_url}"
        
        # Mensaje optimizado (Markdown con link que el chat renderiza como botón)
        message = f"""🍽️ *Reporte Diario - {business_name}*

📊 Análisis realizados: {summary.get('total_events', 0)}
👥 Personas únicas: {summary.get('persons_total', 0)}

📄 *Reporte completo listo*

[📥 Descargar reporte PDF]({pdf_url_abs})

_Generado automáticamente a las 7:30 AM_"""
        
        # 3. Inyectar en chat (backend _sessions)
        t1 = time.time()
        chat_injected = _inject_to_chat_session(user_id, message)
        timing["chat_injection_ms"] = int((time.time() - t1) * 1000)
        
        # 4. También guardar en eva_chat_history.json (persistencia)
        _save_to_chat_history(user_id, message)
        
        # 5. Enviar push FCM
        t2 = time.time()
        push_result = await _send_fcm_push(
            user_id=user_id,
            title="📊 Reporte Diario Disponible",
            body=f"Tu reporte de {business_name} está listo en el chat",
            pdf_url=pdf_url_abs,
            action="open_chat",
            duration_seconds=15
        )
        timing["push_send_ms"] = int((time.time() - t2) * 1000)
        timing["push_delivery_time_ms"] = push_result.get("delivery_time_ms", 0)
        
        # 6. Guardar en notificaciones
        _save_notification_record(
            user_id=user_id,
            message=message,
            pdf_url=pdf_url,
            chat_injected=chat_injected,
            push_sent=push_result.get("sent", False),
            timing=timing
        )
        
        timing["total_ms"] = int((time.time() - start_time) * 1000)
        
        logger.info(f"✅ Reporte enviado a {user_id} en {timing['total_ms']}ms")
        
        return {
            "success": True,
            "chat_injected": chat_injected,
            "push_sent": push_result.get("sent", False),
            "push_delivery_time_ms": timing.get("push_delivery_time_ms", 0),
            "pdf_url": pdf_url_abs,
            "pdf_path": pdf_path,
            "message": message,
            "timing": timing,
            "business_name": business_name
        }
        
    except Exception as e:
        logger.error(f"Error send_daily_report_complete: {e}")
        import traceback
        traceback.print_exc()
        return {
            "success": False,
            "error": str(e),
            "timing": timing
        }


def _inject_to_chat_session(user_id: str, message: str) -> bool:
    """Inyecta mensaje en la sesión activa del chat (memoria)."""
    try:
        from eva_v2 import _sessions
        
        # Buscar sesión activa
        session_id = None
        for sid, sdata in _sessions.items():
            if sdata.get("user_id") == user_id:
                session_id = sid
                break
        
        if not session_id:
            session_id = f"chat_{user_id}_{int(time.time())}"
            _sessions[session_id] = {
                "user_id": user_id,
                "camera_id": "",
                "msgs": [],
                "messages": [],
                "last_activity": time.time()
            }
        
        # Inyectar mensaje
        _sessions[session_id]["msgs"].append({
            "role": "assistant",
            "content": message,
            "timestamp": time.time(),
            "summary": True,
            "is_daily_report": True
        })
        
        _sessions[session_id]["messages"].append({
            "role": "assistant",
            "content": message,
            "timestamp": time.time()
        })
        
        logger.info(f"✅ Inyectado en sesión {session_id}")
        return True
        
    except Exception as e:
        logger.error(f"Error _inject_to_chat_session: {e}")
        return False


def _save_to_chat_history(user_id: str, message: str) -> bool:
    """Guarda mensaje en eva_chat_history.json (persistencia)."""
    try:
        history_file = STORAGE_ROOT / "users" / user_id / "eva_chat_history.json"
        
        if history_file.exists():
            with open(history_file) as f:
                data = json.load(f)
        else:
            data = {"history": [], "summary": ""}
        
        data["history"].append({
            "role": "assistant",
            "content": message,
            "timestamp": time.time(),
            "summary": True,
            "is_daily_report": True
        })
        
        with open(history_file, "w") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        
        logger.info(f"✅ Guardado en {history_file}")
        return True
        
    except Exception as e:
        logger.error(f"Error _save_to_chat_history: {e}")
        return False


async def _send_fcm_push(
    user_id: str,
    title: str,
    body: str,
    pdf_url: str,
    action: str = "open_chat",
    duration_seconds: int = 15
) -> Dict[str, Any]:
    """Envía push FCM optimizado."""
    start = time.time()
    
    try:
        from google.oauth2 import service_account
        import google.auth.transport.requests
        import requests as _req
        
        # Leer tokens
        user_file = STORAGE_ROOT / "users" / user_id / "user.json"
        if not user_file.exists():
            return {"sent": False, "error": "user.json not found"}
        
        with open(user_file) as f:
            user_data = json.loads(f.read_text())
        
        tokens = user_data.get("fcm_tokens", [])
        business_name = user_data.get("business_name", "Tu negocio")
        
        if not tokens:
            return {"sent": False, "error": "no tokens"}
        
        # OAuth2
        creds = service_account.Credentials.from_service_account_file(
            "/home/sam/ai_system/firebase-key.json",
            scopes=["https://www.googleapis.com/auth/firebase.messaging"]
        )
        creds.refresh(google.auth.transport.requests.Request())
        
        # Link al chat (no a events/reports)
        chat_link = "https://ojoia.com.do/#chat"
        
        sent_count = 0
        for tok in tokens:
            try:
                payload = {
                    "message": {
                        "token": tok,
                        "notification": {
                            "title": title,
                            "body": body,
                            "click_action": chat_link
                        },
                        "data": {
                            "type": "daily_report",
                            "url": chat_link,
                            "pdf_url": pdf_url,
                            "action": action,
                            "duration_seconds": str(duration_seconds),
                            "title": title,
                            "body": body,
                            "tag": "daily_report"
                        },
                        "webpush": {
                            "notification": {
                                "title": title,
                                "body": body,
                                "icon": "/img/icon-192.png",
                                "badge": "/img/icon-192.png",
                                "require_interaction": True,
                                "tag": "daily_report",
                                "timestamp": int(time.time() * 1000)
                            },
                            "fcm_options": {"link": chat_link},
                            "data": {
                                "duration_seconds": str(duration_seconds),
                                "action": action
                            }
                        },
                        "android": {
                            "priority": "high",
                            "ttl": f"{duration_seconds}s",
                            "notification": {
                                "channel_id": "daily_reports",
                                "visibility": "PUBLIC",
                                "click_action": "OPEN_CHAT_ACTIVITY"
                            }
                        }
                    }
                }
                
                resp = _req.post(
                    "https://fcm.googleapis.com/v1/projects/ojoia-67216/messages:send",
                    headers={"Authorization": f"Bearer {creds.token}", "Content-Type": "application/json"},
                    json=payload,
                    timeout=10
                )
                
                if resp.status_code == 200:
                    sent_count += 1
            
            except Exception as e:
                logger.warning(f"FCM token error: {e}")
        
        delivery_time = int((time.time() - start) * 1000)
        logger.info(f"✅ FCM: {sent_count}/{len(tokens)} en {delivery_time}ms")
        
        return {
            "sent": sent_count > 0,
            "count": sent_count,
            "total_tokens": len(tokens),
            "delivery_time_ms": delivery_time
        }
        
    except Exception as e:
        logger.error(f"Error _send_fcm_push: {e}")
        return {"sent": False, "error": str(e)}


def _save_notification_record(
    user_id: str,
    message: str,
    pdf_url: str,
    chat_injected: bool,
    push_sent: bool,
    timing: Dict
) -> bool:
    """Guarda registro de notificación enviada."""
    try:
        notif_dir = STORAGE_ROOT / "users" / user_id / "notifications"
        notif_dir.mkdir(parents=True, exist_ok=True)
        
        notif_file = notif_dir / f"report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        
        record = {
            "type": "daily_report",
            "user_id": user_id,
            "sent_at": datetime.now().isoformat(),
            "message": message,
            "pdf_url": pdf_url,
            "channels": {
                "chat": chat_injected,
                "push_fcm": push_sent
            },
            "timing": timing,
            "read": False
        }
        
        with open(notif_file, "w") as f:
            json.dump(record, f, indent=2, ensure_ascii=False)
        
        return True
        
    except Exception as e:
        logger.error(f"Error _save_notification_record: {e}")
        return False
