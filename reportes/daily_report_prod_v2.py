"""
daily_report_prod_v2.py - Versión optimizada con URLs reales
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

async def send_daily_report_with_real_url(
    user_id: str, 
    camera_id: Optional[str] = None, 
    date: str = "yesterday"
) -> Dict[str, Any]:
    """
    Envío completo con URLs reales:
    1. Genera página HTML + PDF
    2. Inyecta en chat con URL real
    3. Envía push FCM apuntando a URL real
    """
    start_time = time.time()
    timing = {"start": start_time}
    
    try:
        # 1. Generar página HTML + PDF (obtener URLs reales)
        from .page_generator import generate_report_page
        t0 = time.time()
        page_result = await generate_report_page(user_id, date, camera_id)
        timing["page_generation_ms"] = int((time.time() - t0) * 1000)
        
        if not page_result.get("success"):
            return page_result
        
        html_url = page_result.get("html_url")
        pdf_url = page_result.get("pdf_url")
        report = page_result.get("report", {})
        
        # 2. Preparar mensaje con URL REAL
        business_name = report.get("business_name", "Tu negocio")
        summary = report.get("summary", {})
        
        # Mensaje CON URL REAL QUE ABRE PÁGINA
        message = f"""🍽️ *Reporte Diario - {business_name}*

📊 Análisis realizados: {summary.get('total_events', 0)}
👥 Personas únicas: {summary.get('persons_total', 0)}

📄 *Tu reporte está listo*

[📊 Ver reporte completo]({html_url})
[📥 Descargar PDF]({pdf_url})

_Generado automáticamente a las 7:30 AM_"""
        
        # 3. Inyectar en chat
        t1 = time.time()
        chat_injected = _inject_to_chat_session_v2(user_id, message, html_url)
        timing["chat_injection_ms"] = int((time.time() - t1) * 1000)
        
        # 4. Guardar en historial
        _save_to_chat_history_v2(user_id, message, html_url)
        
        # 5. Enviar push FCM apuntando a URL REAL
        t2 = time.time()
        push_result = await _send_fcm_push_with_url(
            user_id=user_id,
            title="📊 Reporte Diario Disponible",
            body=f"Tu reporte de {business_name} está listo. Toca para ver.",
            target_url=html_url,  # URL REAL, no #chat
            duration_seconds=15
        )
        timing["push_send_ms"] = int((time.time() - t2) * 1000)
        timing["push_delivery_time_ms"] = push_result.get("delivery_time_ms", 0)
        
        # 6. Guardar registro
        _save_notification_record_v2(
            user_id=user_id,
            message=message,
            html_url=html_url,
            pdf_url=pdf_url,
            chat_injected=chat_injected,
            push_sent=push_result.get("sent", False),
            timing=timing
        )
        
        timing["total_ms"] = int((time.time() - start_time) * 1000)
        
        logger.info(f"✅ Reporte enviado a {user_id} en {timing['total_ms']}ms")
        logger.info(f"   HTML URL: {html_url}")
        logger.info(f"   PDF URL: {pdf_url}")
        
        return {
            "success": True,
            "chat_injected": chat_injected,
            "push_sent": push_result.get("sent", False),
            "push_delivery_time_ms": timing.get("push_delivery_time_ms", 0),
            "html_url": html_url,
            "pdf_url": pdf_url,
            "message": message,
            "timing": timing,
            "business_name": business_name
        }
        
    except Exception as e:
        logger.error(f"Error send_daily_report_with_real_url: {e}")
        import traceback
        traceback.print_exc()
        return {
            "success": False,
            "error": str(e)
        }


def _inject_to_chat_session_v2(user_id: str, message: str, url: str) -> bool:
    """Inyecta mensaje con URL en sesión activa."""
    try:
        from eva_v2 import _sessions
        
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
        
        _sessions[session_id]["msgs"].append({
            "role": "assistant",
            "content": message,
            "timestamp": time.time(),
            "summary": True,
            "is_daily_report": True,
            "report_url": url
        })
        
        _sessions[session_id]["messages"].append({
            "role": "assistant",
            "content": message,
            "timestamp": time.time()
        })
        
        logger.info(f"✅ Inyectado en sesión {session_id}")
        return True
        
    except Exception as e:
        logger.error(f"Error _inject_to_chat_session_v2: {e}")
        return False


def _save_to_chat_history_v2(user_id: str, message: str, url: str) -> bool:
    """Guarda mensaje con URL en eva_chat_history.json."""
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
            "is_daily_report": True,
            "report_url": url
        })
        
        with open(history_file, "w") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        
        logger.info(f"✅ Guardado en {history_file}")
        return True
        
    except Exception as e:
        logger.error(f"Error _save_to_chat_history_v2: {e}")
        return False


async def _send_fcm_push_with_url(
    user_id: str,
    title: str,
    body: str,
    target_url: str,
    duration_seconds: int = 15
) -> Dict[str, Any]:
    """Envía push FCM apuntando a URL REAL."""
    start = time.time()
    
    try:
        from google.oauth2 import service_account
        import google.auth.transport.requests
        import requests as _req
        
        user_file = STORAGE_ROOT / "users" / user_id / "user.json"
        if not user_file.exists():
            return {"sent": False, "error": "user.json not found"}
        
        with open(user_file) as f:
            user_data = json.loads(f.read_text())
        
        tokens = user_data.get("fcm_tokens", [])
        if not tokens:
            return {"sent": False, "error": "no tokens"}
        
        creds = service_account.Credentials.from_service_account_file(
            "/home/sam/ai_system/firebase-key.json",
            scopes=["https://www.googleapis.com/auth/firebase.messaging"]
        )
        creds.refresh(google.auth.transport.requests.Request())
        
        sent_count = 0
        for tok in tokens:
            try:
                payload = {
                    "message": {
                        "token": tok,
                        "notification": {
                            "title": title,
                            "body": body,
                            "click_action": target_url  # URL REAL
                        },
                        "data": {
                            "type": "daily_report",
                            "url": target_url,  # URL REAL
                            "title": title,
                            "body": body,
                            "tag": "daily_report",
                            "duration_seconds": str(duration_seconds)
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
                            "fcm_options": {"link": target_url},  # URL REAL
                            "data": {
                                "duration_seconds": str(duration_seconds)
                            }
                        },
                        "android": {
                            "priority": "high",
                            "ttl": f"{duration_seconds}s",
                            "notification": {
                                "channel_id": "daily_reports",
                                "visibility": "PUBLIC",
                                "click_action": target_url  # URL REAL
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
        logger.info(f"   Target URL: {target_url}")
        
        return {
            "sent": sent_count > 0,
            "count": sent_count,
            "total_tokens": len(tokens),
            "delivery_time_ms": delivery_time
        }
        
    except Exception as e:
        logger.error(f"Error _send_fcm_push_with_url: {e}")
        return {"sent": False, "error": str(e)}


def _save_notification_record_v2(
    user_id: str,
    message: str,
    html_url: str,
    pdf_url: str,
    chat_injected: bool,
    push_sent: bool,
    timing: Dict
) -> bool:
    """Guarda registro de notificación."""
    try:
        notif_dir = STORAGE_ROOT / "users" / user_id / "notifications"
        notif_dir.mkdir(parents=True, exist_ok=True)
        
        notif_file = notif_dir / f"report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        
        record = {
            "type": "daily_report",
            "user_id": user_id,
            "sent_at": datetime.now().isoformat(),
            "message": message,
            "urls": {
                "html": html_url,
                "pdf": pdf_url
            },
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
        logger.error(f"Error _save_notification_record_v2: {e}")
        return False
