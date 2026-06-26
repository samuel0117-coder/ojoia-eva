"""
eva/auto_config.py — Auto-generación de configuración de cámara.

Filosofía: Eva ve la imagen + conoce el negocio → genera reglas automáticamente.
El usuario NO escribe reglas. Solo confirma o ajusta.

Una sola llamada a Qwen. Un solo endpoint. Una sola pantalla.
"""
import json
import logging
import re as _re
import time
from pathlib import Path
from typing import Any, Dict, Optional

import httpx

logger = logging.getLogger(__name__)

QWEN_URL     = "http://localhost:8004/v1/chat/completions"
QWEN_TIMEOUT = 90


# ── Fallback templates por tipo de negocio ──────────────────────────────────
_FALLBACK_TEMPLATES = {
    "colmado": {
        "zone": "área de mostrador y caja",
        "rules_es": [
            "Persona entra al área del cajero",
            "Producto se entrega sin funda o bolsa",
            "Empleado con manos cerca de la caja registradora",
        ],
        "rules_en": [
            "Is anyone crossing into the employee area behind the counter?",
            "Is any product handed to customer without a bag?",
            "Is anyone's hand moving near the cash register suspiciously?",
        ],
        "scanner_question": "Is there any unauthorized person behind the counter or suspicious activity near the cash register?",
        "system_prompt": (
            "Security camera at a colmado cash area. "
            "Alert if: someone crosses into employee area, product given without bag, "
            "suspicious hand movements near register. "
            "Outside business hours: any person = immediate alert."
        ),
    },
    "retail": {
        "zone": "área de mostrador y caja",
        "rules_es": [
            "Persona entra al área del cajero",
            "Producto se entrega sin funda o bolsa",
            "Empleado con manos cerca de la caja registradora",
        ],
        "rules_en": [
            "Is anyone crossing into the employee area behind the counter?",
            "Is any product handed to customer without a bag?",
            "Is anyone's hand moving near the cash register suspiciously?",
        ],
        "scanner_question": "Is there any unauthorized person behind the counter or suspicious activity near the cash register?",
        "system_prompt": (
            "Security camera at a retail cash area. "
            "Alert if: someone crosses into employee area, product given without bag, "
            "suspicious hand movements near register. "
            "Outside business hours: any person = immediate alert."
        ),
    },
    "farmacia": {
        "zone": "mostrador de farmacia",
        "rules_es": [
            "Persona entra al área del farmacéutico",
            "Alguien accede a medicamentos sin supervisión",
            "Movimiento fuera de horario",
        ],
        "rules_en": [
            "Is any customer crossing into the pharmacist area?",
            "Is anyone accessing medication shelves without staff supervision?",
            "Is there any person present outside business hours?",
        ],
        "scanner_question": "Is there any unauthorized person behind the counter or accessing medication areas?",
        "system_prompt": (
            "Security camera at a pharmacy counter. "
            "Alert if: customer enters employee area, unauthorized medication access, "
            "any person outside business hours."
        ),
    },
    "finca": {
        "zone": "área de pastoreo y corral",
        "rules_es": [
            "Persona en el área fuera del horario laboral",
            "Animal fuera de su cercado",
            "Vehículo no autorizado en la finca",
        ],
        "rules_en": [
            "Is there any person in the pasture or enclosure area outside work hours?",
            "Is any animal outside its designated enclosure area?",
            "Is there any unauthorized vehicle in the farm area?",
        ],
        "scanner_question": "Is there any unauthorized person or animal outside its enclosure in this farm area?",
        "system_prompt": (
            "Security camera at a farm/pasture area. "
            "During hours: monitor animal containment. "
            "After hours: any person = immediate alert."
        ),
    },
    "restaurante": {
        "zone": "área de cocina y mostrador",
        "rules_es": [
            "Persona en la cocina después de cerrar",
            "Plato entregado sin ticket visible",
            "Dos o más empleados en área restringida simultáneamente",
        ],
        "rules_en": [
            "Is anyone in the kitchen area after closing time?",
            "Is any food dish handed to customer without a visible ticket?",
            "Are there two or more people in the employee-only area simultaneously?",
        ],
        "scanner_question": "Is there any unauthorized person in the kitchen area or any food served without a ticket?",
        "system_prompt": (
            "Security camera at a restaurant kitchen/counter area. "
            "Alert if: person in kitchen after hours, food without ticket, "
            "multiple people in restricted area."
        ),
    },
}


def _get_fallback(biz_type: str) -> dict:
    """Obtener template de fallback según tipo de negocio."""
    biz = biz_type.lower().strip()
    for key, template in _FALLBACK_TEMPLATES.items():
        if key in biz:
            return template
    # Genérico
    return {
        "zone": "zona principal",
        "rules_es": [
            "Persona en área restringida",
            "Movimiento sospechoso en la zona",
            "Actividad fuera de horario laboral",
        ],
        "rules_en": [
            "Is anyone in a restricted area without authorization?",
            "Is there any suspicious activity in this area?",
            "Is there any person present outside business hours?",
        ],
        "scanner_question": "Is there any suspicious activity or unauthorized person in this area?",
        "system_prompt": (
            f"Security camera monitoring {biz_type}. "
            "Alert on suspicious activity, restricted area access, or presence outside business hours."
        ),
    }


async def auto_generate_config(
    user_id: str,
    image_b64: str,
    camera_id: str,
    storage_root: Path,
) -> dict:
    """
    Genera configuración completa de cámara en 1 llamada a Qwen.
    
    Retorna dict listo para que el frontend muestre:
    - imagen de la cámara
    - 3 reglas en español
    - botones [✅ Listo] [✏️ Ajustar]
    """
    # Cargar datos del usuario
    user_file = storage_root / "users" / user_id / "user.json"
    biz_type = "negocio"
    biz_name = "tu negocio"
    schedule = {"open": "07:00", "close": "19:00"}
    if user_file.exists():
        try:
            ud = json.loads(user_file.read_text())
            biz_type = ud.get("business_type", biz_type)
            biz_name = ud.get("business_name", biz_name)
            schedule = ud.get("schedule", schedule)
        except Exception:
            pass

    # Si es edición, cargar reglas existentes para mejorarlas
    existing_rules = []
    if camera_id and camera_id != "unknown":
        cam_file = storage_root / "users" / user_id / "cameras" / camera_id / "camera.json"
        if cam_file.exists():
            try:
                cfg = json.loads(cam_file.read_text())
                existing_rules = cfg.get("rules_es", [])
                if not biz_type or biz_type == "negocio":
                    biz_type = cfg.get("business_type", biz_type)
                if not schedule or schedule == {"open": "07:00", "close": "19:00"}:
                    schedule = cfg.get("schedule", schedule)
            except Exception:
                pass

    is_editing = len(existing_rules) > 0

    # Prompt para Qwen
    editing_context = ""
    if is_editing:
        editing_context = f"""
REGLAS ACTUALES (que el usuario quiere mejorar):
{chr(10).join(f"  - {r}" for r in existing_rules[:3])}

Genera reglas NUEVAS que reemplacen las anteriores. 
Las nuevas reglas deben ser VISUALMENTE VERIFICABLES en la imagen.
No incluyas reglas sobre facturas, recibos, uniformes u otros elementos 
que una cámara no puede ver claramente.
"""

    prompt = f"""You are a security expert configuring a surveillance camera.

BUSINESS: {biz_name} ({biz_type})
BUSINESS HOURS: {schedule.get('open','07:00')} to {schedule.get('close','19:00')}
{'EDITING MODE: User wants to improve existing rules.' if is_editing else 'NEW CAMERA: First time setup.'}
{editing_context}

Analyze the camera image and generate VISUAL security rules.

RULES (IMPORTANT):
- Maximum 3 rules
- Each rule MUST be visually observable by a camera (no abstract concepts like "invoice", "permission", "uniform")
- Focus on: body positions, movements, areas, objects — things a camera CAN see
- Outside business hours: any person = immediate alert (automatic, don't include as rule)

RETURN ONLY THIS JSON:
{{
  "zone": "identified zone in Spanish (one word, e.g. caja, entrada, mostrador, cocina, corral)",
  "eva_message": "1-2 sentence warm description in Spanish of what you see and what you'll monitor",
  "rules": [
    {{"es": "short observable rule 1 in Spanish", "en": "Is/Are visual question 1 in English?"}},
    {{"es": "short observable rule 2 in Spanish", "en": "Is/Are visual question 2 in English?"}},
    {{"es": "short observable rule 3 in Spanish", "en": "Is/Are visual question 3 in English?"}}
  ],
  "scanner_question": "primary visual check in English (most important rule as yes/no question)",
  "system_prompt": "2-sentence security context in English: what the camera sees, what to alert on, business hours"
}}"""

    config = None
    try:
        async with httpx.AsyncClient(timeout=QWEN_TIMEOUT) as cl:
            r = await cl.post(QWEN_URL, json={
                "model": "qwen",
                "messages": [{"role": "user", "content": [
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}},
                    {"type": "text", "text": prompt}
                ]}],
                "max_tokens": 400,
            })
            if r.status_code == 200:
                raw = r.json()["choices"][0]["message"]["content"].strip()
                m = _re.search(r'\{.*\}', raw, _re.DOTALL)
                if m:
                    config = json.loads(m.group())
                    logger.info(f"Auto-config generated for {biz_type}: {len(config.get('rules',[]))} rules")
    except Exception as e:
        logger.error(f"Auto-config Qwen failed: {e}")

    if not config or not config.get("rules"):
        # Usar template de fallback
        logger.warning(f"Using fallback template for {biz_type}")
        fallback = _get_fallback(biz_type)
        config = {
            "zone": fallback["zone"],
            "eva_message": f"Veo el área de {fallback['zone']}. Esto es lo que voy a vigilar:",
            "rules": [
                {"es": fallback["rules_es"][i], "en": fallback["rules_en"][i]}
                for i in range(min(3, len(fallback["rules_es"])))
            ],
            "scanner_question": fallback["scanner_question"],
            "system_prompt": fallback["system_prompt"],
        }

    # Asegurar que reglas tengan campos es/en
    cleaned_rules = []
    for rule in config.get("rules", []):
        if isinstance(rule, dict):
            cleaned_rules.append({
                "es": rule.get("es", ""),
                "en": rule.get("en", rule.get("es", "")),
            })

    return {
        "camera_id": camera_id,
        "zone": config.get("zone", "zona principal"),
        "eva_message": config.get("eva_message", "Configuración lista para tu cámara."),
        "rules": cleaned_rules[:3],
        "rules_es": [r["es"] for r in cleaned_rules[:3]],
        "rules_en": [r["en"] for r in cleaned_rules[:3]],
        "scanner_question": config.get("scanner_question", ""),
        "system_prompt": config.get("system_prompt", ""),
        "schedule": schedule,
        "yolo_triggers": _yolo_triggers_for_biz(biz_type),
        "grid_size": 8 if any(w in biz_type.lower() for w in ["finca", "granja", "agricultura"]) else 12,
        "cooldown_min": 5,
        "night_mode": True,
        "is_editing": is_editing,
    }


def _yolo_triggers_for_biz(biz_type: str) -> list:
    """YOLO triggers según tipo de negocio."""
    biz = biz_type.lower()
    if any(w in biz for w in ["finca", "granja", "agricultura"]):
        return ["person", "cow", "horse", "bird", "dog", "cat", "sheep"]
    if any(w in biz for w in ["parqueo", "parking", "estacion"]):
        return ["person", "car", "motorcycle", "truck", "bus"]
    return ["person"]
