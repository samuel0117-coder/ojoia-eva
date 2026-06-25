"""
eva/eva_chat.py — OjoIA Eva v14

Arquitectura:
- Máquina de estados determinista
- GREET/HARDWARE/WAIT_IMAGE/ANALYZE — sin cambios (flujo hardware)
- PROBLEM — captura preocupación + 2 preguntas de contexto del negocio
- RULES — Qwen diseña 3 reglas + system_prompt en UNA llamada (imagen + user.json + chat)
- REVIEW — usuario acepta/rechaza/modifica cada regla individual
- CONFIRM — resumen final + guardar
"""
import base64
import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
import re as _re

from .state_machine import EvaPhase, is_yes, is_no, is_hardware_done
from .camera_builder import build_camera_config, save_camera_config
from .knowledge_base import (
    get_scene_profile, get_checks_for_scene, VISUAL_CHECK_TEMPLATES,
)

# Palabras que indican reglas abstractas (no verificables visualmente)
_ABSTRACT_KEYWORDS = [
    "factura", "recibo", "ticket", "permiso", "autorización", "autorizacion",
    "uniforme", "identificación", "identificacion", "documento", "registro",
    "papel", "comprobante", "firma", "contraseña", "password",
]

# Mapeo de reglas abstractas → alternativas visuaes
_ABSTRACT_TO_VISUAL = {
    "factura": "¿El empleado interactúa con la caja registradora antes de entregar el producto?",
    "recibo": "¿El empleado toca o mira la caja registradora antes de la entrega?",
    "ticket": "¿El empleado interactúa con la caja antes de entregar producto al cliente?",
    "permiso": "¿La persona está en un área restringida sin ser empleado?",
    "autorizacion": "¿La persona está en un área restringida sin ser empleado?",
    "autorizada": "¿La persona está en un área restringida sin ser empleado?",
    "uniforme": "¿La persona lleva ropa de trabajo (delantal, chaleco) o es civil?",
    "identificacion": "¿La persona lleva visible una identificación o gafete?",
}


def _is_abstract_rule(text: str) -> bool:
    """Detecta si una regla es abstracta (no verificable visualmente)."""
    t = text.lower()
    return any(kw in t for kw in _ABSTRACT_KEYWORDS)


def _suggest_visual_alternative(text: str) -> str:
    """Sugiere alternativa visual para una regla abstracta."""
    t = text.lower()
    for keyword, suggestion in _ABSTRACT_TO_VISUAL.items():
        if keyword in t:
            return suggestion
    return "¿Se ve algún comportamiento físico específico que la cámara pueda detectar?"


def _validate_rules_against_knowledge_base(rules: list, business_type: str, zone: str) -> list:
    """
    Valida reglas propuestas contra la knowledge base.
    - Marca reglas abstractas como no verificables
    - Sugiere alternativas visuales
    - Filtra reglas que no aplican a la escena
    Retorna lista de reglas limpiadas con metadatos.
    """
    profile = get_scene_profile(business_type, zone)
    validated = []

    for rule in rules:
        if isinstance(rule, dict):
            es = rule.get("es", rule.get("en", str(rule)))
        else:
            es = str(rule)

        is_abstract = _is_abstract_rule(es)
        suggestion = _suggest_visual_alternative(es) if is_abstract else None

        validated.append({
            "es": es,
            "en": rule.get("en", es) if isinstance(rule, dict) else es,
            "abstract": is_abstract,
            "suggestion": suggestion,
            "from_knowledge_base": False,
        })

    return validated

# Validación simple de config
def _validate_camera_config(config: dict) -> bool:
    return bool(config.get("camera_id") and config.get("zone"))

logger = logging.getLogger(__name__)

QWEN_URL     = "http://localhost:8004/v1/chat/completions"
QWEN_TIMEOUT = 90
MAX_TOKENS   = 200
MAX_HISTORY  = 8      # últimos N mensajes en el contexto del LLM

# ── Frame buffer ──────────────────────────────────────────────────────────────
_latest_frame: Optional[bytes] = None
_latest_frame_time: float = 0
_orchestrator = None

def set_orchestrator(orch):
    global _orchestrator
    _orchestrator = orch

def ingest_frame_for_eva(frame_bytes: bytes):
    global _latest_frame, _latest_frame_time
    _latest_frame = frame_bytes
    _latest_frame_time = time.time()

# ── Sesiones ──────────────────────────────────────────────────────────────────
_sessions: Dict[str, Dict[str, Any]] = {}

def get_user_sessions(user_id: str) -> Dict[str, Dict]:
    return {sid: s for sid, s in _sessions.items() if s.get("user_id") == user_id}

def destroy_session(session_id: str):
    _sessions.pop(session_id, None)

def _get_session(session_id: str, storage_root: Path = None) -> Optional[Dict]:
    # Buscar en memoria primero
    s = _sessions.get(session_id)
    if s:
        return s
    # Si no está en memoria, buscar en disco
    if storage_root:
        return _load_session_from_disk(session_id, storage_root)
    return None

def _get_camera_config(user_id: str, camera_id: str, storage_root: Path) -> Dict:
    """Leer config de una cámara desde camera.json o user.json."""
    cam_file = storage_root / "users" / user_id / "cameras" / camera_id / "camera.json"
    if cam_file.exists():
        try:
            return json.loads(cam_file.read_text())
        except Exception:
            pass
    # Fallback: buscar en user.json → cameras[]
    try:
        user_file = storage_root / "users" / user_id / "user.json"
        if user_file.exists():
            ud = json.loads(user_file.read_text())
            for c in ud.get("cameras", []):
                if c.get("camera_id") == camera_id:
                    return c
    except Exception:
        pass
    return {}

def _load_user_data(user_id: str, storage_root: Path) -> Dict:
    for path in [
        storage_root / "users" / user_id / "user.json",
        storage_root / user_id / "user.json",
    ]:
        if path.exists():
            try:
                return json.loads(path.read_text())
            except Exception:
                pass
    return {}

def _create_session(session_id: str, user_id: str,
                    storage_root: Path, cam_id: str = "") -> Dict:
    ud = _load_user_data(user_id, storage_root)
    s = {
        "session_id":     session_id,
        "user_id":        user_id,
        "phase":          EvaPhase.GREET.value,
        # datos del negocio
        "owner_name":     ud.get("name", "amigo"),
        "business_name":  ud.get("business_name", "tu negocio"),
        "business_type":  ud.get("business_type", "negocio"),
        "schedule":       ud.get("schedule", {"open":"08:00","close":"22:00"}),
        # configuración de la cámara
        "zone":           "",
        "concern":        "",
        # reglas Qwen
        "confirmed_rules":[],
        "rejected_rules": [],
        "qwen_rules":     [],
        "qwen_system_prompt": "",
        "qwen_scanner_question": "",
        "pending_rule":   None,
        "review_index":   0,
        # contexto del negocio (preguntas de PROBLEM)
        "business_answers":  [],
        "camera_id":      cam_id,
        "camera_connected": False,
        "position_confirmed": False,
        # imagen
        "image_b64":      "",
        "image_desc":     "",
        # hardware wizard
        "hardware_step":  0,
        # historial para el LLM
        "msgs":           [],
        "created_at":     time.time(),
        "last_activity":  time.time(),
    }
    # Si es edición de cámara existente, cargar datos y saltar a RULES directo
    if cam_id and cam_id != "unknown":
        cam_cfg = _get_camera_config(user_id, cam_id, storage_root)
        if cam_cfg.get("zone"):
            s["zone"] = cam_cfg["zone"]
            s["camera_id"] = cam_id
            s["camera_connected"] = True
            s["position_confirmed"] = True
            s["_editing"] = True
            # Cargar reglas en español primero, fallback a rules (inglés)
            existing_rules = cam_cfg.get("rules_es", []) or cam_cfg.get("rules", [])
            if existing_rules:
                cleaned_rules = []
                for r in existing_rules:
                    if isinstance(r, dict):
                        es = r.get("es", r.get("en", str(r)))
                        cleaned_rules.append({"es": es})
                    else:
                        cleaned_rules.append({"es": str(r).strip()})
                s["confirmed_rules"] = cleaned_rules
            # Cargar métricas si existen
            if "metrics" in cam_cfg:
                s["metrics"] = cam_cfg["metrics"]
            else:
                s["metrics"] = {"total_events": 0, "total_alerts": 0, "total_false_positives": 0, "rules": {}, "needs_review": False}
            existing_prompt = cam_cfg.get("system_prompt", "")
            if existing_prompt:
                s["qwen_system_prompt"] = existing_prompt
            existing_sq = cam_cfg.get("scanner_question", "")
            if existing_sq:
                s["qwen_scanner_question"] = existing_sq
            existing_concern = cam_cfg.get("conversation_context", "")
            if existing_concern:
                s["concern"] = existing_concern
            # Saltar directo a RULES para mejorar reglas existentes
            s["phase"] = EvaPhase.RULES.value
    _sessions[session_id] = s
    return s

# ── Helpers LLM ───────────────────────────────────────────────────────────────

async def _qwen(messages: list, max_tok: int = MAX_TOKENS) -> str:
    try:
        async with httpx.AsyncClient(timeout=QWEN_TIMEOUT) as cl:
            r = await cl.post(
                QWEN_URL,
                json={"model":"qwen","messages":messages,"max_tokens":max_tok}
            )
            if r.status_code == 200:
                return r.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        logger.error(f"Qwen error: {e}")
    return "Disculpa, tuve un momento. ¿Puedes repetir?"


def _resize(img: bytes, max_px: int = 400) -> str:
    try:
        from gateway_resize import resize_image
        img = resize_image(img, max_size=max_px)
    except Exception:
        pass
    return base64.b64encode(img).decode()


def _get_frame() -> Optional[bytes]:
    global _latest_frame
    # Frame del buffer de Eva (sin filtro YOLO)
    if _latest_frame and (time.time() - _latest_frame_time < 60):
        return _latest_frame
    # Fallback al orquestador
    if _orchestrator:
        try:
            return _orchestrator.grid.get_last_frame_bytes()
        except Exception:
            pass
    return None


async def _describe_image(b64: str) -> str:
    """Análisis experto de la imagen: objetos de seguridad, ángulo, zonas ciegas."""
    try:
        small = _resize(base64.b64decode(b64), 500)
        msgs = [{"role": "user", "content": [
            {"type":"image_url","image_url":{"url":f"data:image/jpeg;base64,{small}"}},
            {"type":"text","text":(
                "Analiza esta imagen de cámara de seguridad. Responde SOLO en formato JSON:\n"
                "{\n"
                "  \"zone\": \"qué zona ves (caja/entrada/almacén/corral/cocina/etc)\",\n"
                "  \"objects\": [\"lista de objetos de seguridad relevantes que ves: "
                "cash_register, counter, products, door, fence, gate, shelf, "
                "animal_area, vehicle_entry, etc\"],\n"
                "  \"angle_quality\": \"1-5 (1=mala, 5=excelente)\",\n"
                "  \"blind_spots\": [\"zonas que NO se ven bien o quedan ocultas\"],\n"
                "  \"light\": \"buena/regular/mala\",\n"
                "  \"risks_visible\": [\"riesgos inmediatos que observas en la imagen\"]\n"
                "}"
            )}
        ]}]
        return await _qwen(msgs, 150)
    except Exception:
        return ""


async def _propose_rule(session: Dict) -> Dict[str, str]:
    """
    Generar regla específica basada en base de datos de reglas.
    Evita repetir reglas ya aceptadas, rechazadas o propuestas recientemente.
    """
    from .rule_engine import suggest_rules
    zone = session.get("zone", "zona")
    biz_type = session.get("business_type", "negocio")
    concern = session.get("concern", "")
    confirmed = session.get("confirmed_rules", [])
    rejected = session.get("_rejected_rules", [])
    
    # Convert rejected strings to dict format for filtering
    rejected_dicts = [{"es": r, "en": ""} if isinstance(r, str) else r for r in rejected]
    all_excluded = confirmed + rejected_dicts
    
    # Obtener sugerencias filtradas
    suggestions = suggest_rules(
        business_type=biz_type,
        concern=concern,
        scene_desc=session.get("image_desc", ""),
        zone=zone,
        confirmed_rules=all_excluded,
        max_rules=3
    )
    
    if suggestions:
        return suggestions[0]
    
    # Fallback
    return {"es": f"Vigilar actividad sospechosa en {zone}", "en": f"Is there any suspicious activity in {zone}?"}


async def _analyze_position(session: Dict) -> str:
    """Eva analiza la imagen con datos de scene_analysis y da opinión experta."""
    first     = session["owner_name"].split()[0]
    zone      = session.get("zone","la zona")
    biz_type  = session.get("business_type","negocio")
    img_b64   = session.get("image_b64","")
    scene     = session.get("scene_analysis","")

    if not img_b64:
        return (f"✅ La cámara quedó conectada, {first}.\n\n"
                f"No recibí la imagen todavía. "
                f"¿La posición en {zone} está bien? ¿Puedes ver el área que quieres vigilar?")

    small = _resize(base64.b64decode(img_b64), 500)
    scene_info = f"\nDatos del análisis: {scene}" if scene else ""
    sys_p = (
        f"Eres Eva, asistente de seguridad de OjoIA. Dominicana, directa. Máximo 4 líneas.\n"
        f"Dueño: {first}. Negocio: {biz_type}. Zona: {zone}.{scene_info}\n\n"
        f"Analiza la imagen y da tu opinión de experta:\n"
        f"1. ¿Es el área correcta ({zone})? ¿Se ve bien lo que hay vigilar?\n"
        f"2. ¿Hay problemas: reflejo, ángulo malo, zonas oscuras, obstáculos?\n"
        f"3. Si hay problema: sugiere ajuste específico ('inclina 10° hacia abajo').\n"
        f"4. Si está bien: confirmalo y di que es una buena posición."
    )
    msgs = [
        {"role":"system","content":sys_p},
        {"role":"user","content":[
            {"type":"image_url","image_url":{"url":f"data:image/jpeg;base64,{small}"}},
            {"type":"text","text":"¿Qué ves? ¿La posición está bien?"}
        ]}
    ]
    return await _qwen(msgs, MAX_TOKENS)


# ── Pasos de hardware (sin LLM — respuestas fijas) ───────────────────────────
HARDWARE_STEPS = [
    # Paso 0: encender
    ("enciende", "Perfecto 👍\n\nEnciende la cámara. La luz del frente va a encenderse y apagarse.\n\nAvísame cuando la veas."),
    # Paso 1: buscar WiFi
    ("busca_wifi", "Ve al WiFi de tu celular o computadora.\nVas a ver una red que dice **OjoIA-XXXX**.\n\nConéctate ahí y avísame."),
    # Paso 2: elegir red
    ("elige_red", "Se abrirá una página automáticamente.\nElige el WiFi de tu negocio, ponle la clave y guarda.\n\nAvísame cuando la luz deje de parpadear."),
]

# ── Respuesta ─────────────────────────────────────────────────────────────────

def _resp(session: Dict, text: str, img_b64: str = "", buttons: list = None) -> Dict:
    rules = session.get("confirmed_rules", [])
    rules_text = [r["es"] if isinstance(r, dict) else str(r) for r in rules]
    return {
        "success":          True,
        "response":         text,
        "image_url":        f"data:image/jpeg;base64,{img_b64}" if img_b64 else "",
        "phase":            session["phase"],
        "rules_count":      len(rules),
        "rules_text":       rules_text,
        "zone":             session.get("zone",""),
        "has_image":        bool(session.get("image_b64")),
        "ready_to_confirm": session["phase"] == EvaPhase.CONFIRM.value,
        "camera_saved":     session["phase"] == EvaPhase.DONE.value,
        "buttons":          buttons or [],
        "metrics":          session.get("metrics", {}),
    }

# ── REVIEW — aceptar/rechazar/modificar cada regla individual ────────────────
async def _handle_review(session, session_id, user_id, storage_root, first):
    """Maneja la fase REVIEW: presenta reglas de Qwen una a una."""
    message = session.get("_last_review_msg", "")
    confirmed = session.get("confirmed_rules", [])
    rejected = session.get("rejected_rules", [])
    qwen_rules = session.get("qwen_rules", [])
    idx = session.get("review_index", 0)

    # Mensaje actual del usuario (viene del handler principal)
    # Necesitamos pasarlo de alguna forma — usamos _last_review_msg
    if not message:
        # Primera vez — mostrar primera regla
        if qwen_rules:
            rule = qwen_rules[0]
            session["pending_rule"] = rule
            text = (
                f"{first}, revisemos las 3 reglas diseñadas 👇\n\n"
                f"**Regla 1 de 3:** *\"{rule.get('es', '')}\"*\n\n"
                f"¿Te parece bien, la rechazas o la modificas?"
            )
            session["msgs"].append({"role":"assistant","content":text})
            _sessions[session_id] = session
            return _resp(session, text)
        else:
            # Sin reglas — fallback
            session["phase"] = EvaPhase.CONFIRM.value
            session["_confirm_shown"] = True
            text = f"{first}, no se pudieron generar reglas. ¿Apruebas con reglas genéricas?"
            session["msgs"].append({"role":"assistant","content":text})
            _sessions[session_id] = session
            return _resp(session, text)

    message = message.strip()
    session["msgs"].append({"role":"user","content":message})

    # Determinar respuesta del usuario
    accepted = is_yes(message)
    rejected_flag = is_no(message)
    is_custom = len(message) > 15 and not accepted and not rejected_flag
    msg_lower = message.lower().strip()
    is_keep = session.get("_editing") and ("mantener" in msg_lower or "actuales" in msg_lower or "no cambiar" in msg_lower)
    is_new = session.get("_editing") and ("nuevas" in msg_lower or "nuevo" in msg_lower or "cambiar todo" in msg_lower)

    current_rule = qwen_rules[idx] if idx < len(qwen_rules) else None

    # Modo edición: "mantener" → conservar reglas actuales, ir a CONFIRM
    if is_keep:
        session["phase"] = EvaPhase.CONFIRM.value
        session["_confirm_shown"] = True
        rules_display = "\n".join([f"  {i+1}. {r.get('es', r.get('en', str(r))) if isinstance(r, dict) else r}" for i, r in enumerate(confirmed)])
        zone = session.get("zone", "zona vigilada")
        text = (
            f"Perfecto {first}. Mantienes las reglas actuales.\n\n"
            f"**Reglas ({len(confirmed)}):**\n{rules_display}\n\n"
            f"Apruebas esta configuracion?"
        )
        session["msgs"].append({"role":"assistant","content":text})
        _sessions[session_id] = session
        return _resp(session, text)

# Modo edición: "nuevas" → usar las reglas de Qwen
    if is_new:
        session["confirmed_rules"] = []
        session["rejected_rules"] = []
        session["review_index"] = 0
        _nr = qwen_rules[0].get('es', '') if qwen_rules else ''
        text = (
            "Perfecto " + first + ". Usamos las nuevas reglas de Qwen.\n\n"
            "Revisemoslas una por una. Te parece bien la **primera regla**?\n\n"
            '-> "' + _nr + '"'
        )
        session["pending_rule"] = qwen_rules[0] if qwen_rules else None
        session["msgs"].append({"role":"assistant","content":text})
        _sessions[session_id] = session
        return _resp(session, text)

    if accepted and current_rule:
        # Aceptar regla actual
        rule_es = current_rule.get("es", "")
        if not any(r.get("es", "") == rule_es for r in confirmed):
            confirmed.append(current_rule)
        session["confirmed_rules"] = confirmed
        session["review_index"] = idx + 1
        session["pending_rule"] = None

        next_idx = idx + 1
        if next_idx >= len(qwen_rules):
            # Todas revisadas — ir a CONFIRM
            session["phase"] = EvaPhase.CONFIRM.value
            session["_confirm_shown"] = True
            rules_display = "\n".join([f"  {i+1}. {r.get('es','')}" for i, r in enumerate(confirmed)])
            sched = session.get("schedule", {})
            zone = session.get("zone", "zona vigilada")
            text = (
                f"{first}, esto es lo que quedó configurado 📋\n\n"
                f"📷 **Cámara:** Cámara {zone}\n"
                f"📍 **Zona:** {zone}\n"
                f"🕐 **Horario normal:** {sched.get('open','08:00')} a {sched.get('close','22:00')}\n"
                f"🌙 **Fuera de horario:** alerta inmediata si veo a cualquier persona\n\n"
                f"**Reglas confirmadas ({len(confirmed)}/3):**\n{rules_display}\n\n"
                f"¿Apruebas esta configuración?"
            )
            session["msgs"].append({"role":"assistant","content":text})
            _sessions[session_id] = session
            return _resp(session, text)
        else:
            # Mostrar siguiente regla
            next_rule = qwen_rules[next_idx]
            session["pending_rule"] = next_rule
            remaining = len(qwen_rules) - next_idx
            text = (
                f"✅ Regla confirmada.\n\n"
                f"**Regla {next_idx+1} de 3:** *\"{next_rule.get('es', '')}\"*\n\n"
                f"¿Te parece bien? ({remaining} restante{'s' if remaining > 1 else ''})"
            )
            session["msgs"].append({"role":"assistant","content":text})
            _sessions[session_id] = session
            return _resp(session, text)

    elif rejected_flag and current_rule:
        # Rechazar regla actual
        rule_es = current_rule.get("es", "")
        if not any(r.get("es", "") == rule_es for r in rejected):
            rejected.append(current_rule)
        session["rejected_rules"] = rejected
        session["review_index"] = idx + 1
        session["pending_rule"] = None

        next_idx = idx + 1
        if next_idx >= len(qwen_rules):
            # No hay más reglas — ¿aceptamos lo que hay o reintentamos?
            if confirmed:
                session["phase"] = EvaPhase.CONFIRM.value
                session["_confirm_shown"] = True
                rules_display = "\n".join([f"  {i+1}. {r.get('es','')}" for i, r in enumerate(confirmed)])
                sched = session.get("schedule", {})
                zone = session.get("zone", "zona vigilada")
                text = (
                    f"{first}, esto es lo que quedó configurado 📋\n\n"
                    f"📷 **Cámara:** Cámara {zone}\n"
                    f"📍 **Zona:** {zone}\n"
                    f"🕐 **Horario normal:** {sched.get('open','08:00')} a {sched.get('close','22:00')}\n"
                    f"🌙 **Fuera de horario:** alerta inmediata si veo a cualquier persona\n\n"
                    f"**Reglas confirmadas ({len(confirmed)}/3):**\n{rules_display}\n\n"
                    f"¿Apruebas esta configuración?"
                )
            else:
                # Todas rechazadas — reintentar con Qwen
                session["phase"] = EvaPhase.RULES.value
                text = f"Entendido {first}. Voy a diseñar 3 reglas diferentes. Un momento... 🧠"
            session["msgs"].append({"role":"assistant","content":text})
            _sessions[session_id] = session
            return _resp(session, text)
        else:
            next_rule = qwen_rules[next_idx]
            session["pending_rule"] = next_rule
            text = (
                f"Entendido, la descartamos.\n\n"
                f"**Regla {next_idx+1} de 3:** *\"{next_rule.get('es', '')}\"*\n\n"
                f"¿Te parece bien?"
            )
            session["msgs"].append({"role":"assistant","content":text})
            _sessions[session_id] = session
            return _resp(session, text)

    elif is_custom:
        # Usuario propone modificación — aceptar como regla personalizada
        try:
            custom_en_raw = await _qwen([{"role":"user","content":(
                f"Convierte esta regla de seguridad al inglés como una pregunta visual "
                f"(sí/no) para una cámara de vigilancia. Solo responde el JSON:\n"
                f"Regla: \"{message}\"\n"
                f"{{\"en\": \"pregunta en inglés\"}}"
            )}], 80)
            m = __import__("re").search(r'\{[^}]+\}', custom_en_raw)
            en = json.loads(m.group())["en"] if m else f"Is there any violation in the {session.get('zone','area')}?"
        except Exception:
            en = f"Is there any violation in the {session.get('zone','area')}?"

        custom_rule = {"es": message, "en": en}
        confirmed.append(custom_rule)
        session["confirmed_rules"] = confirmed
        session["review_index"] = idx + 1
        session["pending_rule"] = None

        next_idx = idx + 1
        if next_idx >= len(qwen_rules):
            session["phase"] = EvaPhase.CONFIRM.value
            session["_confirm_shown"] = True
            rules_display = "\n".join([f"  {i+1}. {r.get('es','')}" for i, r in enumerate(confirmed)])
            sched = session.get("schedule", {})
            zone = session.get("zone", "zona vigilada")
            text = (
                f"{first}, esto es lo que quedó configurado 📋\n\n"
                f"📷 **Cámara:** Cámara {zone}\n"
                f"📍 **Zona:** {zone}\n"
                f"🕐 **Horario normal:** {sched.get('open','08:00')} a {sched.get('close','22:00')}\n"
                f"🌙 **Fuera de horario:** alerta inmediata si veo a cualquier persona\n\n"
                f"**Reglas confirmadas ({len(confirmed)}/3):**\n{rules_display}\n\n"
                f"¿Apruebas esta configuración?"
            )
        else:
            next_rule = qwen_rules[next_idx]
            session["pending_rule"] = next_rule
            text = (
                f"✅ Regla personalizada guardada.\n\n"
                f"**Regla {next_idx+1} de 3:** *\"{next_rule.get('es', '')}\"*\n\n"
                f"¿Te parece bien?"
            )
        session["msgs"].append({"role":"assistant","content":text})
        _sessions[session_id] = session
        return _resp(session, text)

    else:
        # No se entendió — repetir la regla actual
        if current_rule:
            text = (
                f"No entendí bien. La regla actual es:\n\n"
                f"👉 *\"{current_rule.get('es', '')}\"*\n\n"
                f"¿Te parece bien (sí), la rechazas (no) o la modificas (escríbela)?"
            )
        else:
            text = "No entendí. ¿Aceptas la regla (sí), la rechazas (no) o la modificas?"
        session["msgs"].append({"role":"assistant","content":text})
        _sessions[session_id] = session
        return _resp(session, text)


# ── HANDLER PRINCIPAL ─────────────────────────────────────────────────────────

async def handle_eva_chat(
    user_id:     str,
    message:     str,
    session_id:  str,
    cam_id:      Optional[str],
    include_frame: bool,
    storage_root: Path,
) -> Dict[str, Any]:

    now = time.time()

    # Inicializar o recuperar sesión
    session = _get_session(session_id, storage_root)
    if not session:
        session = _create_session(session_id, user_id, storage_root, cam_id or "")
    elif cam_id and not session.get("camera_id"):
        session["camera_id"] = cam_id

    first = session["owner_name"].split()[0] if session.get("owner_name") else "amigo"
    phase = session["phase"]

    # Si __greet__ llega pero la sesión ya está en fase posterior (edición),
    # y el mensaje es __greet__, no mostrar GREET — continuar a la fase actual
    if message == "__greet__" and phase != EvaPhase.GREET.value:
        # Dejar que el handler de la fase actual procese el __greet__ como trigger
        # Solo interceptar GREET real (fase inicial)
        pass  # No retornar — continuar abajo a los handlers de fase

    # ── GREET ──────────────────────────────────────────────────────────────────
    if phase == EvaPhase.GREET.value and message == "__greet__":
        biz = session.get("business_name","tu negocio")
        biz_type = session.get("business_type","")
        biz_str = f"{biz}" + (f" ({biz_type})" if biz_type else "")
        text = (
            f"Hola {first} 👋\n\n"
            f"Vi que tienes {biz_str}. Vamos a configurar una cámara nueva.\n\n"
            f"¿Dónde la vas a poner? (ej: en la caja, en la entrada, en el almacén)"
        )
        session["msgs"].append({"role":"assistant","content":text})
        _sessions[session_id] = session
        return _resp(session, text)

    # ── Capturar zona desde GREET ──────────────────────────────────────────────
    if phase == EvaPhase.GREET.value:
        session["zone"] = message.strip()
        session["phase"] = EvaPhase.HARDWARE.value
        session["hardware_step"] = 0
        text = HARDWARE_STEPS[0][1]
        session["msgs"].append({"role":"user","content":message})
        session["msgs"].append({"role":"assistant","content":text})
        _sessions[session_id] = session
        return _resp(session, text)

    # ── HARDWARE ───────────────────────────────────────────────────────────────
    if phase == EvaPhase.HARDWARE.value:
        step = session.get("hardware_step", 0)
        session["msgs"].append({"role":"user","content":message})

        # Avanzar paso si el usuario confirmó
        if is_hardware_done(message) or is_yes(message):
            next_step = step + 1
        else:
            next_step = step  # repetir el mismo paso si no confirmó

        if next_step >= len(HARDWARE_STEPS):
            # Todos los pasos de hardware completados → esperar imagen
            session["phase"] = EvaPhase.WAIT_IMAGE.value
            text = (
                "✅ ¡Listo! Voy a conectarme a tu cámara.\n\n"
                "Dame un momento... _(conectando al dispositivo)_"
            )
        else:
            session["hardware_step"] = next_step
            text = HARDWARE_STEPS[next_step][1]

        session["msgs"].append({"role":"assistant","content":text})
        _sessions[session_id] = session
        return _resp(session, text)

    # ── WAIT_IMAGE ─────────────────────────────────────────────────────────────
    if phase == EvaPhase.WAIT_IMAGE.value:
        session["msgs"].append({"role":"user","content":message})

        # Intentar obtener frame
        frame = _get_frame()
        if frame and not session.get("image_b64"):
            b64 = _resize(frame, 640)
            session["image_b64"] = b64
            session["camera_connected"] = True
            raw_desc = await _describe_image(b64)
            session["image_desc"] = raw_desc
            # Parsear JSON de scene_analysis
            try:
                m = _re.search(r'\{[\s\S]*\}', raw_desc)
                if m:
                    sa = json.loads(m.group())
                    session["scene_analysis"] = json.dumps(sa, ensure_ascii=False)
                else:
                    session["scene_analysis"] = ""
            except Exception:
                session["scene_analysis"] = ""
            session["phase"] = EvaPhase.ANALYZE.value
            # Guardar frame en disco
            try:
                fd = storage_root / "users" / user_id / "cameras" / \
                     session.get("camera_id","unknown") / "frames"
                fd.mkdir(parents=True, exist_ok=True)
                with open(fd / "first.jpg", "wb") as f:
                    f.write(frame)
            except Exception:
                pass

        if session["phase"] == EvaPhase.ANALYZE.value:
            # Ir directo a análisis
            text = await _analyze_position(session)
            session["msgs"].append({"role":"assistant","content":text})
            _sessions[session_id] = session
            return _resp(session, text, img_b64=session.get("image_b64",""))

        # Sin imagen todavía
        if is_yes(message) or "siguiente" in message.lower():
            # Usuario no quiere esperar → saltar a PROBLEM sin imagen
            session["phase"] = EvaPhase.PROBLEM.value
            text = (
                f"Perfecto 👍 {first}.\n\n"
                f"La posición quedó confirmada.\n\n"
                f"Ahora dime: ¿qué es lo que más te preocupa en {session.get('zone','esa zona')} "
                f"cuando no estás?"
            )
        else:
            text = (
                "Todavía esperando la imagen... 📷\n\n"
                "Si ya está conectada, escribe **'siguiente'** para continuar."
            )
        session["msgs"].append({"role":"assistant","content":text})
        _sessions[session_id] = session
        return _resp(session, text)

    # ── ANALYZE ────────────────────────────────────────────────────────────────
    if phase == EvaPhase.ANALYZE.value:
        session["msgs"].append({"role":"user","content":message})
        session["position_confirmed"] = True
        session["phase"] = EvaPhase.PROBLEM.value
        zone = session.get("zone","esa zona")
        text = (
            f"Perfecto 👍 {first}.\n\n"
            f"La posición quedó bien.\n\n"
            f"Ahora dime: ¿qué es lo que más te preocupa de seguridad en {zone} "
            f"cuando no estás?"
        )
        session["msgs"].append({"role":"assistant","content":text})
        _sessions[session_id] = session
        return _resp(session, text)

    # ── PROBLEM ────────────────────────────────────────────────────────────────
    # 3 pasos: preocupación → roles/personal → evento sospechoso específico
    # Modo edición: si _editing=True, mostrar datos existentes y preguntar qué cambiar
    if phase == EvaPhase.PROBLEM.value:
        session["msgs"].append({"role":"user","content":message})

        # Modo edición: saludo con datos existentes
        if session.get("_editing") and not session.get("_edit_done"):
            session["_edit_done"] = True
            zone = session.get("zone", "esa zona")
            rules = session.get("confirmed_rules", [])
            rules_text = ""
            if rules:
                rules_text = "\nReglas actuales:\n" + "\n".join([f"  • {r.get('es', r) if isinstance(r, dict) else r}" for r in rules[:3]])
            text = (
                f"Hola {first} 👋\n\n"
                f"Estás editando **{zone}**.{rules_text}\n\n"
                f"Dime: ¿qué quieres cambiar? "
                f"(ej: la zona, las reglas, la preocupación...)"
            )
            session["msgs"].append({"role":"assistant","content":text})
            _sessions[session_id] = session
            return _resp(session, text)

        # Paso 1: capturar preocupación principal
        if not session.get("concern"):
            session["concern"] = message.strip()
            zone = session.get("zone", "esa zona")
            text = (
                f"Entendido, {first}. 👍\n\n"
                f"**1 de 3:** ¿Quién está normalmente en {zone}? "
                f"(ej: solo un cajero, dos empleados rotando, el dueño todo el día...)"
            )
            session.setdefault("business_answers", [])
            session["msgs"].append({"role":"assistant","content":text})
            _sessions[session_id] = session
            return _resp(session, text)

        # Paso 2: quién trabaja ahí
        if len(session.get("business_answers", [])) < 1:
            session.setdefault("business_answers", []).append(message.strip())
            zone = session.get("zone", "esa zona")
            text = (
                f"Perfecto 👍\n\n"
                f"**2 de 3:** ¿Qué es lo que **NO** debería pasar ahí? "
                f"(ej: empleado solo después de cerrar, dos personas en caja al mismo tiempo, "
                f"cajón abierto sin nadie, alguien con bolso grande...)"
            )
            session["msgs"].append({"role":"assistant","content":text})
            _sessions[session_id] = session
            return _resp(session, text)

        # Paso 3: qué no debería pasar (evento sospechoso) → transiciona a RULES
        session.setdefault("business_answers", []).append(message.strip())
        session["phase"] = EvaPhase.RULES.value

        text = (
            f"Gracias {first}. Ya tengo todo el contexto 🧠\n\n"
            f"Analizando tu negocio + imagen + preocupaciones..."
        )
        session["msgs"].append({"role":"assistant","content":text})
        _sessions[session_id] = session
        return _resp(session, text)

    # ── RULES ───────────────────────────────────────────────────────────────────
    if phase == EvaPhase.RULES.value:
        # Edición: __greet__ inicial muestra diagnóstico + botones de modo
        if session.get("_editing") and message == "__greet__":
            session["_edit_rules_shown"] = True
            existing = session.get("confirmed_rules", [])
            zone = session.get("zone", "esta zona")
            biz_type = session.get("business_type", "negocio")
            metrics = session.get("metrics", {})

            # Análisis de reglas actuales contra knowledge base
            abstract_count = sum(1 for r in existing if _is_abstract_rule(r.get("es", "") if isinstance(r, dict) else str(r)))
            total_fp = metrics.get("total_false_positives", 0)
            total_events = metrics.get("total_events", 0)
            total_alerts = metrics.get("total_alerts", 0)

            # Construir texto de reglas con métricas
            rules_text = ""
            for i, r in enumerate(existing):
                r_es = r.get("es", r) if isinstance(r, dict) else r
                r_metrics = metrics.get("rules", {}).get(f"rule_{i}", {})
                fp = r_metrics.get("false_positives", 0)
                alerts = r_metrics.get("alerts", 0)
                flag = " ⚠️" if fp >= 2 else ""
                abstract_flag = " ❌ abstracta" if _is_abstract_rule(r_es) else ""
                rules_text += f"  {i+1}. {r_es} ({alerts} alertas, {fp} falsas){flag}{abstract_flag}\n"

            # Detectar problemas
            problems = []
            if abstract_count > 0:
                problems.append(f"🔴 {abstract_count} reglas son abstractas (la cámara no puede verificarlas)")
            if total_events > 20 and total_alerts == 0:
                problems.append("🔴 La cámara no está detectando violaciones")
            if total_fp >= 3:
                problems.append(f"🔴 {total_fp} falsas alarmas — sensibilidad muy alta")

            problems_text = ""
            if problems:
                problems_text = "**Problemas encontrados:**\n" + "\n".join(f"  {p}" for p in problems) + "\n\n"

            text = (
                f"Revisé tu cámara en {zone} 🔧\n\n"
                f"**Reglas actuales:**\n{rules_text}\n"
                f"{problems_text}"
                f"**¿Qué quieres hacer?**"
            )
            session["msgs"].append({"role": "assistant", "content": text})
            _sessions[session_id] = session
            _save_session_to_disk(session, storage_root)
            return _resp(session, text, buttons=[
                {"label": "🛠️ Diagnóstico", "value": "diagnostic"},
                {"label": "🔧 Ajustar reglas", "value": "mejorar"},
                {"label": "🆕 Rediseñar todo", "value": "redesign"},
            ])

        elif message != "__greet__":
            session["msgs"].append({"role": "user", "content": message})

        # Si ya tenemos reglas de Qwen generadas → procesar respuesta del usuario
        if session.get("qwen_rules") and message != "__greet__":
            qwen_rules = session["qwen_rules"]
            msg_lower = message.lower().strip()
            msg_stripped = message.strip()

            # ── Detectar patrones de modificación ──
            # "cambia la 1 por X", "modificar regla 2: X", "la 1 es X", "regla 3: X"
            modify_match = _re.search(r'(?:cambia(?:r)?|modifica(?:r)?)\s+(?:la\s+|regla\s+)?(\d+)(?:\s+por\s+|\s*:\s*|\s+a\s+)(.+)', msg_lower)
            # "1. X", "1) X", "1: X", "1- X", "1 X" (número al inicio + texto)
            numbered_match = _re.match(r'^(\d+)\s*[\.\)\-:]\s*(.+)', msg_stripped) or _re.match(r'^(\d+)\s+(.+)', msg_stripped)
            # "la 1: X" o "regla 2: X"
            replace_match = _re.search(r'(?:la|regla)\s+(\d+)\s*(?:es|sería|cambia a|=|:)\s*(.+)', msg_stripped, _re.IGNORECASE)

            # Detectar número solo (ej: "1") → preguntar cuál
            number_only = _re.match(r'^(\d+)\s*$', msg_stripped)

            is_modify_intent = (modify_match or replace_match or numbered_match or number_only
                or msg_lower in ("modificar", "modificar alguna", "modificar una", "btn_modificar", "modificar")
                or any(kw in msg_lower for kw in ("cambiar la", "cambia la", "modificar la", "otra para la")))

            # ── Aceptar todas ──
            # Limpiar emoji del mensaje para matching
            _msg_clean = _re.sub(r'[^\w\s,;:\-\(\)]', '', msg_lower).strip()
            _accept_exact = {"todas bien", "aceptar todas", "todas", "perfecto", "sí acepto", "si acepto", "acepto", "guardar", "confirmar", "me gustan", "están bien", "sí", "si", "ok", "dale", "ya", "listo", "listas", "hecho", "aceptar_todas"}
            if _msg_clean in _accept_exact or msg_lower in _accept_exact or msg_lower.startswith(("si, ", "sí, ", "si ", "sí ", "ok, ", "dale, ", "listo, ")):
                session["confirmed_rules"] = [{"es": r.get("es", "")} for r in qwen_rules[:3]]
                session["phase"] = EvaPhase.CONFIRM.value
                session["_confirm_shown"] = True
                rules_display = "\n".join(f"  {i+1}. {r.get('es','')}" for i, r in enumerate(session["confirmed_rules"]))
                sched = session.get("schedule", {})
                zone = session.get("zone", "esta zona")
                text = (
                    f"Perfecto {first}. Reglas confirmadas ✅\n\n"
                    f"📷 **Cámara:** Cámara {zone}\n"
                    f"🕐 **Horario:** {sched.get('open','08:00')} a {sched.get('close','22:00')}\n\n"
                    f"**Reglas ({len(session['confirmed_rules'])}):**\n{rules_display}\n\n"
                    f"¿Confirmas la configuración?"
                )
                session["msgs"].append({"role": "assistant", "content": text})
                _sessions[session_id] = session
                _save_session_to_disk(session, storage_root)
                return _resp(session, text, buttons=[
                    {"label": "Sí, guardar ✅", "value": "confirmar"},
                    {"label": "Cambiar algo ✏️", "value": "volver"},
                ])

            # ── Descartar todas ──
            elif msg_lower in {"descartar", "descartar todas", "no sirven", "no me gustan", "no van", "empezar de nuevo", "no quiero esas", "no las quiere", "btn_descartar"}:
                session["qwen_rules"] = []
                session.setdefault("_rejected_rules", []).extend([r.get("es","") for r in qwen_rules])
                text = f"Entendido {first}. Diseñando 3 reglas diferentes... 🧠"
                session["msgs"].append({"role": "assistant", "content": text})
                _sessions[session_id] = session
                # Continuar abajo para llamar a Qwen de nuevo

            # ── Modificar regla específica ──
            elif is_modify_intent:
                rule_idx = None
                new_text = None

                if modify_match:
                    rule_idx = int(modify_match.group(1)) - 1
                    new_text = modify_match.group(2).strip()
                elif replace_match:
                    rule_idx = int(replace_match.group(1)) - 1
                    new_text = replace_match.group(2).strip()
                elif numbered_match:
                    rule_idx = int(numbered_match.group(1)) - 1
                    new_text = numbered_match.group(2).strip()
                elif number_only:
                    rule_idx = int(number_only.group(1)) - 1
                    new_text = None  # Pedir texto después

                if rule_idx is not None and rule_idx < len(qwen_rules):
                    if new_text:
                        qwen_rules[rule_idx] = {"es": new_text}
                        session["qwen_rules"] = qwen_rules
                        new_text_show = "\n".join(f"  {i+1}. {r.get('es', '')}" for i, r in enumerate(qwen_rules[:3]))
                        text = (
                            f"✅ Regla {rule_idx+1} actualizada.\n\n"
                            f"**Reglas actuales:**\n{new_text_show}\n\n"
                            f"¿Qué quieres hacer?"
                        )
                        session["msgs"].append({"role": "assistant", "content": text})
                        _sessions[session_id] = session
                        _save_session_to_disk(session, storage_root)
                        return _resp(session, text, buttons=[
                            {"label": "Todas bien ✅", "value": "aceptar_todas"},
                            {"label": "Modificar otra ✏️", "value": "modificar"},
                            {"label": "Descartar ❌", "value": "descartar"},
                        ])
                    else:
                        # Pidió modificar pero no dio el texto nuevo
                        current = qwen_rules[rule_idx].get("es", "")
                        text = (
                            f"La regla {rule_idx+1} actual es:\n"
                            f"  👉 *\"{current}\"*\n\n"
                            f"¿Por cuál la cambias?"
                        )
                        session["_pending_modify_idx"] = rule_idx
                        session["msgs"].append({"role": "assistant", "content": text})
                        _sessions[session_id] = session
                        return _resp(session, text)
                else:
                    # Número fuera de rango
                    text = f"Dime un número del 1 al {len(qwen_rules)}. ¿Cuál regla quieres cambiar?"
                    session["msgs"].append({"role": "assistant", "content": text})
                    _sessions[session_id] = session
                    return _resp(session, text)

            # ── Texto libre largo → podría ser una regla nueva o una opinión ──
            elif len(msg_stripped) > 5:
                # Verificar si hay un pending modify idx (preguntando por texto nuevo)
                pending_idx = session.get("_pending_modify_idx")
                if pending_idx is not None and pending_idx < len(qwen_rules):
                    qwen_rules[pending_idx] = {"es": msg_stripped}
                    session["qwen_rules"] = qwen_rules
                    session["_pending_modify_idx"] = None
                    new_text_show = "\n".join(f"  {i+1}. {r.get('es', '')}" for i, r in enumerate(qwen_rules[:3]))
                    text = (
                        f"✅ Regla {pending_idx+1} actualizada.\n\n"
                        f"**Reglas actuales:**\n{new_text_show}\n\n"
                        f"¿Qué quieres hacer?"
                    )
                    session["msgs"].append({"role": "assistant", "content": text})
                    _sessions[session_id] = session
                    _save_session_to_disk(session, storage_root)
                    return _resp(session, text, buttons=[
                        {"label": "Todas bien ✅", "value": "aceptar_todas"},
                        {"label": "Modificar otra ✏️", "value": "modificar"},
                        {"label": "Descartar ❌", "value": "descartar"},
                    ])

                # Texto que parece una regla (corta, concreta) → reemplazar regla 1 por defecto
                elif len(msg_stripped) < 120 and not msg_stripped.endswith("?"):
                    qwen_rules[0] = {"es": msg_stripped}
                    session["qwen_rules"] = qwen_rules
                    new_text_show = "\n".join(f"  {i+1}. {r.get('es', '')}" for i, r in enumerate(qwen_rules[:3]))
                    text = (
                        f"✅ Regla 1 actualizada.\n\n"
                        f"**Reglas actuales:**\n{new_text_show}\n\n"
                        f"¿Qué quieres hacer?"
                    )
                    session["msgs"].append({"role": "assistant", "content": text})
                    _sessions[session_id] = session
                    _save_session_to_disk(session, storage_root)
                    return _resp(session, text, buttons=[
                        {"label": "Todas bien ✅", "value": "aceptar_todas"},
                        {"label": "Modificar otra ✏️", "value": "modificar"},
                        {"label": "Descartar ❌", "value": "descartar"},
                    ])

                # Texto largo / opinión → usar como contexto para regenerar
                else:
                    session["_edit_free_input"] = msg_stripped
                    text = f"Entendido {first}. Tomando eso en cuenta... 🧠 Ajustando reglas..."
                    session["msgs"].append({"role": "assistant", "content": text})
                    session["qwen_rules"] = []  # Limpiar para regenerar
                    _sessions[session_id] = session
                    # Continuar abajo para llamar a Qwen de nuevo

            else:
                # No se entendió — re-mostrar reglas
                new_text_show = "\n".join(f"  {i+1}. {r.get('es', '')}" for i, r in enumerate(qwen_rules[:3]))
                text = (
                    f"No entendí bien. ¿Qué quieres hacer?\n\n"
                    f"**Reglas propuestas:**\n{new_text_show}\n\n"
                    f"— Escribe **1, 2 o 3** para modificar una regla\n"
                    f"— Escribe la regla nueva directamente\n"
                    f"— O dime **todas bien** para confirmar"
                )
                session["msgs"].append({"role": "assistant", "content": text})
                _sessions[session_id] = session
                return _resp(session, text, buttons=[
                    {"label": "Todas bien ✅", "value": "aceptar_todas"},
                    {"label": "Modificar alguna ✏️", "value": "modificar"},
                    {"label": "Descartar ❌", "value": "descartar"},
                ])

            # Si llegamos aquí sin retornar, significa que se descartó o se pidió regenerar
            # Continuar abajo para llamar a Qwen de nuevo

        # Edición: procesar respuesta del usuario (botones inline)
        if session.get("_editing") and message != "__greet__" and not session.get("qwen_rules"):
            msg_lower = message.lower().strip()
            existing = session.get("confirmed_rules", [])
            zone = session.get("zone", "esta zona")

            # DIAGNOSTICO: Analizar reglas actuales y proponer estrategias
            if msg_lower in ("diagnostic", "diagnóstico", "diagnostico", "analizar", "revisar"):
                # Analizar reglas actuales contra knowledge base
                abstract_rules = []
                visual_rules = []
                for r in existing:
                    r_es = r.get("es", "") if isinstance(r, dict) else str(r)
                    if _is_abstract_rule(r_es):
                        abstract_rules.append(r_es)
                    else:
                        visual_rules.append(r_es)

                profile = get_scene_profile(session.get("business_type", ""), zone)
                recommended = get_checks_for_scene(
                    session.get("business_type", ""), zone, session.get("concern", "")
                )

                lines = [f"Samuel, analicé tu cámara de {zone} 🔍\n"]
                lines.append(f"**Reglas actuales: {len(existing)}**")
                if abstract_rules:
                    lines.append(f"\n🔴 **{len(abstract_rules)} reglas abstractas (NO funcionan):**")
                    for r in abstract_rules:
                        suggestion = _suggest_visual_alternative(r)
                        lines.append(f'   ❌ "{r}"')
                        lines.append(f'   💡 **Mejor:** "{suggestion}"')
                if visual_rules:
                    lines.append(f"\n✅ **{len(visual_rules)} reglas visuales:**")
                    for r in visual_rules:
                        lines.append(f'   ✓ "{r}"')

                if profile and recommended:
                    lines.append(f"\n\n🎯 **Estrategia recomendada para {profile.get('description', zone)}:**")
                    for i, check in enumerate(recommended[:3], 1):
                        lines.append(f"   {i}. {check['es']}")

                lines.append(f"\n\n¿Qué quieres hacer?")

                text = "\n".join(lines)
                session["msgs"].append({"role": "assistant", "content": text})
                _sessions[session_id] = session
                return _resp(session, text, buttons=[
                    {"label": "✅ Usar estrategia recomendada", "value": "use_strategy"},
                    {"label": "🔧 Ajustar reglas actuales", "value": "mejorar"},
                    {"label": "🔄 Empezar desde cero", "value": "redesign"},
                ])

            # USAR ESTRATEGIA RECOMENDADA: Aplicar checks del knowledge base directamente
            elif msg_lower in ("use_strategy", "usar estrategia", "usar recomendación", "aceptar estrategia"):
                recommended = get_checks_for_scene(
                    session.get("business_type", ""), zone, session.get("concern", "")
                )
                if recommended:
                    new_rules = []
                    for check in recommended[:3]:
                        new_rules.append({
                            "es": check["es"],
                            "en": check.get("en", check["es"]),
                            "severity": check.get("severity", "medium"),
                            "abstract": False,
                            "from_knowledge_base": True,
                        })
                    session["confirmed_rules"] = new_rules
                    session["phase"] = EvaPhase.CONFIRM.value
                    session["_confirm_shown"] = True
                    rules_display = "\n".join(f"  {i+1}. {r.get('es','')}" for i, r in enumerate(new_rules))
                    text = (
                        f"Perfecto Samuel. Aplicé la estrategia recomendada:\n\n"
                        f"**Nuevas reglas ({len(new_rules)}):**\n{rules_display}\n\n"
                        f"¿Confirmas?"
                    )
                    session["msgs"].append({"role": "assistant", "content": text})
                    _save_session_to_disk(session, storage_root)
                    _sessions[session_id] = session
                    return _resp(session, text, buttons=[
                        {"label": "Sí, guardar ✅", "value": "confirmar"},
                        {"label": "Modificar alguna ✏️", "value": "modificar"},
                    ])
                else:
                    # No hay estrategia predefinida, caer a Qwen
                    session["_edit_free_input"] = None
                    session.setdefault("_edit_rules_shown", True)
                    _sessions[session_id] = session
                    # Continuar abajo para llamar a Qwen

            # MANTENER: Confirmar reglas actuales
            elif msg_lower in ("mantener", "mantener actuales", "no cambiar", "están bien", "ok", "si", "sí", "quedo así", "btn_mantener", "mantener estas reglas"):
                session["phase"] = EvaPhase.CONFIRM.value
                session["_confirm_shown"] = True
                rules_display = "\n".join(f"  {i+1}. {r.get('es', r) if isinstance(r, dict) else r}" for i, r in enumerate(existing))
                sched = session.get("schedule", {})
                text = (
                    f"Perfecto {first}. Mantienes las reglas actuales.\n\n"
                    f"📷 **Cámara:** Cámara {zone}\n"
                    f"🕐 **Horario:** {sched.get('open','08:00')} a {sched.get('close','22:00')}\n\n"
                    f"**Reglas ({len(existing)}):**\n{rules_display}\n\n"
                    f"¿Confirmas?"
                )
                session["msgs"].append({"role": "assistant", "content": text})
                _save_session_to_disk(session, storage_root)
                _sessions[session_id] = session
                return _resp(session, text, buttons=[
                    {"label": "Sí, guardar ✅", "value": "confirmar"},
                    {"label": "No, cambiar algo ✏️", "value": "volver"},
                ])

            # MEJORAR: Llamar a Qwen para generar reglas mejores
            elif msg_lower in ("mejorar", "mejorar reglas", "mas precisas", "más precisas", "btn_mejorar", "ajustar"):
                session["_edit_rules_shown"] = True
                session["_edit_free_input"] = None
                text = f"Analizando para diseñar reglas más precisas... 🧠"
                session["msgs"].append({"role": "assistant", "content": text})
                _sessions[session_id] = session
                # Continuar abajo para llamar a Qwen

            # REDISEÑAR: Empezar desde cero con análisis completo
            elif msg_lower in ("redesign", "rediseñar", "redisenar", "empezar desde cero", "cambiar todo", "nuevas reglas"):
                session["confirmed_rules"] = []
                session["qwen_rules"] = []
                session["_edit_rules_shown"] = True
                session["_edit_free_input"] = None
                recommended = get_checks_for_scene(
                    session.get("business_type", ""), zone, session.get("concern", "")
                )
                if recommended:
                    session["_kb_recommended"] = [c["es"] for c in recommended[:3]]
                text = f"Diseñando estrategias para tu {zone} desde cero... 🧠"
                session["msgs"].append({"role": "assistant", "content": text})
                _sessions[session_id] = session
                # Continuar abajo para llamar a Qwen

            else:
                # Mensaje no reconocido — mostrar ayuda
                text = (
                    f"{first}, elige una opción:\n\n"
                    f"• **Diagnóstico** — Analizo tus reglas actuales\n"
                    f"• **Mejorar** — Diseño reglas más precisas\n"
                    f"• **Rediseñar** — Empiezo desde cero\n"
                    f"• **Mantener** — Dejo las reglas como están"
                )
                session["msgs"].append({"role": "assistant", "content": text})
                _sessions[session_id] = session
                return _resp(session, text, buttons=[
                    {"label": "🛠️ Diagnóstico", "value": "diagnostic"},
                    {"label": "🔧 Mejorar", "value": "mejorar"},
                    {"label": "🆕 Rediseñar", "value": "redesign"},
                    {"label": "✅ Mantener", "value": "mantener"},
                ])

        # ── Llamar a Qwen para generar 3 reglas (todo en español) ──
        qwen_rules_existing = session.get("qwen_rules", [])
        if not qwen_rules_existing:
            concern = session.get("concern", "")
            biz_type = session.get("business_type", "negocio")
            biz_name = session.get("business_name", "negocio")
            zone = session.get("zone", "zona")
            image_desc = session.get("image_desc", "")
            script = session.get("schedule", {})
            business_answers = session.get("business_answers", [])
            is_editing = session.get("_editing", False)
            existing = session.get("confirmed_rules", [])

            # Reglas existentes con métricas
            existing_rules_text = ""
            if is_editing and existing:
                metrics = session.get("metrics", {})
                for i, r in enumerate(existing):
                    r_es = r.get("es", r) if isinstance(r, dict) else r
                    r_m = metrics.get("rules", {}).get(f"rule_{i}", {})
                    fp = r_m.get("false_positives", 0)
                    alerts = r_m.get("alerts", 0)
                    fp_note = f" ⚠️ {fp} falsas alarmas" if fp >= 2 else ""
                    existing_rules_text += f"  {i+1}. {r_es} ({alerts} alertas){fp_note}\n"

            problem_context = ""
            if business_answers:
                if len(business_answers) >= 1:
                    problem_context += f"[Quién trabaja]: {business_answers[0]}\n"
                if len(business_answers) >= 2:
                    problem_context += f"[Qué NO debe pasar]: {business_answers[1]}\n"

            # Prompt de Qwen — todo en español, sin inglés
            qwen_sys = (
                "Eres un EXPERTO EN SEGURIDAD COMERCIAL para pequeños negocios en República Dominicana.\n"
                "Tu trabajo es diseñar reglas de vigilancia ESPECÍFICAS que una cámara con IA\n"
                "pueda verificar visualmente frame por frame.\n\n"
                "=== REGLAS PARA BUENAS REGLAS ===\n"
                "✅ ESPECÍFICA: Describe un comportamiento CONCRETO visible en imagen\n"
                "✅ OBSERVABLE: Alguien mirando la CÁMARA puede confirmar sí o no\n"
                "✅ CONDICIONAL: Incluye CUÁNDO/DÓNDE exactamente\n"
                "✅ ANTI-FALSO-POSITIVO: Evita ambigüedad — si hay duda, NO es violación\n\n"
                "✅ Buenos ejemplos:\n"
                "  'El cajón está abierto pero no hay nadie detrás del mostrador'\n"
                "  'Hay más de 2 personas detrás del mostrador al mismo tiempo'\n"
                "  'El empleado mete producto en bolsa sin pasar por caja'\n"
                "  'Hay una persona después de la hora de cierre'\n\n"
                "❌ Malos ejemplos (evitar):\n"
                '  "Vigilar el área" — demasiado genérico\n'
                '  "Actividad sospechosa" — subjetivo, no observable\n'
                '  "El empleado no debe robar" — no es verificable visualmente\n'
                '  "Monitorear 24/7" — no es una regla espec\u00edfica\n\n'
                f"=== HORARIO ===\n"
                f"Horario de operación: {script.get('open','08:00')} a {script.get('close','22:00')}.\n"
                "FUERA de ese horario, el sistema YA alerta automático si detecta personas.\n"
                "Tus 3 reglas son SOLO para horario de operación.\n\n"
            )

            if is_editing and existing_rules_text:
                qwen_sys += (
                    f"=== REGLAS ACTUALES (con rendimiento) ===\n{existing_rules_text}\n"
                    "INSTRUCCIÓN: Las reglas con falsas alarmas necesitan ser MÁS ESPECÍFICAS.\n"
                    "Agrega condiciones extra para reducir falsos positivos.\n"
                    "Si una regla no genera alertas, es demasiado restrictiva — relajarla.\n\n"
                )

            qwen_sys += (
                f"=== NEGOCIO ===\n"
                f"- Tipo: {biz_type}\n"
                f"- Nombre: {biz_name}\n"
                f"- Zona de esta cámara: {zone}\n"
                f"- Preocupación: {concern or 'seguridad general'}\n"
            )
            if problem_context:
                qwen_sys += f"\n{problem_context}"
            if image_desc:
                qwen_sys += f"\n[Lo que la cámara ve]: {image_desc}\n"

            if is_editing:
                free_input = session.get("_edit_free_input", "")
                if free_input:
                    qwen_sys += f"\n[Quiere mejorar]: {free_input}\n"

            qwen_sys += (
                '\n=== FORMATO DE SALIDA ===\n'
                'Responde SOLO JSON válido:\n'
                '{\n'
                '  "rules": [\n'
                '    {"es": "regla específica y concreta 1"},\n'
                '    {"es": "regla específica y concreta 2"},\n'
                '    {"es": "regla específica y concreta 3"}\n'
                '  ],\n'
                '  "system_prompt": "Cámara de seguridad vigilando [zona] de un [tipo] en República Dominicana. Horario: [horario]. Reglas: [lista en español]. Fuera de horario: cualquier persona = alerta inmediata.",\n'
                '  "scanner_question": "pregunta en español que englobe las 3 reglas para el analista de vigilancia"\n'
                '}'
            )

            qwen_msgs = [{"role": "system", "content": qwen_sys}]
            img_b64 = session.get("image_b64", "")
            if img_b64:
                try:
                    small = _resize(base64.b64decode(img_b64), 500)
                    qwen_msgs.append({
                        "role": "user",
                        "content": [
                            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{small}"}},
                            {"type": "text", "text": "Diseña las reglas basándote en esta imagen y el contexto."}
                        ]
                    })
                except Exception:
                    qwen_msgs.append({"role": "user", "content": "Diseña las reglas basándote en el contexto."})
            else:
                qwen_msgs.append({"role": "user", "content": "Diseña las reglas basándote en el contexto."})

            qwen_resp = await _qwen(qwen_msgs, 600)

        # Parsear JSON de Qwen
        try:
            json_match = __import__("re").search(r'\{[\s\S]*\}', qwen_resp)
            if json_match:
                data = json.loads(json_match.group())
            else:
                raise ValueError("No JSON found")
        except Exception as e:
            logger.error(f"Qwen JSON parse error: {e}, raw: {qwen_resp[:200]}")
            data = {
                "rules": [
                    {"es": f"Vigilar {zone} durante horario de operación"},
                    {"es": f"Alertar si hay personas después del cierre"},
                    {"es": f"Monitorear movimiento inusual en {zone}"}
                ],
                "system_prompt": f"Cámara de seguridad vigilando {zone} de un {biz_type} en República Dominicana. Horario: {script.get('open','08:00')}-{script.get('close','22:00')}. Reglas en español.",
                "scanner_question": f"¿Hay alguna actividad que viole las reglas de seguridad en {zone}?"
            }

        # Guardar en sesión
        qwen_rules = data.get("rules", [])
        if isinstance(qwen_rules, list) and len(qwen_rules) > 0:
            # Asegurar que todas las reglas tengan campo "es"
            cleaned = []
            for r in qwen_rules[:3]:
                if isinstance(r, dict):
                    cleaned.append({"es": r.get("es", r.get("en", str(r)))})
                else:
                    cleaned.append({"es": str(r)})
            session["qwen_rules"] = cleaned
        session["qwen_system_prompt"] = data.get("system_prompt", "")
        session["qwen_scanner_question"] = data.get("scanner_question", "")
        if not session.get("_editing"):
            session["confirmed_rules"] = []
            session["rejected_rules"] = []
            session["review_index"] = 0

        # Mostrar las 3 reglas propuestas
        qr = session["qwen_rules"]
        new_text = "\n".join(f"  {i+1}. {r.get('es', '')}" for i, r in enumerate(qr[:3]))
        text = (
            f"Perfecto {first}. Te propongo estas 3 reglas:\n\n"
            f"{new_text}\n\n"
            f"**¿Qué quieres hacer?**"
        )
        session["msgs"].append({"role": "assistant", "content": text})
        _sessions[session_id] = session
        _save_session_to_disk(session, storage_root)
        return _resp(session, text, buttons=[
            {"label": "Todas bien ✅", "value": "aceptar_todas"},
            {"label": "Modificar alguna ✏️", "value": "modificar"},
            {"label": "Descartar ❌", "value": "descartar"},
        ])

    # ── REVIEW ──────────────────────────────────────────────────────────────────
    if phase == EvaPhase.REVIEW.value:
        # Guardar mensaje para que _handle_review lo procese
        session["_last_review_msg"] = message
        return await _handle_review(session, session_id, user_id, storage_root, first)

    # ── CONFIRM ────────────────────────────────────────────────────────────────
    if session["phase"] == EvaPhase.CONFIRM.value:
        if session.get("_confirm_shown"):
            session["msgs"].append({"role":"user","content":message})
            if is_yes(message):
                config = build_camera_config(session)
                saved  = save_camera_config(user_id, config, storage_root)
                session["phase"] = EvaPhase.DONE.value
                text = (
                    f"🎉 ¡Listo {first}! Tu cámara está configurada y vigilando.\n\n"
                    f"{'✅ Todo guardado correctamente.' if saved else '⚠️ Hubo un problema al guardar, contacta al soporte.'}\n\n"
                    f"Te avisaré si detecto algo sospechoso en {session.get('zone','tu negocio')}."
                )
                session["msgs"].append({"role":"assistant","content":text})
                _sessions[session_id] = session
                _save_session_to_disk(session, storage_root)
                return _resp(session, text)
            else:
                # Usuario quiere volver/cambiar algo — regresar a reglas con las que ya tiene
                session["_confirm_shown"] = False
                session["phase"] = EvaPhase.RULES.value
                confirmed = session.get("confirmed_rules", [])
                rules_show = "\n".join(f"  {i+1}. {r.get('es','') if isinstance(r,dict) else str(r)}" for i, r in enumerate(confirmed))
                text = (
                    f"Perfecto {first}. Volvamos a revisar.\n\n"
                    f"**Reglas actuales:**\n{rules_show}\n\n"
                    f"¿Qué quieres hacer?"
                )
                session["qwen_rules"] = []  # Limpiar propuesta anterior
                session["msgs"].append({"role":"assistant","content":text})
                _sessions[session_id] = session
                return _resp(session, text, buttons=[
                    {"label": "Mantener estas reglas ✅", "value": "btn_mantener"},
                    {"label": "Mejorar", "value": "btn_mejorar"},
                    {"label": "Modificar una", "value": "btn_modificar"},
                ])

        # Primera vez que llegamos a CONFIRM — mostrar resumen
        session["msgs"].append({"role":"user","content":message})
        rules = session.get("confirmed_rules", [])
        rules_display = "\n".join([
            f"  {i+1}. {r['es'] if isinstance(r,dict) else str(r)}"
            for i, r in enumerate(rules)
        ])
        sched = session.get("schedule", {})
        zone  = session.get("zone","zona vigilada")
        text = (
            f"{first}, esto es lo que quedó configurado 📋\n\n"
            f"📷 **Cámara:** Cámara {zone}\n"
            f"📍 **Zona:** {zone}\n"
            f"🕐 **Horario normal:** {sched.get('open','08:00')} a {sched.get('close','22:00')}\n"
            f"🌙 **Fuera de horario:** alerta inmediata si veo a cualquier persona\n\n"
            f"**Reglas de vigilancia:**\n{rules_display}\n\n"
            f"¿Apruebas esta configuración?"
        )
        session["_confirm_shown"] = True
        session["msgs"].append({"role":"assistant","content":text})
        _sessions[session_id] = session
        return _resp(session, text)

    # ── DONE ───────────────────────────────────────────────────────────────────
    if session["phase"] == EvaPhase.DONE.value:
        text = (
            f"Tu cámara ya está activa y vigilando, {first}. "
            f"Si necesitas agregar otra cámara o cambiar algo, avísame."
        )
        session["msgs"].append({"role":"user","content":message})
        session["msgs"].append({"role":"assistant","content":text})
        _sessions[session_id] = session
        return _resp(session, text)

    # ── Fallback — fase desconocida ─────────────────────────────────────────
    logger.warning(f"Fase desconocida: {session['phase']}")
    text = "Hola, ¿en qué te puedo ayudar?"
    session["msgs"].append({"role":"user","content":message})
    session["msgs"].append({"role":"assistant","content":text})
    _sessions[session_id] = session
    return _resp(session, text)


def _save_session_to_disk(session: Dict, storage_root: Path):
    """Persistir sesión Eva en disco para sobrevivir reinicios."""
    try:
        cam_id = session.get("camera_id", "")
        user_id = session.get("user_id", "")
        if not cam_id or not user_id:
            return
        session_file = storage_root / "users" / user_id / "cameras" / cam_id / "eva_session.json"
        session_file.parent.mkdir(parents=True, exist_ok=True)
        # Guardar solo datos necesarios (no msgs completos para no crecer mucho)
        persist = {
            "session_id": session.get("session_id"),
            "phase": session.get("phase"),
            "zone": session.get("zone"),
            "concern": session.get("concern"),
            "confirmed_rules": session.get("confirmed_rules", []),
            "rejected_rules": session.get("rejected_rules", []),
            "qwen_rules": session.get("qwen_rules", []),
            "qwen_system_prompt": session.get("qwen_system_prompt", ""),
            "qwen_scanner_question": session.get("qwen_scanner_question", ""),
            "review_index": session.get("review_index", 0),
            "_editing": session.get("_editing", False),
            "_edit_rules_shown": session.get("_edit_rules_shown", False),
            "_confirm_shown": session.get("_confirm_shown", False),
            "_pending_modify_idx": session.get("_pending_modify_idx"),
            "camera_id": cam_id,
            "user_id": user_id,
        }
        session_file.write_text(json.dumps(persist, indent=2, ensure_ascii=False))
    except Exception as e:
        logger.error(f"Error guardando sesión: {e}")


def _load_session_from_disk(session_id: str, storage_root: Path) -> Optional[Dict]:
    """Cargar sesión Eva desde disco si existe."""
    try:
        # Buscar en todas las carpetas de usuario/cámara
        for user_dir in storage_root.glob("users/*"):
            for cam_dir in user_dir.glob("cameras/*"):
                session_file = cam_dir / "eva_session.json"
                if session_file.exists():
                    data = json.loads(session_file.read_text())
                    if data.get("session_id") == session_id:
                        # Cargar datos del usuario para completar campos faltantes
                        ud = _load_user_data(user_dir.name, storage_root)
                        # Asegurar campos mínimos
                        data.setdefault("owner_name", ud.get("name", "amigo"))
                        data.setdefault("business_name", ud.get("business_name", "tu negocio"))
                        data.setdefault("business_type", ud.get("business_type", "negocio"))
                        data.setdefault("schedule", ud.get("schedule", {"open":"08:00","close":"22:00"}))
                        data.setdefault("msgs", [])
                        data.setdefault("camera_connected", True)
                        data.setdefault("position_confirmed", True)
                        data.setdefault("image_b64", "")
                        data.setdefault("image_desc", "")
                        data.setdefault("scene_analysis", "")
                        data.setdefault("business_answers", [])
                        data.setdefault("hardware_step", 0)
                        data.setdefault("metrics", {"total_events":0,"total_alerts":0,"total_false_positives":0,"rules":{},"needs_review":False})
                        # Asegurar que confirmed_rules sea lista
                        if not isinstance(data.get("confirmed_rules"), list):
                            data["confirmed_rules"] = []
                        return data
        return None
    except Exception:
        return None
