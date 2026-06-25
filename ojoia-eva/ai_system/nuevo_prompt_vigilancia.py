"""
nuevo_prompt_vigilancia.py — Nuevo sistema de análisis de frames con Qwen.

Este módulo define:
1. El prompt que se envía a Qwen para analizar grids de frames
2. El esquema del business.json donde se guardan los datos
3. La lógica para procesar la respuesta de Qwen y actualizar el business.json

FILOSOFÍA:
- NO usamos reglas fijas binarias (alerta/no alerta)
- Qwen analiza el grid y devuelve un JSON estructurado con lo que ve
- Los datos se acumulan en el business.json
- Eva consulta esos datos para responder preguntas
- Las alertas se generan por anomalías detectadas por Qwen, no por reglas predefinidas
"""

import json
import time
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, List, Any

logger = logging.getLogger(__name__)

STORAGE_ROOT = Path("/home/sam/storage")


# ═══════════════════════════════════════════════════════════════
# 1. PROMPT DE VIGILANCIA — Se envía a Qwen con cada grid
# ═══════════════════════════════════════════════════════════════

def build_vigilance_prompt(
    business_name: str,
    business_type: str,
    zone: str,
    schedule_open: str,
    schedule_close: str,
    is_after_hours: bool,
    known_people: List[Dict] = None,
    concerns: List[str] = None,
) -> str:
    """
    Construye el prompt que se envía a Qwen para analizar un grid de frames.
    
    El prompt está diseñado para que Qwen devuelva un JSON estructurado
    con datos útiles sobre lo que ve en las imágenes.
    """
    
    # Formatear personas conocidas
    people_str = ""
    if known_people:
        people_str = "PERSONAS CONOCIDAS:\n"
        for p in known_people:
            name = p.get("name", "Desconocido")
            role = p.get("role", "")
            tags = ", ".join(p.get("visual_tags", []))
            people_str += f"  • {name} ({role}): {tags}\n"
    
    # Formatear preocupaciones
    concerns_str = ""
    if concerns:
        concerns_str = "PREOCUPACIONES DEL NEGOCIO:\n"
        for c in concerns:
            concerns_str += f"  • {c}\n"
    
    # Nota de horario
    hours_note = ""
    if is_after_hours:
        hours_note = "\n⚠️ ATENCIÓN: Fuera de horario comercial. Cualquier persona presente es sospechosa.\n"
    
    return f"""Eres un sistema de videointeligencia para {business_name} ({business_type}).
Analizas imágenes de seguridad de la zona: {zone}.
Horario comercial: {schedule_open} a {schedule_close}.{hours_note}
{concerns_str}{people_str}
Analiza estas {16} frames (grid) y responde SOLO con un JSON válido:

{{
  "descripcion": "Descripción breve de lo que ves (1-2 oraciones en español)",
  "personas": NÚMERO_TOTAL_DE_PERSONAS,
  "personas_detalle": [
    {{
      "rol": "empleado/cliente/proveedor/desconocido/sospechoso",
      "accion": "qué está haciendo (cobrando, comprando, organizando, esperando, caminando, etc)",
      "ropa": "descripción de ropa visible",
      "identificado": "nombre si es conocido, o null"
    }}
  ],
  "objetos_relevantes": ["producto", "caja_registradora", "bolsa", "vehiculo", "animal", etc],
  "nivel_actividad": "bajo/medio/alto",
  "anomalia": true/false,
  "anomalia_detalle": "descripción de la anomalía si la hay, o null",
  "flujo_clientes": "sin_clientes/en_fila/compra_activa/normal"
}}

INSTRUCCIONES:
- Sé preciso. Si no ves personas, personas=0 y personas_detalle=[].
- Para "rol": empleado=trabaja ahí, cliente=compra, proveedor=entrega, desconocido=no identificable, sospechoso=comportamiento inusual.
- Para "accion": describe específicamente qué hace cada persona.
- "anomalia": true solo si ves algo claramente fuera de lo normal (persona sospechosa, comportamiento extraño, objeto inusual, fuera de horario).
- "flujo_clientes": evalúa si hay clientes y cómo se mueven.
- Responde SOLO el JSON, sin texto adicional.
"""


# ═══════════════════════════════════════════════════════════════
# 2. ESQUEMA DEL BUSINESS.JSON — Datos que Eva consulta
# ═══════════════════════════════════════════════════════════════

BUSINESS_JSON_SCHEMA = {
    "user_id": "string",
    "business_name": "string",
    "business_type": "string (colmado/tienda/restaurante/bodega/otro)",
    "owner": {
        "name": "string",
        "phone": "string",
        "email": "string"
    },
    "schedule": {
        "open": "HH:MM",
        "close": "HH:MM"
    },
    "main_concerns": ["lista de preocupaciones de seguridad"],
    
    "cameras": {
        "cam_id": {
            "name": "string",
            "zone": "string",
            "active": True,
            "last_frame_ts": 0,
            "prompt_vigilancia": "prompt personalizado para esta cámara",
            "today_summary": {
                "date": "YYYY-MM-DD",
                "total_events": 0,
                "total_personas": 0,
                "alertas": 0,
                "pico_hora": "HH:MM",
                "pico_personas": 0,
                "qwen_analisis": [
                    {
                        "hora": "HH:MM",
                        "descripcion": "string",
                        "personas": 0,
                        "personas_detalle": [],
                        "nivel_actividad": "bajo/medio/alto",
                        "anomalia": False,
                        "flujo_clientes": "normal"
                    }
                ]
            }
        }
    },
    
    "people": {
        "known": [
            {
                "id": "string",
                "name": "string",
                "role": "empleado/dueno/proveedor",
                "visual_tags": ["delantal azul", "camisa blanca"],
                "patterns": {
                    "usual_arrival": "HH:MM",
                    "usual_departure": "HH:MM",
                    "common_zone": "string"
                },
                "first_seen": "timestamp",
                "reliability_score": 0.0
            }
        ],
        "suspicious": [
            {
                "id": "string",
                "visual_tags": ["gorra negra", "camisa negra"],
                "first_seen": "timestamp",
                "last_seen": "timestamp",
                "incidents": 0,
                "notes": "string"
            }
        ]
    },
    
    "daily_summaries": {
        "YYYY-MM-DD": {
            "date": "YYYY-MM-DD",
            "total_events": 0,
            "total_personas": 0,
            "alertas": 0,
            "pico_hora": "HH:MM",
            "pico_personas": 0,
            "cameras_data": {
                "cam_name": {
                    "eventos": 0,
                    "personas": 0,
                    "alertas": 0,
                    "momentos_clave": ["string"]
                }
            },
            "highlights": ["string"],
            "generated_at": "ISO timestamp"
        }
    },
    
    "conversation_context": {
        "weaknesses": ["string"],
        "agreed_rules": ["string"],
        "last_chat_summary": "string"
    }
}


# ═══════════════════════════════════════════════════════════════
# 3. PROCESAMIENTO DE RESPUESTA DE QWEN
# ═══════════════════════════════════════════════════════════════

def parse_qwen_response(text: str) -> Optional[Dict]:
    """Parsea la respuesta JSON de Qwen."""
    if not text:
        return None
    
    text = text.strip()
    
    # Intentar parsear directamente
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return normalize_qwen_data(data)
    except Exception:
        pass
    
    # Buscar JSON dentro del texto
    import re
    m = re.search(r'\{[^{}]*"descripcion"[^{}]*\}', text, re.DOTALL)
    if m:
        try:
            data = json.loads(m.group())
            return normalize_qwen_data(data)
        except Exception:
            pass
    
    # Buscar cualquier JSON
    m = re.search(r'\{[\s\S]*\}', text)
    if m:
        try:
            data = json.loads(m.group())
            if isinstance(data, dict):
                return normalize_qwen_data(data)
        except Exception:
            pass
    
    return None


def normalize_qwen_data(data: Dict) -> Dict:
    """Normaliza la respuesta de Qwen al formato estándar."""
    return {
        "descripcion": data.get("descripcion", data.get("description", "")),
        "personas": data.get("personas", data.get("persons", 0)),
        "personas_detalle": data.get("personas_detalle", data.get("persons_details", [])),
        "objetos_relevantes": data.get("objetos_relevantes", data.get("relevant_objects", [])),
        "nivel_actividad": data.get("nivel_actividad", data.get("activity_level", "medio")),
        "anomalia": data.get("anomalia", data.get("anomaly", False)),
        "anomalia_detalle": data.get("anomalia_detalle", data.get("anomaly_detail", None)),
        "flujo_clientes": data.get("flujo_clientes", data.get("customer_flow", "normal")),
    }


# ═══════════════════════════════════════════════════════════════
# 4. ACTUALIZACIÓN DEL BUSINESS.JSON
# ═══════════════════════════════════════════════════════════════

def load_business_json(user_id: str) -> Dict:
    """Carga o crea el business.json del usuario."""
    bp = STORAGE_ROOT / "users" / user_id / "business.json"
    if bp.exists():
        with open(bp) as f:
            return json.load(f)
    return create_empty_business(user_id)


def save_business_json(user_id: str, data: Dict):
    """Guarda el business.json atómicamente."""
    bp = STORAGE_ROOT / "users" / user_id / "business.json"
    bp.parent.mkdir(parents=True, exist_ok=True)
    tmp = bp.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    tmp.replace(bp)


def create_empty_business(user_id: str) -> Dict:
    """Crea un business.json vacío con el schema correcto."""
    return {
        "user_id": user_id,
        "business_name": "",
        "business_type": "",
        "owner": {"name": "", "phone": "", "email": ""},
        "schedule": {"open": "07:00", "close": "19:00"},
        "main_concerns": [],
        "cameras": {},
        "people": {"known": [], "suspicious": []},
        "daily_summaries": {},
        "conversation_context": {"weaknesses": [], "agreed_rules": [], "last_chat_summary": ""}
    }


def update_camera_analysis(user_id: str, camera_id: str, qwen_data: Dict):
    """
    Actualiza el business.json con un nuevo análisis de Qwen.
    Se llama cada vez que Qwen analiza un grid.
    """
    business = load_business_json(user_id)
    today = datetime.now().strftime("%Y-%m-%d")
    now = datetime.now().strftime("%H:%M")
    
    # Asegurar que la cámara existe
    if camera_id not in business.get("cameras", {}):
        business.setdefault("cameras", {})[camera_id] = {
            "name": camera_id,
            "zone": "",
            "active": True,
            "last_frame_ts": int(time.time()),
            "prompt_vigilancia": "",
            "today_summary": {
                "date": today,
                "total_events": 0,
                "total_personas": 0,
                "alertas": 0,
                "pico_hora": None,
                "pico_personas": 0,
                "qwen_analisis": []
            }
        }
    
    cam = business["cameras"][camera_id]
    summary = cam.setdefault("today_summary", {
        "date": today, "total_events": 0, "total_personas": 0,
        "alertas": 0, "pico_hora": None, "pico_personas": 0, "qwen_analisis": []
    })
    
    # Resetear si es un nuevo día
    if summary.get("date") != today:
        summary = {
            "date": today, "total_events": 0, "total_personas": 0,
            "alertas": 0, "pico_hora": None, "pico_personas": 0, "qwen_analisis": []
        }
        cam["today_summary"] = summary
    
    # Actualizar contadores
    summary["total_events"] = summary.get("total_events", 0) + 1
    personas = qwen_data.get("personas", 0)
    summary["total_personas"] = summary.get("total_personas", 0) + personas
    
    if qwen_data.get("anomalia"):
        summary["alertas"] = summary.get("alertas", 0) + 1
    
    # Actualizar pico
    if personas > summary.get("pico_persons", 0):
        summary["pico_hora"] = now
        summary["pico_personas"] = personas
    
    # Guardar análisis de Qwen
    analisis = {
        "hora": now,
        "descripcion": qwen_data.get("descripcion", ""),
        "personas": personas,
        "personas_detalle": qwen_data.get("personas_detalle", []),
        "nivel_actividad": qwen_data.get("nivel_actividad", "medio"),
        "anomalia": qwen_data.get("anomalia", False),
        "anomalia_detalle": qwen_data.get("anomalia_detalle", None),
        "flujo_clientes": qwen_data.get("flujo_clientes", "normal"),
    }
    summary.setdefault("qwen_analisis", []).append(analisis)
    
    # Actualizar timestamp
    cam["last_frame_ts"] = int(time.time())
    
    save_business_json(user_id, business)
    logger.info(f"Business actualizado: {camera_id} | {personas} personas | anomalia={qwen_data.get('anomalia')}")
