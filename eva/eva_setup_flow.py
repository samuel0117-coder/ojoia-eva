"""
eva_setup_flow.py — Flujo completo de configuración con Eva.

Estados:
GREETING → BUSINESS_TYPE → CONCERNS → SCHEDULE → RESUMEN → 
CAMERA_CONNECT → CAMERA_SHOW_FRAME → CAMERA_ZONE → CAMERA_TASKS → 
CAMERA_MORE → CAMERA_PROMPT → MORE_CAMERAS → FINALIZAR → CHAT_OS
"""

import json
import time
import logging
from pathlib import Path
from typing import Optional, Dict, List, Any

logger = logging.getLogger(__name__)
STORAGE_ROOT = Path("/home/sam/storage")

# ── Tipos de negocio ──
def extract_business_type(name: str) -> Optional[str]:
    keywords = {
        "finca": "finca", "granja": "granja", "colmado": "colmado",
        "tienda": "tienda", "restaurante": "restaurante", "bodega": "bodega",
        "farmacia": "farmacia", "clinica": "clínica", "hospital": "hospital",
        "banco": "banco", "taller": "taller", "ferreteria": "ferretería",
        "supermercado": "supermercado", "panaderia": "panadería",
        "pizzeria": "pizzería", "barberia": "barbería", "hotel": "hotel",
        "gym": "gimnasio", "oficina": "oficina", "escuela": "escuela",
    }
    for kw, bt in keywords.items():
        if kw in name.lower():
            return bt
    return None

# ── Helpers ──
def is_confirmation(text: str) -> bool:
    return any(w in text.lower() for w in ["sí", "si", "yes", "claro", "correcto", "perfecto", "bueno", "ok", "dale", "va", "listo"])

def parse_schedule(text: str) -> dict:
    import re
    # Buscar patrones como "6am a 6pm", "6:00am a 10:00pm", etc.
    m = re.search(r'(\d{1,2})\s*(?::\s*(\d{2}))?\s*(am|pm)?\s*(?:a|to|-|–)\s*(\d{1,2})\s*(?::\s*(\d{2}))?\s*(am|pm)?', text.lower())
    if m:
        oh, om, oampm, ch, cm, campm = int(m.group(1)), int(m.group(2) or 0), m.group(3), int(m.group(4)), int(m.group(5) or 0), m.group(6)
        # Convertir a 24h
        if oampm == "pm" and oh < 12: oh += 12
        if oampm == "am" and oh == 12: oh = 0
        if campm == "pm" and ch < 12: ch += 12
        if campm == "am" and ch == 12: ch = 0
        # Si no se especificó AM/PM, asumir que hora de cierre es PM si es menor
        if not oampm and not campm:
            if ch <= oh: ch += 12
        return {"open": f"{oh:02d}:{om:02d}", "close": f"{ch:02d}:{cm:02d}"}
    return {"open": "07:00", "close": "19:00"}

def get_latest_frame(user_id: str, cam_id: str) -> dict:
    """Busca el último frame de una cámara."""
    cam_dir = STORAGE_ROOT / "users" / user_id / "cameras" / cam_id
    if not cam_dir.exists():
        # Buscar en todas las cámaras del usuario
        base = STORAGE_ROOT / "users" / user_id / "cameras"
        if base.exists():
            for d in sorted(base.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
                if d.is_dir():
                    for f in d.glob("latest_vigilance.jpg"):
                        return {"has_frame": True, "frame_path": str(f), "cam_id": d.name}
                    for f in sorted(d.glob("events/*.jpg"), key=lambda x: x.stat().st_mtime, reverse=True)[:1]:
                        return {"has_frame": True, "frame_path": str(f), "cam_id": d.name}
        return None
    
    # Buscar latest_vigilance.jpg primero (frame en vivo del ESP32)
    latest = cam_dir / "latest_vigilance.jpg"
    if latest.exists():
        return {"has_frame": True, "frame_path": str(latest), "cam_id": cam_id}
    
    # Si no, buscar frames recientes en events/
    events = sorted(cam_dir.glob("*.jpg"), key=lambda x: x.stat().st_mtime, reverse=True)
    if events:
        return {"has_frame": True, "frame_path": str(events[0]), "cam_id": cam_id}
    
    return None

def generate_camera_prompt(business_name, business_type, zone, tasks, schedule, concerns):
    """Genera el prompt de vigilancia para una cámara."""
    tasks_str = "\n".join(f"  • {t}" for t in tasks) if tasks else "  • Vigilar la zona"
    concerns_str = ", ".join(concerns) if concerns else "seguridad general"
    
    return f"""Eres videointeligencia para {business_name} ({business_type}).
Zona: {zone}.
Horario: {schedule.get('open','07:00')}-{schedule.get('close','19:00')}.

TAREAS DE ESTA CÁMARA:
{tasks_str}

PREOCUPACIONES: {concerns_str}

Analiza estas imágenes. Responde SOLO con JSON:
{{"descripcion":"1-2 oraciones de lo que ves","personas":N,"detalle":[{{"rol":"empleado/cliente/proveedor/desconocido/sospechoso","accion":"qué hace","ropa":"descripción breve","conocido":"nombre o null"}}],"objetos":["objeto1","objeto2"],"actividad":"bajo/medio/alto","anomalia":true/false,"anomalia_detalle":"descripción o null","flujo":"sin_clientes/normal/en_fila/muy_concurrido"}}

INSTRUCCIONES:
- Sé preciso. Sin personas: personas=0, detalle=[].
- "anomalia": true solo si es CLARAMENTE fuera de lo normal.
- "flujo": evalúa movimiento de personas/animales en la zona.
- SOLO el JSON."""

# ── Respuestas de Eva ──
def get_eva_response(phase: str, session: dict, user_message: str) -> dict:
    """Genera la respuesta de Eva según el estado."""
    
    if phase == "GREETING":
        user_msg = user_message.strip()
        if user_msg and len(user_msg) > 1 and user_msg.lower() not in ["hola", "hi", "hey", "buenos", "buenas"]:
            business_name = user_msg
            business_type = extract_business_type(business_name)
            session["business_name"] = business_name
            if business_type:
                return {
                    "response": f"¡{business_name}! 🌿 Ya veo que es {business_type}. ¿Es correcto o es de otro tipo?",
                    "next_phase": "BUSINESS_TYPE",
                    "data": {"business_name": business_name, "business_type_inferred": business_type},
                }
            else:
                return {
                    "response": f"¡{business_name}! ¿Qué tipo de negocio es? Por ejemplo: finca, tienda, restaurante, bodega, clínica, etc.",
                    "next_phase": "BUSINESS_TYPE",
                    "data": {"business_name": business_name},
                }
        return {
            "response": "¡Hola! 👋 Soy Eva, tu asistente de seguridad inteligente. Vamos a configurar tu sistema de vigilancia.\n\n¿Cómo se llama tu negocio?",
            "next_phase": "BUSINESS_NAME",
            "data": {},
        }
    
    elif phase == "BUSINESS_NAME":
        business_name = user_message.strip()
        business_type = extract_business_type(business_name)
        session["business_name"] = business_name
        if business_type:
            return {
                "response": f"¡{business_name}! 🌿 Ya veo que es {business_type}. ¿Es correcto o es de otro tipo?",
                "next_phase": "BUSINESS_TYPE",
                "data": {"business_name": business_name, "business_type_inferred": business_type},
            }
        else:
            return {
                "response": f"¡{business_name}! ¿Qué tipo de negocio es? Por ejemplo: finca, tienda, restaurante, bodega, clínica, etc.",
                "next_phase": "BUSINESS_TYPE",
                "data": {"business_name": business_name},
            }
    
    elif phase == "BUSINESS_TYPE":
        business_type = user_message.strip().lower()
        business_name = session.get("business_name", "tu negocio")
        return {
            "response": f"Perfecto. ¿Cuáles son tus principales preocupaciones de seguridad?\n• ¿Robo o hurto?\n• ¿Vigilar que todo esté en orden?\n• ¿Controlar quién entra?\n• ¿Vigilar empleados?\n• ¿Otra cosa?",
            "next_phase": "CONCERNS",
            "data": {"business_type": business_type},
        }
    
    elif phase == "CONCERNS":
        concerns = [c.strip() for c in user_message.replace(",", "\n").split("\n") if c.strip()]
        business_name = session.get("business_name", "tu negocio")
        return {
            "response": f"Entendido. ¿Cuál es el horario de {business_name}? ¿A qué hora abren y cierran?",
            "next_phase": "SCHEDULE",
            "data": {"concerns": concerns},
        }
    
    elif phase == "SCHEDULE":
        schedule = parse_schedule(user_message)
        business_name = session.get("business_name", "tu negocio")
        business_type = session.get("business_type", "negocio")
        concerns = session.get("concerns", [])
        concerns_str = "\n".join(f"  • {c}" for c in concerns) if concerns else "  • Seguridad general"
        return {
            "response": f"Perfecto. Tengo todo claro:\n\n🏢 {business_name} ({business_type})\n🔒 Preocupaciones:\n{concerns_str}\n⏰ Horario: {schedule.get('open', '07:00')} a {schedule.get('close', '19:00')}\n\n¿Está bien así?",
            "next_phase": "RESUMEN_NEGOCIO",
            "data": {"schedule": schedule},
        }
    
    elif phase == "RESUMEN_NEGOCIO":
        if is_confirmation(user_message):
            return {
                "response": "¡Genial! Ahora vamos a conectar tu primera cámara.\n\n¿Tienes la cámara OjoIA contigo?",
                "next_phase": "CAMERA_CONNECT",
                "data": {},
            }
        else:
            return {
                "response": "Entendido. Vamos a corregir. ¿Qué quieres cambiar?",
                "next_phase": "BUSINESS_NAME",
                "data": {},
            }
    
elif phase == "CAMERA_CONNECT":
        if is_confirmation(user_message):
            cam_count = session.get("camera_count", 0) + 1
            session["camera_count"] = cam_count
            return {
                "response": f"¡Vamos a conectar la cámara {cam_count}!\n\n1. 🔌 Conecta la cámara a la corriente.\n2. ⏳ Espera ~10 segundos hasta que el LED se encienda.\n3. ✅ Cuando veas el LED encendido, dime 'listo'.\n\nTómate tu tiempo. 👍",
                "next_phase": "WIZARD_DISPOSITION",
                "data": {"camera_count": cam_count, "waiting": True},
            }
        else:
            return {
                "response": "No hay prisa. Cuando tengas la cámara conectada, escríbeme 'listo'.",
                "next_phase": "CAMERA_CONNECT",
                "data": {},
            }
    
    elif phase == "WIZARD_DISPOSITION":
        # Verificar si hay frame disponible
        user_id = session.get("user_id", "")
        frame_info = None
        if user_id:
            frame_info = get_latest_frame(user_id, cam_id=None)
        
        if not frame_info and user_message.lower().strip() in ["listo", "ready", "ya", "está listo", "conectada", "ok"]:
            # Aún no hay frame, seguir esperando
            return {
                "response": "Déjame verificar si la cámara ya está enviando imágenes... ⏳",
                "next_phase": "WIZARD_DISPOSITION",
                "data": {"waiting_for_frame": True},
            }
        
        if frame_info and user_message.lower().strip() in ["listo", "ready", "ya", "está listo", "conectada", "ok", "bien", "se ve bien"]:
            session["camera_id"] = frame_info.get("cam_id", "")
            return {
                "response": "¡Excelente! 🎉 La cámara está funcionando.\n\nAhora viene lo más importante: **vamos a definir las zonas de interés** (la caja, la entrada, la cocina, etc.).\n\n👉 Toca el botón de abajo para ir a Configurar Zonas. Dibuja un rectángulo sobre cada área importante y ponle nombre. Cuando termines, regresa aquí y dime 'listo'.",
                "next_phase": "WIZARD_ZONES_DRAW",
                "data": {"show_camera_frame": True, "camera_id": frame_info.get("cam_id", "")},
                "camera_id": frame_info.get("cam_id", ""),
            }
        
        if not frame_info:
             return {
                "response": "¿Ya ves el LED de la cámara encendido? Cuando esté listo, dime 'listo'.",
                "next_phase": "WIZARD_DISPOSITION",
                "data": {"waiting_for_frame": True},
            }
        
        return {
            "response": "¿Ya ves la imagen de la cámara? Cuando estés listo, di 'listo' y pasaremos a configurar las zonas de interés.",
            "next_phase": "WIZARD_DISPOSITION",
            "data": {},
        }
        else:
            return {
                "response": "No hay prisa. Cuando tengas la cámara, escríbeme 'listo'.",
                "next_phase": "CAMERA_CONNECT",
                "data": {},
            }
    
elif phase == "WIZARD_QR":
        if user_message.lower().strip() in ["listo", "ready", "ya", "está listo", "conectada", "ok"]:
            return {
                "response": "¡Perfecto! Ahora, antes de activar Eva, necesito que aceptes que OjoIA procesa imágenes de video para seguridad. ¿Aceptas los términos de uso?",
                "next_phase": "WIZARD_LEGAL",
                "data": {"legal_accepted": True, "claim_token": session.get("claim_token", "")},
                "claim_token": session.get("claim_token", ""),
            }
        else:
            return {
                "response": "Cuando hayas escaneado el QR y la cámara se haya conectado a tu WiFi, escríbeme 'listo'.",
                "next_phase": "WIZARD_QR",
                "data": {"claim_token": session.get("claim_token", "")},
                "claim_token": session.get("claim_token", ""),
            }
        else:
            return {
                "response": "Cuando hayas escaneado el QR y la cámara se haya conectado a tu WiFi, escríbeme 'listo'.",
                "next_phase": "WIZARD_QR",
                "data": {},
            }
    
    elif phase == "WIZARD_LEGAL":
        if is_confirmation(user_message):
            return {
                "response": "¡Gracias! 🙌\n\nAhora veamos lo que la cámara está viendo. Te voy a mostrar la previsualización para que me digas si se ve bien o si hay que mover la cámara.",
                "next_phase": "WIZARD_DISPOSITION",
                "data": {"legal_accepted": True},
            }
        else:
            return {
                "response": "Necesito que aceptes los términos para poder activar el monitoreo. ¿Aceptas? (Sí / No)",
                "next_phase": "WIZARD_LEGAL",
                "data": {},
            }
    
    elif phase == "WIZARD_DISPOSITION":
        # Aquí idealmente se mostraría un frame de la cámara
        frame_info = None
        user_id = session.get("user_id", "")
        if user_id:
            frame_info = get_latest_frame(user_id, cam_id=None)
        if not frame_info and user_message.lower().strip() in ["listo", "ready", "ya", "está listo", "ok", "bien", "se ve bien", "perfecto"]:
            return {
                "response": "¡Excelente! 🎉 Ya tenemos la vista de la cámara.\n\nAhora viene lo más importante: **vamos a definir las zonas de interés** (la caja, la entrada, la cocina, etc.).\n\n👉 Ve a la pestaña **Cámara**, pulsa sobre la imagen, elige 'Agregar zona' y dibuja un rectángulo con tu dedo. Cuando termines, regresa aquí y dime 'listo'.",
                "next_phase": "WIZARD_ZONES_DRAW",
                "data": {"show_camera_frame": True},
            }
        if not frame_info:
             return {
                "response": "¿Ya ves la imagen de la cámara? ¿Se ve bien o necesita moverse/rotarse? (Indícame si está oscuro, de lado, o si algo obstruye la vista)",
                "next_phase": "WIZARD_DISPOSITION",
                "data": {"waiting_for_frame": True},
            }
        return {
            "response": "¿Ya ves la imagen de la cámara? Cuando estés listo, di 'listo' y pasaremos a configurar las zonas de interés.",
            "next_phase": "WIZARD_DISPOSITION",
            "data": {},
        }
    
    elif phase == "WIZARD_ZONES_DRAW":
        # El frontend mostrará el overlay para dibujar zonas
        # Simplemente esperamos a que el usuario regrese
        if user_message.lower().strip() in ["listo", "ready", "ya", "está listo", "ok", "terminé", "terminado", "hecho"]:
            # Verificar si tiene zonas
            user_id = session.get("user_id", "")
            zones = []
            if user_id:
                from camera_zones import get_camera_zones
                zones = get_camera_zones(user_id, session.get("camera_id", ""))
            zone_str = f" {len(zones)} zonas dibujadas" if zones else ""
            return {
                "response": f"¡Perfecto!{zone_str} ¡Eva ya está vigilando!\n\nTe avisaré si algo importante pasa. Puedes volver al chat en cualquier momento y preguntarme '¿qué pasó hoy?' o 'muéstrame las alertas'.\n\n¿Tienes otra cámara para configurar?",
                "next_phase": "MORE_CAMERAS",
                "data": {"zones_count": len(zones)},
            }
        else:
            return {
                "response": "Regresa cuando hayas dibujado las zonas en la pestaña Cámara (agrega la caja, la entrada, la cocina, etc.) y dime 'listo'.",
                "next_phase": "WIZARD_ZONES_DRAW",
                "data": {"camera_id": session.get("camera_id", "")},
                "camera_id": session.get("camera_id", ""),
            }
            }
    
    elif phase == "CAMERA_SHOW_FRAME":
        if user_message.lower().strip() in ["listo", "ready", "ya", "está listo", "conectada"]:
            cam_count = session.get("camera_count", 1)
            
            # Buscar el latest_vigilance.jpg de cualquier cámara del usuario
            # El usuario aún no tiene cámara asignada, buscar en cámaras conocidas
            user_id = session.get("user_id", "")
            frame_info = None
            
            # Buscar en todas las cámaras del usuario
            if user_id:
                frame_info = get_latest_frame(user_id, f"cam_{cam_count:03d}")
                if not frame_info:
                    # Buscar en cámaras ESP32 existentes
                    base = STORAGE_ROOT / "users" / user_id / "cameras"
                    if base.exists():
                        for d in sorted(base.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
                            if d.is_dir():
                                latest = d / "latest_vigilance.jpg"
                                if latest.exists():
                                    frame_info = {"has_frame": True, "frame_path": str(latest), "cam_id": d.name}
                                    break
            
            if frame_info and frame_info.get("has_frame"):
                return {
                    "response": f"¡Excelente! 🎉 La cámara {cam_count} está funcionando.\n\n📷 [CÁMARA CONECTADA - Frame disponible]\n\n¿Dónde pusiste esta cámara? Por ejemplo: caja, almacén, entrada, sala, patio, corral, etc.",
                    "next_phase": "CAMERA_ZONE",
                    "data": {"show_camera_frame": True, "frame_path": frame_info["frame_path"], "cam_id": frame_info.get("cam_id", f"cam_{cam_count:03d}")},
                    "show_camera_frame": True,
                }
            else:
                return {
                    "response": f"¡Perfecto! La cámara {cam_count} está conectada.\n\n¿Dónde pusiste esta cámara? Por ejemplo: caja, almacén, entrada, sala, patio, corral, etc.\n\n(El frame de la cámara se mostrará cuando empiece a transmitir)",
                    "next_phase": "CAMERA_ZONE",
                    "data": {},
                }
        else:
            return {
                "response": "Cuando la cámara esté conectada y veas el LED fijo, escríbeme 'listo'.",
                "next_phase": "CAMERA_SHOW_FRAME",
                "data": {},
            }
    
    elif phase == "CAMERA_ZONE":
        zone = user_message.strip()
        cam_count = session.get("camera_count", 1)
        return {
            "response": f"Perfecto. ¿Qué quieres que vigile esta cámara en {zone}?\n\nPor ejemplo:\n• Contar animales y avisar si falta alguno\n• Ver si están bien (pastando, enfermos, etc.)\n• Detectar personas que entren\n• Contar sacos de alimento\n• Todo lo anterior\n• Otra cosa",
            "next_phase": "CAMERA_TASKS",
            "data": {"camera_zone": zone},
        }
    
    elif phase == "CAMERA_TASKS":
        tasks = [t.strip() for t in user_message.replace(",", "\n").split("\n") if t.strip()]
        cam_count = session.get("camera_count", 1)
        zone = session.get("camera_zone", "la zona")
        return {
            "response": f"¿Algo más importante para esta cámara en {zone}?",
            "next_phase": "CAMERA_MORE",
            "data": {"camera_tasks": tasks},
        }
    
    elif phase == "CAMERA_MORE":
        cam_count = session.get("camera_count", 1)
        zone = session.get("camera_zone", "la zona")
        tasks = session.get("camera_tasks", [])
        if user_message.lower().strip() not in ["no", "nada", "no nada", "eso es todo", "listo"]:
            tasks.append(user_message.strip())
        
        business_name = session.get("business_name", "el negocio")
        business_type = session.get("business_type", "negocio")
        schedule = session.get("schedule", {"open": "07:00", "close": "19:00"})
        concerns = session.get("concerns", [])
        
        prompt = generate_camera_prompt(business_name, business_type, zone, tasks, schedule, concerns)
        
        return {
            "response": f"¡Listo! ✅ Voy a crear el sistema de vigilancia para {zone}.\n\n[Generando...]\n✓ Prompt de cámara creado\n✓ Vigilancia activa\n✓ Alertas configuradas\n\nCada vez que detecte algo importante, te avisaré aquí.\n\n¿Tienes otra cámara para configurar?",
            "next_phase": "MORE_CAMERAS",
            "data": {"camera_configured": True, "camera_prompt": prompt, "camera_zone": zone, "camera_tasks": tasks, "camera_id": f"cam_{cam_count:03d}"},
            "camera_configured": True,
        }
    
    elif phase == "MORE_CAMERAS":
        if is_confirmation(user_message):
            cam_count = session.get("camera_count", 0) + 1
            session["camera_count"] = cam_count
            return {
                "response": f"¡Vamos con la cámara {cam_count}! ¿Dónde la vas a poner?",
                "next_phase": "CAMERA_ZONE",
                "data": {"camera_count": cam_count},
            }
        else:
            cam_count = session.get("camera_count", 1)
            return {
                "response": f"¡Tu sistema está listo! 🎉\n\nRESUMEN:\n{cam_count} cámara(s) configurada(s)\n\n¿QUÉ PUEDES HACER?\n• '¿Cuántas vacas hay ahora?'\n• '¿Viste algo raro hoy?'\n• 'Muéstrame la cámara del corral'\n• Te avisaré automáticamente si detecto algo\n\n¿Tienes alguna pregunta?",
                "next_phase": "CHAT_OS",
                "data": {"configuracion_completa": True},
            }
    
    elif phase == "CHAT_OS":
        # Chat normal - no debería llegar aquí desde el setup
        return {
            "response": "¿En qué puedo ayudarte?",
            "next_phase": "CHAT_OS",
            "data": {},
            "use_chat_os": True,
        }
    
    return {"response": "No entendí bien. ¿Puedes repetir?", "next_phase": phase, "data": {}}


SETUP_PHASES = [
    "GREETING", "BUSINESS_TYPE", "CONCERNS", "SCHEDULE", "RESUMEN",
    "CAMERA_CONNECT", "CAMERA_SHOW_FRAME", "CAMERA_ZONE", "CAMERA_TASKS",
    "CAMERA_MORE", "CAMERA_PROMPT", "MORE_CAMERAS", "FINALIZAR", "CHAT_OS"
]
