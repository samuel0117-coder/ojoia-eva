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
    
    message = f"""{config['icon']} *{config['title']}*

{summary.get('summary', 'Sin resumen disponible')}

📄 *Reporte completo:* {report_result.get('pdf_url', 'No disponible')}

_Este reporte se genera automáticamente todos los días a las 7:30 AM_"""
    
    return {
        "success": True,
        "message": message,
        "pdf_url": report_result.get("pdf_url"),
        "pdf_path": report_result.get("pdf_path")
    }
