"""
page_generator.py - Genera páginas HTML y PDF de reportes diarios
"""

import json
import os
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, Any, Optional
import logging

logger = logging.getLogger(__name__)
STORAGE_ROOT = Path("/home/sam/storage")
REPORTS_DIR = STORAGE_ROOT / "report_pages"
REPORTS_DIR.mkdir(parents=True, exist_ok=True)

# Dominio público donde se sirven los reportes (API, no Firebase Hosting SPA)
BASE_URL = os.environ.get("OJOIA_REPORTS_BASE_URL", "https://api.ojoia.com.do")


def _resolve_date_str(date: str) -> str:
    """Resuelve 'today', 'yesterday' o fecha ISO a una fecha real."""
    today = datetime.now().date()
    if date == "today":
        return today.strftime("%Y-%m-%d")
    if date == "yesterday":
        return (today - timedelta(days=1)).strftime("%Y-%m-%d")
    return date or today.strftime("%Y-%m-%d")


def _compute_cutoff_range(date: str, cutoff_hour: Optional[int] = None):
    """Devuelve (start_ts, end_ts) para el corte del reporte.

    - Si cutoff_hour es None (default): ventana full-day 00:00–23:59 del día base.
    - Si cutoff_hour se especifica: ventana de 24h que termina a las cutoff_hour
      del día base (ej. cutoff_hour=12 con date='yesterday' cubre desde
      12:00 PM de hace 2 días hasta 12:00 PM de ayer).
    """
    today = datetime.now().date()
    if date == "today":
        base = today
    elif date == "yesterday":
        base = today - timedelta(days=1)
    else:
        try:
            base = datetime.strptime(date, "%Y-%m-%d").date()
        except Exception:
            base = today
    day_start = datetime.combine(base, datetime.min.time())
    if cutoff_hour is None:
        start_dt = day_start
        end_dt = day_start + timedelta(days=1)
    else:
        end_dt = day_start + timedelta(hours=cutoff_hour)
        start_dt = end_dt - timedelta(days=1)
    return start_dt.timestamp(), end_dt.timestamp()

def generate_report_html(user_id: str, report_data: Dict, date: str) -> str:
    """
    Genera página HTML completa con gráficos Chart.js
    """
    business_name = report_data.get("business_name", "Tu negocio")
    business_type = report_data.get("business_type", "default")
    summary = report_data.get("summary", {})
    date_str = _resolve_date_str(date)
    
    # Extraer métricas
    total_events = summary.get("total_events", 0)
    persons_total = summary.get("persons_total", 0)
    counts = summary.get("counts_total", {})
    notable_events = summary.get("notable_events", [])[:10]
    
    # Icono según tipo de negocio
    icons = {
        "restaurant": ("🍽️", "Restaurante"),
        "farmacia": ("💊", "Farmacia"),
        "retail": ("🛒", "Tienda"),
        "office": ("🏢", "Oficina"),
        "default": ("📊", "Reporte")
    }
    icon, biz_label = icons.get(business_type, icons["default"])
    
    # Preparar datos para gráficos
    event_labels = []
    event_counts = {}
    for evt in notable_events:
        hour = "N/A"
        dt = evt.get("datetime", "")
        if "T" in dt:
            hour = dt.split("T")[-1][:5]
        elif " " in dt:
            hour = dt.split(" ")[-1][:5]
        event_counts[hour] = event_counts.get(hour, 0) + 1
    for hour, count in sorted(event_counts.items()):
        event_labels.append(hour)
    event_values = [event_counts[h] for h in event_labels]
    
    html_url = f"{BASE_URL}/reportes/{user_id}/reporte_{date_str}.html"
    pdf_url = f"{BASE_URL}/reportes/{user_id}/reporte_{date_str}.pdf"
    
    html = f"""<!DOCTYPE html>
<html lang="es">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Reporte Diario - {business_name}</title>
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); min-height: 100vh; padding: 20px; color: #333; }}
        .container {{ max-width: 900px; margin: 0 auto; background: white; border-radius: 20px; padding: 40px; box-shadow: 0 20px 60px rgba(0,0,0,0.3); }}
        .header {{ text-align: center; margin-bottom: 40px; padding-bottom: 30px; border-bottom: 3px solid #667eea; }}
        .header h1 {{ font-size: 2.5rem; color: #1a1a1a; margin-bottom: 10px; }}
        .header .icon {{ font-size: 4rem; margin-bottom: 10px; }}
        .header p {{ color: #666; font-size: 1.1rem; }}
        .stats-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 20px; margin-bottom: 40px; }}
        .stat-card {{ background: linear-gradient(135deg, #f5f7fa 0%, #c3cfe2 100%); padding: 25px; border-radius: 15px; text-align: center; }}
        .stat-value {{ font-size: 2.5rem; font-weight: bold; color: #667eea; }}
        .stat-label {{ color: #666; margin-top: 5px; font-size: 0.9rem; }}
        .chart-container {{ margin: 30px 0; padding: 20px; background: #f8f9fa; border-radius: 15px; }}
        .events-list {{ margin-top: 30px; }}
        .event-item {{ padding: 15px; margin: 10px 0; background: #f8f9fa; border-left: 4px solid #667eea; border-radius: 8px; }}
        .event-time {{ font-weight: bold; color: #667eea; }}
        .event-desc {{ color: #555; margin-top: 5px; }}
        .footer {{ text-align: center; margin-top: 40px; padding-top: 20px; border-top: 2px solid #eee; color: #999; font-size: 0.85rem; }}
        .btn-download {{ display: inline-block; margin-top: 20px; padding: 15px 30px; background: #667eea; color: white; text-decoration: none; border-radius: 10px; font-weight: bold; }}
        .btn-download:hover {{ background: #5568d3; }}
        .links {{ text-align: center; margin: 20px 0; }}
        .links a {{ display: inline-block; margin: 0 10px; color: #667eea; text-decoration: none; font-weight: 500; }}
        .links a:hover {{ text-decoration: underline; }}
        @media (max-width: 600px) {{ .container {{ padding: 20px; }} .header h1 {{ font-size: 1.8rem; }} }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <div class="icon">{icon}</div>
            <h1>Reporte Diario</h1>
            <p><b>{business_name}</b> ({biz_label}) • {date_str}</p>
        </div>
        
        <div class="stats-grid">
            <div class="stat-card">
                <div class="stat-value">{total_events:,}</div>
                <div class="stat-label">📊 Análisis realizados</div>
            </div>
            <div class="stat-card">
                <div class="stat-value">{persons_total:,}</div>
                <div class="stat-label">👥 Personas únicas</div>
            </div>
            <div class="stat-card">
                <div class="stat-value">{len(notable_events)}</div>
                <div class="stat-label">🔔 Eventos registrados</div>
            </div>
        </div>
        
        <div class="links">
            <a href="{pdf_url}">📥 Descargar PDF</a>
            <a href="https://ojoia.com.do/#chat">💬 Abrir chat</a>
        </div>
        
        <div class="chart-container">
            <canvas id="eventsChart"></canvas>
        </div>
        
        <div class="events-list">
            <h2 style="margin-bottom: 20px; color: #1a1a1a;">📋 Últimos Eventos</h2>
"""
    
    for evt in notable_events:
        dt = evt.get("datetime", "N/A")
        desc = evt.get("description", "")[:150]
        html += f"""
            <div class="event-item">
                <div class="event-time">🕐 {dt}</div>
                <div class="event-desc">{desc}</div>
            </div>
"""
    
    html += f"""
        </div>
        
        <div style="text-align: center;">
            <a href="{pdf_url}" class="btn-download">📥 Descargar PDF</a>
        </div>
        
        <div class="footer">
            <p><b>Generado por OjoIA</b> - Sistema de seguridad inteligente</p>
            <p>Reporte generado el {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>
        </div>
    </div>
    
    <script>
        const ctx = document.getElementById('eventsChart').getContext('2d');
        new Chart(ctx, {{
            type: 'bar',
            data: {{
                labels: {json.dumps(event_labels)},
                datasets: [{{
                    label: 'Eventos por hora',
                    data: {json.dumps(event_values)},
                    backgroundColor: 'rgba(102, 126, 234, 0.6)',
                    borderColor: 'rgba(102, 126, 234, 1)',
                    borderWidth: 2,
                    borderRadius: 8
                }}]
            }},
            options: {{
                responsive: true,
                plugins: {{
                    legend: {{ display: false }},
                    title: {{ display: true, text: 'Actividad del día', font: {{ size: 16 }} }}
                }},
                scales: {{
                    y: {{ beginAtZero: true, ticks: {{ stepSize: 1 }} }},
                    x: {{ grid: {{ display: false }} }}
                }}
            }}
        }});
    </script>
</body>
</html>
"""
    return html


async def generate_report_page(user_id: str, date: str = "yesterday", camera_id: str = None, cutoff_hour: Optional[int] = None) -> Dict[str, Any]:
    """
    Genera página HTML y PDF del reporte.
    Devuelve URLs públicas accesibles (servidas por api.ojoia.com.do).
    Por defecto reporta el día completo (00:00–23:59); cutoff_hour activa
    una ventana móvil de 24h terminando a esa hora.
    """
    try:
        # 1. Calcular rango de corte (default: día completo)
        start_ts, end_ts = _compute_cutoff_range(date, cutoff_hour)
        
        # 2. Obtener datos del reporte con el corte
        from .daily_report import generate_daily_report_pdf
        report = await generate_daily_report_pdf(user_id, camera_id, date, start_ts=start_ts, end_ts=end_ts)
        
        if not report.get("success"):
            return report
        
        # 3. Generar HTML
        date_str = _resolve_date_str(date)
        html_content = generate_report_html(user_id, report, date_str)
        
        # 4. Guardar HTML
        page_dir = REPORTS_DIR / user_id
        page_dir.mkdir(parents=True, exist_ok=True)
        
        html_file = page_dir / f"reporte_{date_str}.html"
        html_file.write_text(html_content, encoding='utf-8')
        
        # 5. Generar PDF con reportlab
        pdf_file = await _generate_pdf_from_report(report, page_dir, date_str)
        
        # 6. URLs públicas reales (servidas por api.ojoia.com.do, no Firebase Hosting)
        html_url = f"{BASE_URL}/reportes/{user_id}/reporte_{date_str}.html"
        pdf_url = f"{BASE_URL}/reportes/{user_id}/reporte_{date_str}.pdf"
        
        return {
            "success": True,
            "html_url": html_url,
            "pdf_url": pdf_url,
            "html_path": str(html_file),
            "pdf_path": str(pdf_file),
            "report": report,
            "date_str": date_str,
            "start_ts": start_ts,
            "end_ts": end_ts
        }
        
    except Exception as e:
        logger.error(f"Error generando página: {e}")
        import traceback
        traceback.print_exc()
        return {"success": False, "error": str(e)}


async def _generate_pdf_from_report(report: Dict, page_dir: Path, date_str: str) -> Path:
    """
    Genera PDF usando reportlab.
    """
    from reportlab.lib.pagesizes import letter
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
    from reportlab.lib import colors
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    
    pdf_file = page_dir / f"reporte_{date_str}.pdf"
    doc = SimpleDocTemplate(str(pdf_file), pagesize=letter)
    styles = getSampleStyleSheet()
    
    story = []
    
    # Título
    title_style = ParagraphStyle('CustomTitle', parent=styles['Heading1'], fontSize=24, textColor=colors.HexColor('#667eea'), spaceAfter=30)
    story.append(Paragraph(f"📊 Reporte Diario - {report.get('business_name', 'Negocio')}", title_style))
    story.append(Spacer(1, 12))
    
    # Fecha
    story.append(Paragraph(f"<b>Fecha:</b> {date_str}", styles['Normal']))
    story.append(Spacer(1, 20))
    
    # Métricas
    summary = report.get("summary", {})
    metrics_data = [
        ["📊 Análisis realizados", str(summary.get("total_events", 0))],
        ["👥 Personas únicas", str(summary.get("persons_total", 0))],
        ["🔔 Eventos", str(len(summary.get("notable_events", [])))]
    ]
    
    metrics_table = Table(metrics_data, colWidths=[300, 100])
    metrics_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), colors.HexColor('#f5f7fa')),
        ('TEXTCOLOR', (0, 0), (-1, -1), colors.black),
        ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
        ('FONTNAME', (0, 0), (-1, -1), 'Helvetica'),
        ('FONTSIZE', (0, 0), (-1, -1), 12),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 12),
        ('TOPPADDING', (0, 0), (-1, -1), 12),
    ]))
    story.append(metrics_table)
    story.append(Spacer(1, 30))
    
    # Eventos
    story.append(Paragraph("<b>📋 Últimos Eventos:</b>", styles['Heading2']))
    story.append(Spacer(1, 12))
    
    for evt in summary.get("notable_events", [])[:10]:
        dt = evt.get("datetime", "N/A")
        desc = evt.get("description", "")[:200]
        story.append(Paragraph(f"<b>{dt}</b><br/>{desc}", styles['Normal']))
        story.append(Spacer(1, 8))
    
    # Footer
    story.append(Spacer(1, 30))
    footer_style = ParagraphStyle('Footer', parent=styles['Normal'], fontSize=10, textColor=colors.grey)
    story.append(Paragraph(f"Generado por OjoIA - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", footer_style))
    
    doc.build(story)
    return pdf_file
