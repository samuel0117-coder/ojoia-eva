"""
reportes/daily_report.py - Generador de reportes diarios en PDF
"""

import json
import os
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, List, Any, Optional
import logging

logger = logging.getLogger(__name__)
STORAGE_ROOT = Path("/home/sam/storage")

SMART_SUMMARY_CONFIG = {
    "restaurant": {
        "metrics": ["platos", "bebidas", "fundas", "clientes"],
        "title": "Reporte Diario - Restaurante",
        "icon": "🍽️",
        "metrics_labels": {
            "platos": "🍽️ Platos servidos",
            "bebidas": "🥤 Bebidas vendidas",
            "fundas": "🛍️ Fundas usadas",
            "clientes": "👥 Clientes atendidos"
        }
    },
    "farmacia": {
        "metrics": ["clientes", "medicamentos", "consultas"],
        "title": "Reporte Diario - Farmacia",
        "icon": "💊",
        "metrics_labels": {
            "clientes": "👥 Clientes atendidos",
            "medicamentos": "💊 Medicamentos",
            "consultas": "🩺 Consultas"
        }
    },
    "retail": {
        "metrics": ["clientes", "carritos", "cajas"],
        "title": "Reporte Diario - Tienda",
        "icon": "🛒",
        "metrics_labels": {
            "clientes": "👥 Clientes",
            "carritos": "🛒 Carritos",
            "cajas": "💰 Cajas"
        }
    },
    "default": {
        "metrics": ["personas", "objetos", "eventos"],
        "title": "Reporte Diario",
        "icon": "📊",
        "metrics_labels": {
            "personas": "👥 Personas",
            "objetos": "📦 Objetos",
            "eventos": "🔔 Eventos"
        }
    }
}

async def generate_daily_report_pdf(user_id: str, camera_id: str = None, date: str = "today") -> Dict[str, Any]:
    try:
        from eva.tools import tool_get_activity_summary
        summary_data = await tool_get_activity_summary(user_id, date, camera_id)
        
        user_file = STORAGE_ROOT / "users" / user_id / "user.json"
        if user_file.exists():
            user_data = json.loads(user_file.read_text())
            business_type = (user_data.get("business_type") or "default").lower()
            business_name = user_data.get("business_name") or "Negocio"
        else:
            business_type = "default"
            business_name = "Negocio"
        
        config = SMART_SUMMARY_CONFIG.get(business_type, SMART_SUMMARY_CONFIG["default"])
        
        report_dir = STORAGE_ROOT / "users" / user_id / "daily_reports"
        report_dir.mkdir(parents=True, exist_ok=True)
        
        date_str = date if date != "today" else datetime.now().strftime("%Y-%m-%d")
        camera_suffix = f"_{camera_id}" if camera_id else ""
        pdf_filename = f"reporte_{date_str}{camera_suffix}.html"
        pdf_path = report_dir / pdf_filename
        pdf_url = f"/storage/users/{user_id}/daily_reports/{pdf_filename}"
        
        html_content = f"""
<html>
<head>
    <style>
        body {{ font-family: Arial, sans-serif; padding: 20px; background: #fafafa; }}
        h1 {{ color: #1a1a1a; border-bottom: 2px solid #4a90e2; padding-bottom: 10px; }}
        h2 {{ color: #4a90e2; }}
        h3 {{ color: #333; }}
        .metric {{ margin: 10px 0; padding: 15px; background: white; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }}
        .event {{ margin: 8px 0; padding: 10px; border-left: 4px solid #4a90e2; background: #f5f5f5; }}
        .footer {{ margin-top: 30px; padding-top: 20px; border-top: 1px solid #ddd; color: #666; font-size: 12px; }}
    </style>
</head>
<body>
    <h1>{config['icon']} {config['title']}</h1>
    <h2>{business_name} - {date_str}</h2>
    
    <h3>📋 Resumen Ejecutivo</h3>
    <div style="background: white; padding: 15px; border-radius: 8px;">
    <pre style="white-space: pre-wrap; line-height: 1.6;">{summary_data.get('summary', 'Sin resumen disponible')}</pre>
    </div>
    
    <h3>📊 Métricas Principales</h3>
"""
        
        counts = summary_data.get("counts_total", {})
        for metric in config["metrics"]:
            value = counts.get(metric, 0)
            label = config["metrics_labels"].get(metric, metric)
            html_content += f'<div class="metric"><b>{label}:</b> {value}</div>'
        
        html_content += "<h3>🔔 Eventos Destacados</h3>"
        for event in summary_data.get("notable_events", [])[:5]:
            html_content += f'<div class="event"><b>{event.get("datetime", "")}</b> - {event.get("description", "")[:100]}</div>'
        
        html_content += f"""
    <div class="footer">
        <p><b>Generado por OjoIA</b> - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>
        <p>Reporte automático configurado para envío diario a las 7:30 AM</p>
    </div>
</body>
</html>
"""
        
        pdf_path.write_text(html_content)
        logger.info(f"Reporte generado: {pdf_path}")
        
        return {
            "success": True,
            "pdf_path": str(pdf_path),
            "pdf_url": pdf_url,
            "summary": summary_data,
            "business_type": business_type,
            "business_name": business_name,
            "config_used": config,
            "generated_at": datetime.now().timestamp()
        }
        
    except Exception as e:
        logger.error(f"Error generando reporte: {e}")
        return {"success": False, "error": str(e)}


async def send_daily_report_to_chat(user_id: str, camera_id: str = None, date: str = "today") -> Dict[str, Any]:
    report_result = await generate_daily_report_pdf(user_id, camera_id, date)
    
    if not report_result.get("success"):
        return {"success": False, "error": report_result.get("error")}
    
    config = report_result.get("config_used", SMART_SUMMARY_CONFIG["default"])
    summary = report_result.get("summary", {})
    
    chat_url = f"/chat?user={user_id}"
    message = (
        f"{config['icon']} *{config['title']}*\n\n"
        f"{summary.get('summary', 'Sin resumen disponible')}\n\n"
        f"📄 *Reporte completo:* {report_result.get('pdf_url', 'No disponible')}\n\n"
        f"[💬 Ver en el Chat]({chat_url})\n\n"
        f"_Este reporte se genera automáticamente todos los días a las 7:30 AM_"
    )
    
    return {
        "success": True,
        "message": message,
        "pdf_url": report_result.get("pdf_url"),
        "pdf_path": report_result.get("pdf_path")
    }


async def send_daily_report_push_notification(user_id: str, report_message: str, pdf_url: str = None):
    """
    Envía notificación push FCM del reporte diario (mismo sistema que usa Centinela).
    """
    try:
        # Leer tokens FCM del usuario
        user_file = f"/home/sam/storage/users/{user_id}/user.json"
        if not os.path.exists(user_file):
            return False
        
        with open(user_file) as f:
            user_data = json.load(f)
        
        tokens = user_data.get("fcm_tokens", [])
        business_name = user_data.get("business_name", "Tu negocio")
        
        if not tokens:
            logger.warning(f"Sin tokens FCM para {user_id}")
            return False
        
        # OAuth2 credentials (mismo método que orchestrator)
        from google.oauth2 import service_account
        import google.auth.transport.requests
        import requests as _req
        
        _creds = service_account.Credentials.from_service_account_file(
            "/home/sam/ai_system/firebase-key.json",
            scopes=["https://www.googleapis.com/auth/firebase.messaging"]
        )
        _creds.refresh(google.auth.transport.requests.Request())
        access_token = _creds.token
        
        # Preparar el link (mismo formato que eventos)
        link = "https://ojoia.com.do/#reports"
        if pdf_url:
            link = f"https://ojoia.com.do{pdf_url}" if pdf_url.startswith("/") else pdf_url
        
        sent_count = 0
        for tok in tokens:
            try:
                payload = {
                    "message": {
                        "token": tok,
                        "notification": {
                            "title": "📊 Reporte Diario Disponible",
                            "body": f"Tu reporte de {business_name} está listo para revisar"
                        },
                        "data": {
                            "type": "daily_report",
                            "url": link,
                            "report_message": report_message[:500],
                            "title": "📊 Reporte Diario Disponible",
                            "body": f"Tu reporte de {business_name} está listo para revisar",
                            "tag": "daily_report"
                        },
                        "webpush": {
                            "notification": {
                                "title": "📊 Reporte Diario Disponible",
                                "body": f"Tu reporte de {business_name} está listo para revisar",
                                "icon": "/img/icon-192.png",
                                "badge": "/img/icon-192.png",
                                "require_interaction": True,
                                "tag": "daily_report"
                            },
                            "fcm_options": {"link": link}
                        }
                    }
                }
                
                resp = _req.post(
                    "https://fcm.googleapis.com/v1/projects/ojoia-67216/messages:send",
                    headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
                    json=payload,
                    timeout=10
                )
                
                if resp.status_code == 200:
                    sent_count += 1
                    logger.info(f"FCM reporte enviado a token {tok[:20]}...")
                else:
                    logger.warning(f"FCM error {resp.status_code} token {tok[:20]}: {resp.text[:80]}")
            except Exception as e:
                logger.warning(f"Error con token {tok[:20]}: {e}")
        
        logger.info(f"✅ Reporte push enviado: {sent_count}/{len(tokens)} tokens")
        return sent_count > 0
        
    except Exception as e:
        logger.error(f"Error enviando push de reporte: {e}")
        return False


async def send_full_daily_report(user_id: str, camera_id: str = None, date: str = "yesterday"):
    """
    Pipeline completo: genera el reporte + envía push notification + guarda historial.
    """
    # 1. Generar reporte
    report = await generate_daily_report_pdf(user_id, camera_id, date)
    
    if not report.get("success"):
        return {"success": False, "error": report.get("error")}
    
    # 2. Preparar mensaje
    message = f"""🍽️ *Reporte Diario - {report.get('business_name', 'Tu negocio')}*

📊 Hoy se generaron {report.get('summary', {}).get('total_events', 0)} análisis
📄 [Ver reporte completo](https://ojoia.com.do{report.get('pdf_url', '')})

_Generado automáticamente a las 7:30 AM_"""
    
    # 3. Enviar push notification (FCM)
    push_sent = await send_daily_report_push_notification(
        user_id=user_id,
        report_message=message,
        pdf_url=report.get('pdf_url')
    )
    
    # 4. Guardar en historial
    notifications_dir = f"/home/sam/storage/users/{user_id}/notifications"
    os.makedirs(notifications_dir, exist_ok=True)
    
    notification = {
        "type": "daily_report",
        "user_id": user_id,
        "camera_id": camera_id,
        "sent_at": datetime.now().isoformat(),
        "message": message,
        "pdf_url": report.get('pdf_url'),
        "pdf_path": report.get('pdf_path'),
        "action": "open_reports",
        "push_sent": push_sent,
        "read": False
    }
    
    notif_file = f"{notifications_dir}/report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    with open(notif_file, "w") as f:
        json.dump(notification, f, indent=2, ensure_ascii=False)
    
    return {
        "success": True,
        "report": report,
        "message": message,
        "pdf_url": report.get('pdf_url'),
        "push_sent": push_sent
    }


def inject_report_into_chat(user_id: str, message_content: str, role: str = "assistant") -> bool:
    """
    Inyecta el mensaje del reporte directamente en la sesión activa del chat.
    Cuando el usuario recargue el chat, el mensaje aparecerá.
    También funciona si el chat está abierto (live).
    """
    try:
        user_file = f"/home/sam/storage/users/{user_id}/user.json"
        if not os.path.exists(user_file):
            return False
        
        with open(user_file, "r") as f:
            user_data = json.load(f)
        
        # Inicializar eva_sessions si no existe
        if "eva_sessions" not in user_data:
            user_data["eva_sessions"] = {}
        
        sessions = user_data["eva_sessions"]
        
        # Buscar sesión activa o crear una nueva
        session_id = f"daily_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        
        # Crear la sesión con el reporte
        sessions[session_id] = {
            "messages": [
                {"role": "assistant", "content": message_content, "timestamp": datetime.now().timestamp()},
            ],
            "last_activity": datetime.now().timestamp(),
            "type": "daily_report"
        }
        
        # Guardar usuario actualizado
        with open(user_file, "w") as f:
            json.dump(user_data, f, indent=2, ensure_ascii=False)
        
        logger.info(f"✅ Reporte inyectado en sesión {session_id} para {user_id}")
        return True
        
    except Exception as e:
        logger.error(f"Error inyectando reporte en chat: {e}")
        return False


async def send_full_daily_report_v2(user_id: str, camera_id: str = None, date: str = "yesterday"):
    """
    Pipeline completo: genera reporte + FCM push + inyecta en chat + guarda historial.
    """
    # 1. Generar reporte
    report = await generate_daily_report_pdf(user_id, camera_id, date)
    if not report.get("success"):
        return {"success": False, "error": report.get("error")}
    
    business_name = report.get("business_name", "Tu negocio")
    summary = report.get("summary", {})
    
    # 2. Construir mensaje para chat y push
    chat_message = f"""🍽️ *Reporte Diario - {business_name}*

📊 Análisis realizados: {summary.get('total_events', 0)}
👥 Personas únicas detectadas: {summary.get('persons_total', 0)}

📄 [Ver reporte completo](https://ojoia.com.do{report.get('pdf_url', '')})

_Este reporte se genera automáticamente todos los días a las 7:30 AM_"""
    
    # 3. Inyectar en el chat (para que aparezca al recargar)
    chat_injected = inject_report_into_chat(user_id, chat_message)
    
    # 4. Enviar push FCM (igual que Centinela)
    push_sent = await send_daily_report_push_notification(
        user_id=user_id,
        report_message=chat_message,
        pdf_url=report.get("pdf_url")
    )
    
    # 5. Guardar en historial unificado
    notifications_dir = f"/home/sam/storage/users/{user_id}/notifications"
    os.makedirs(notifications_dir, exist_ok=True)
    
    notification = {
        "type": "daily_report",
        "user_id": user_id,
        "camera_id": camera_id or report.get("camera_id"),
        "sent_at": datetime.now().isoformat(),
        "message": chat_message,
        "pdf_url": report.get("pdf_url"),
        "pdf_path": report.get("pdf_path"),
        "channels_delivered": {
            "chat": chat_injected,
            "push_fcm": push_sent
        },
        "read": False
    }
    
    notif_file = f"{notifications_dir}/report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    with open(notif_file, "w") as f:
        json.dump(notification, f, indent=2, ensure_ascii=False)
    
    return {
        "success": True,
        "report": report,
        "message": chat_message,
        "pdf_url": report.get("pdf_url"),
        "chat_injected": chat_injected,
        "push_sent": push_sent,
        "channels_delivered": notification["channels_delivered"]
    }
