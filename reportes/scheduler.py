"""
reportes/scheduler.py - Scheduler para envío automático de reportes diarios

Envía reportes automáticos a las 7:30 AM (configurable) a través del chat de Eva.
"""

import json
import asyncio
import logging
from pathlib import Path
from datetime import datetime, time, timedelta
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)
STORAGE_ROOT = Path("/home/sam/storage")

# Configuración global del scheduler
SCHEDULER_CONFIG = {
    "default_hour": 7,
    "default_minute": 30,
    "retry_attempts": 3,
    "retry_delay_minutes": 5,
    "enabled": True
}


async def get_user_report_config(user_id: str) -> Dict[str, any]:
    """
    Obtiene configuración de reportes para un usuario.
    """
    config_file = STORAGE_ROOT / "users" / user_id / "business" / "report_config.json"
    
    if config_file.exists():
        config = json.loads(config_file.read_text())
        return {
            "enabled": config.get("enabled", True),
            "hour": config.get("hour", SCHEDULER_CONFIG["default_hour"]),
            "minute": config.get("minute", SCHEDULER_CONFIG["default_minute"]),
            "cameras": config.get("cameras", []),  # Lista de camera_id o [] para todas
            "recipients": config.get("recipients", []),  # Emails adicionales
            "format": config.get("format", "html"),  # html o pdf
            "last_sent": config.get("last_sent"),
            "total_sent": config.get("total_sent", 0)
        }
    
    # Configuración default
    return {
        "enabled": True,
        "hour": SCHEDULER_CONFIG["default_hour"],
        "minute": SCHEDULER_CONFIG["default_minute"],
        "cameras": [],
        "recipients": [],
        "format": "html",
        "last_sent": None,
        "total_sent": 0
    }


async def save_user_report_config(user_id: str, config: Dict[str, any]) -> bool:
    """
    Guarda configuración de reportes para un usuario.
    """
    try:
        config_dir = STORAGE_ROOT / "users" / user_id / "business"
        config_dir.mkdir(parents=True, exist_ok=True)
        
        config_file = config_dir / "report_config.json"
        
        # Leer configuración existente
        existing = {}
        if config_file.exists():
            existing = json.loads(config_file.read_text())
        
        # Actualizar con nuevos valores
        existing.update(config)
        existing["last_updated"] = datetime.now().timestamp()
        
        # Guardar
        config_file.write_text(json.dumps(existing, indent=2, ensure_ascii=False))
        logger.info(f"Configuración de reportes guardada para {user_id}")
        return True
        
    except Exception as e:
        logger.error(f"Error guardando config de reportes: {e}")
        return False


async def send_scheduled_reports():
    """
    Envía reportes programados a todos los usuarios configurados.
    Se ejecuta diariamente a la hora configurada.
    """
    logger.info("🕐 Iniciando envío de reportes programados...")
    
    # Obtener todos los usuarios con configuración
    users_dir = STORAGE_ROOT / "users"
    if not users_dir.exists():
        logger.warning("Directorio de usuarios no existe")
        return
    
    current_time = datetime.now()
    current_hour = current_time.hour
    current_minute = current_time.minute
    
    sent_count = 0
    failed_count = 0
    
    for user_dir in users_dir.iterdir():
        if not user_dir.is_dir():
            continue
        
        user_id = user_dir.name
        
        try:
            # Obtener configuración del usuario
            config = await get_user_report_config(user_id)
            
            # Verificar si está habilitado y es la hora
            if not config.get("enabled", True):
                continue
            
            config_hour = config.get("hour", SCHEDULER_CONFIG["default_hour"])
            config_minute = config.get("minute", SCHEDULER_CONFIG["default_minute"])
            
            # Verificar si es la hora de enviar (con margen de 2 minutos)
            time_match = (
                (current_hour == config_hour) and 
                (abs(current_minute - config_minute) <= 2)
            )
            
            if not time_match:
                continue
            
            logger.info(f"📧 Enviando reporte a {user_id} (config: {config_hour}:{config_minute:02d})")
            
            # Obtener cámaras a reportar
            cameras = config.get("cameras", [])
            if not cameras:
                # Si no hay cámaras específicas, obtener todas las activas
                cameras = await _get_user_cameras(user_id)
            
            # Generar y enviar reporte por cada cámara
            for camera_id in cameras:
                result = await _send_report_for_camera(user_id, camera_id)
                
                if result.get("success"):
                    sent_count += 1
                    # Actualizar estadísticas
                    config["total_sent"] = config.get("total_sent", 0) + 1
                    config["last_sent"] = datetime.now().isoformat()
                    await save_user_report_config(user_id, config)
                else:
                    failed_count += 1
                    logger.error(f"Error enviando reporte {user_id}/{camera_id}: {result.get('error')}")
                    
        except Exception as e:
            logger.error(f"Error procesando usuario {user_id}: {e}")
            failed_count += 1
    
    logger.info(f"✅ Reportes enviados: {sent_count}, Fallidos: {failed_count}")
    return {"sent": sent_count, "failed": failed_count}


async def _send_report_for_camera(user_id: str, camera_id: str) -> Dict[str, any]:
    """
    Genera y envía reporte para una cámara específica al chat de Eva.
    """
    try:
        from .daily_report import send_daily_report_to_chat
        
        # Generar reporte
        report = await send_daily_report_to_chat(
            user_id=user_id,
            camera_id=camera_id,
            date="yesterday"  # Reporte del día anterior
        )
        
        if not report.get("success"):
            return report
        
        # Enviar al chat de Eva (simulado - en producción usar cola de mensajes)
        message = report.get("message", "")
        pdf_url = report.get("pdf_url", "")
        
        logger.info(f"📨 Reporte enviado a chat de {user_id} para cámara {camera_id}")
        logger.info(f"   PDF: {pdf_url}")
        logger.info(f"   Mensaje: {message[:100]}...")
        
        # TODO: En producción, enviar realmente al chat vía:
        # - Cola de mensajes (Redis/RabbitMQ)
        # - O llamar directamente al endpoint de chat
        
        return {
            "success": True,
            "message": message,
            "pdf_url": pdf_url,
            "camera_id": camera_id
        }
        
    except Exception as e:
        logger.error(f"Error enviando reporte: {e}")
        return {"success": False, "error": str(e)}


async def _get_user_cameras(user_id: str) -> List[str]:
    """
    Obtiene lista de cámaras activas para un usuario.
    """
    cameras = []
    cameras_dir = STORAGE_ROOT / "users" / user_id / "cameras"
    
    if not cameras_dir.exists():
        return cameras
    
    for cam_dir in cameras_dir.iterdir():
        if not cam_dir.is_dir():
            continue
        
        camera_file = cam_dir / "camera.json"
        if camera_file.exists():
            try:
                camera_data = json.loads(camera_file.read_text())
                if camera_data.get("active", True):
                    cameras.append(cam_dir.name)
            except:
                pass
    
    return cameras


async def start_scheduler():
    """
    Inicia el scheduler en background.
    Verifica cada minuto si es hora de enviar reportes.
    """
    logger.info("⏰ Iniciando scheduler de reportes diarios...")
    
    while True:
        try:
            # Verificar si es hora de enviar
            now = datetime.now()
            
            # Solo verificar entre 7:00 AM y 8:00 AM (margen de 1 hora)
            if 7 <= now.hour <= 8:
                await send_scheduled_reports()
            
            # Dormir 1 minuto
            await asyncio.sleep(60)
            
        except Exception as e:
            logger.error(f"Error en scheduler: {e}")
            await asyncio.sleep(60)


# Función para probar manualmente el envío
async def test_send_report(user_id: str, camera_id: str = None):
    """
    Prueba envío de reporte manualmente.
    """
    logger.info(f"🧪 Probando envío de reporte para {user_id}...")
    
    if not camera_id:
        cameras = await _get_user_cameras(user_id)
        camera_id = cameras[0] if cameras else None
    
    if not camera_id:
        return {"success": False, "error": "No hay cámaras disponibles"}
    
    result = await _send_report_for_camera(user_id, camera_id)
    
    if result.get("success"):
        logger.info("✅ Prueba exitosa!")
        logger.info(f"   Mensaje: {result.get('message', '')[:200]}")
        logger.info(f"   PDF: {result.get('pdf_url')}")
    else:
        logger.error(f"❌ Prueba fallida: {result.get('error')}")
    
    return result
