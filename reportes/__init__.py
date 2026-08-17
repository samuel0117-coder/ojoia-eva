"""
reportes - Módulo de reportes automáticos de OjoIA

Funcionalidades:
- Reportes diarios inteligentes según tipo de negocio
- Envío automático programado (7:30 AM configurable)
- PDF/HTML con métricas relevantes
- Panel admin para configuración
"""

from .daily_report import generate_daily_report_pdf, send_daily_report_to_chat, SMART_SUMMARY_CONFIG
from .scheduler import (
    get_user_report_config,
    save_user_report_config,
    send_scheduled_reports,
    start_scheduler,
    test_send_report,
    SCHEDULER_CONFIG
)

__all__ = [
    'generate_daily_report_pdf',
    'send_daily_report_to_chat',
    'SMART_SUMMARY_CONFIG',
    'get_user_report_config',
    'save_user_report_config',
    'send_scheduled_reports',
    'start_scheduler',
    'test_send_report',
    'SCHEDULER_CONFIG'
]
