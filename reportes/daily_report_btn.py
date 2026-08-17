"""
Versión mejorada del reporte con botón de descarga
"""
import json
import os
import time
from pathlib import Path
from datetime import datetime
from typing import Dict, Any

STORAGE_ROOT = Path("/home/sam/storage")

async def generate_daily_report_with_button(user_id: str, camera_id: str = None, date: str = "yesterday") -> Dict[str, Any]:
    """
    Genera reporte diario con BOTÓN DE DESCARGA (no solo link).
    El botón se renderiza como HTML en el chat.
    """
    try:
        # 1. Generar el PDF/HTML
        from .daily_report import generate_daily_report_pdf
        report = await generate_daily_report_pdf(user_id, camera_id, date)
        
        if not report.get("success"):
            return report
        
        # 2. Obtener info del negocio
        business_name = report.get("business_name", "Tu negocio")
        summary = report.get("summary", {})
        pdf_url = report.get("pdf_url", "")
        pdf_path = report.get("pdf_path", "")
        
        # 3. Construir mensaje CON BOTÓN HTML
        # El chat renderizará esto como HTML si detecta que es un botón
        message = f"""🍽️ *Reporte Diario - {business_name}*

📊 Análisis realizados: {summary.get('total_events', 0)}
👥 Personas únicas detectadas: {summary.get('persons_total', 0)}

📄 *Reporte completo disponible*

[📥 Descargar reporte PDF]({pdf_url if pdf_url.startswith('http') else 'https://ojoia.com.do' + pdf_url})

_Este reporte se genera automáticamente todos los días a las 7:30 AM_"""
        
        # 4. Alternativa: mensaje con HTML embebido (si el chat lo soporta)
        html_message = f"""
<div style="background:rgba(44,44,46,0.92);border-radius:12px;padding:16px;margin:8px 0;">
  <div style="font-size:16px;font-weight:600;margin-bottom:8px;">🍽️ Reporte Diario - {business_name}</div>
  <div style="font-size:14px;margin:4px 0;">📊 Análisis: {summary.get('total_events', 0)}</div>
  <div style="font-size:14px;margin:4px 0;">👥 Personas: {summary.get('persons_total', 0)}</div>
  <a href="{pdf_url if pdf_url.startswith('http') else 'https://ojoia.com.do' + pdf_url}" 
     download 
     style="display:inline-block;margin-top:12px;padding:10px 20px;background:#4a90e2;color:white;text-decoration:none;border-radius:8px;font-weight:600;">
    📥 Descargar reporte PDF
  </a>
</div>
"""
        
        return {
            "success": True,
            "message": message,
            "html_message": html_message,
            "pdf_url": pdf_url,
            "pdf_path": pdf_path,
            "report": report
        }
        
    except Exception as e:
        return {"success": False, "error": str(e)}
