"""
eva/eva_v2.py — Motor unificado de Eva v2.

NUEVA ARQUITECTURA (Testigo Puro):
   Qwen es TESTIGO, no juzga. Solo narra hechos observables.
   El sistema NO acusa, NO dice "violación", NO juzga a nadie.
   El usuario decide si algo es falta o no.

DOS MODOS:
   1. SETUP: Flujo conversacional camera-first para configurar el sistema.
      Eva identifica zona + tipo de negocio → selecciona plantilla.
      Pregunta abierta: "¿Qué te gustaría que vigile aquí?"
      Usuario responde libre → Eva convierte en frases de atención cortas.
   2. OS: Eva responde preguntas del usuario consultando el diario de
      eventos (JSON rico con narrativa + conteos).

FLUJO SETUP (fases deterministas):
   GREET → ZONE → HARDWARE → WAIT_IMAGE → ANALYZE → CONTEXT → PROMPT_BUILD → CONFIRM → DONE

*El usuario da datos reales en el chat (qué vigilar, horario, notas).
*Eva NO inventa nada — solo usa lo que el usuario dijo.

Una vez DONE, cualquier mensaje va a modo OS.
Cada evento del orquestador se guarda como JSON rico (diario del testigo).
"""

import asyncio
import base64
import json
import logging
import os
import re
import time
import unicodedata
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
from enum import Enum

from eva.tools import OPENAI_TOOLS_SCHEMA

_TOOLS_JSON_SCHEMA = """
Herramientas disponibles (responde SOLO JSON con "tool" y "params"):
- search_events: Busca eventos en el diario. params: query (texto, vacío=todos), date (today/yesterday/YYYY-MM-DD), camera_id (opcional), limit (1-10)
- get_activity_summary: Resume actividad del día (conteos, personas, platos, fundas). params: date (today/yesterday), camera_id (opcional)
- find_anomalies: Busca eventos con attention_hits (observaciones relevantes). params: min_severity (baja/media/alta/critica/observacion), date, camera_id, limit
- latest_events: Lista últimos análisis. params: limit (1-10), date, camera_id
- find_risks: Busca riesgos incendio/humo. params: date, camera_id, limit
- get_vigilance_config: Lee configuración de observación. params: camera_id
- update_vigilance_config: Actualiza observación. params: camera_id, mode (normal/sentinel), schedule, attention_phrases (lista), owner_notes (lista)
- get_latest_frame: Obtiene imagen reciente. params: camera_id
- analyze_frame: Analiza frame. params: camera_id, prompt
- identify_face: Identifica quién en cámara. params: camera_id
- list_employees: Lista empleados. params: {}
- save_event: Guarda evento. params: camera_id, summary, importance (baja/media/alta/critica)
- respond_directly: Responde sin herramientas. params: message
"""

logger = logging.getLogger(__name__)

QWEN_URL     = "http://localhost:8004/v1/chat/completions"
QWEN_TIMEOUT = 60
STORAGE_ROOT = Path("/home/sam/storage")
MAX_OS_TOOL_LIMIT = 10

NEW_CAMERA_INTENTS = {
    "__new_camera__",
    "nueva camara",
    "nueva cámara",
    "instalar camara",
    "instalar cámara",
    "instalar una camara",
    "instalar una cámara",
    "instalar una camara nueva",
    "instalar una cámara nueva",
    "agregar camara",
    "agregar cámara",
    "agregar una camara",
    "agregar una cámara",
    "crear camara",
    "crear cámara",
    "crear una camara",
    "crear una cámara",
    "añadir camara",
    "añadir cámara",
    "añadir una camara",
    "añadir una cámara",
    "poner camara",
    "poner cámara",
    "poner una camara",
    "poner una cámara",
    "montar camara",
    "montar cámara",
    "montar una camara",
    "montar una cámara",
    "configurar camara nueva",
    "configurar una camara nueva",
    "configurar una cámara nueva",
    "quiero una camara",
    "quiero una cámara",
    "quiero instalar una camara",
    "quiero instalar una cámara",
    "quiero instalar una camara nueva",
    "quiero instalar una cámara nueva",
    "quiero agregar una camara",
    "quiero agregar una cámara",
    "quiero añadir una camara",
    "quiero añadir una cámara",
    "quiero crear una camara",
    "quiero crear una cámara",
}

# ── Estado de sesiones ────────────────────────────────────────────────────────
_sessions: Dict[str, Dict[str, Any]] = {}

# ── Frame buffer ────────────────────────────────────────────────────────────────
_latest_frame: Dict[str, bytes] = {}
_latest_frame_time: Dict[str, float] = {}

def ingest_frame_for_eva(frame_bytes: bytes, camera_id: str = "default"):
    global _latest_frame, _latest_frame_time
    _latest_frame[camera_id] = frame_bytes
    _latest_frame_time[camera_id] = time.time()

def _get_frame(camera_id: str = "", user_id: str = "") -> Optional[bytes]:
    global _latest_frame, _latest_frame_time
    if camera_id and camera_id in _latest_frame:
        if time.time() - _latest_frame_time[camera_id] < 120:
            return _latest_frame[camera_id]
    for cid, frame in _latest_frame.items():
        if time.time() - _latest_frame_time.get(cid, 0) < 120:
            return frame
    try:
        from orchestrator import _grids
        for key, grid in _grids.items():
            fb = grid.get_last_frame_bytes()
            if fb:
                return fb
    except Exception:
        pass
    try:
        latest_jpg = None
        latest_mtime = 0
        for uid in [user_id, "default"]:
            if not uid:
                continue
            for f in glob.glob(str(STORAGE_ROOT / "users" / uid / "cameras" / "*" / "frames" / "*.jpg")):
                mtime = os.path.getmtime(f)
                if mtime > latest_mtime:
                    latest_mtime = mtime
                    latest_jpg = f
        if latest_jpg and (time.time() - latest_mtime) < 300:
            return Path(latest_jpg).read_bytes()
    except Exception:
        pass
    return None

def _configured_camera_ids(user_id: str) -> set:
    ud = _load_user_data(user_id)
    ids = set()
    cams = ud.get("cameras", []) if isinstance(ud.get("cameras"), list) else []
    for cam in cams:
        if cam.get("camera_id"):
            ids.add(cam["camera_id"])
        if cam.get("physical_camera_id"):
            ids.add(cam["physical_camera_id"])
        if cam.get("id"):
            ids.add(cam["id"])
    cam_root = STORAGE_ROOT / "users" / user_id / "cameras"
    if cam_root.exists():
        for cam_dir in cam_root.iterdir():
            cf = cam_dir / "camera.json"
            if not cam_dir.is_dir() or not cf.exists():
                continue
            try:
                cfg = json.loads(cf.read_text())
                vals = [cfg.get("camera_id"), cfg.get("id"), cfg.get("physical_camera_id")]
                if not any(vals):
                    continue
                ids.add(cam_dir.name)
                for key in ("camera_id", "id", "physical_camera_id"):
                    if cfg.get(key):
                        ids.add(cfg[key])
            except Exception:
                pass
    return ids

def _get_unconfigured_frame(user_id: str):
    ids = _configured_camera_ids(user_id)
    best = (None, "", 0.0)
    now = time.time()
    for cid, frame in _latest_frame.items():
        if cid in ids:
            continue
        ts = _latest_frame_time.get(cid, 0.0)
        if now - ts < 120 and ts > best[2]:
            best = (frame, cid, ts)
    try:
        cam_root = STORAGE_ROOT / "users" / user_id / "cameras"
        if cam_root.exists():
            for latest in cam_root.glob("*/frames/latest_raw.jpg"):
                cid = latest.parent.parent.name
                if cid in ids:
                    continue
                mtime = latest.stat().st_mtime
                if now - mtime < 300 and mtime > best[2]:
                    best = (latest.read_bytes(), cid, mtime)
    except Exception:
        pass
    return best[0], best[1]

# =============================================================================
# FASES DEL SETUP
# =============================================================================

class SetupPhase(str, Enum):
    GREET = "greet"
    ZONE = "zone"
    HARDWARE = "hardware"
    WAIT_IMAGE = "wait_image"
    ANALYZE = "analyze"
    CONTEXT = "context"
    PROMPT_BUILD = "prompt_build"
    CONFIRM = "confirm"
    DONE = "done"

# =============================================================================
# UTILIDADES
# =============================================================================

def _load_user_data(user_id: str) -> Dict:
    for p in [STORAGE_ROOT / "users" / user_id / "user.json", STORAGE_ROOT / user_id / "user.json"]:
        if p.exists():
            try: return json.loads(p.read_text())
            except Exception: pass
    return {}

def _save_user_data(user_id: str, data: Dict):
    uf = STORAGE_ROOT / "users" / user_id / "user.json"
    uf.parent.mkdir(parents=True, exist_ok=True)
    tmp = uf.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    tmp.replace(uf)

def _owner_name(ud: Dict) -> str:
    owner = ud.get("owner")
    if isinstance(owner, dict):
        return owner.get("name") or owner.get("owner_name") or ud.get("name") or ud.get("owner_name") or "amigo"
    return ud.get("name") or ud.get("owner_name") or "amigo"

def _count_configured_cameras(user_id: str, ud: Dict) -> int:
    cams = ud.get("cameras", []) if isinstance(ud.get("cameras"), list) else []
    cfg = [c for c in cams if c.get("camera_id") or c.get("id")]
    if cfg:
        return len(cfg)
    if "cameras" not in ud:
        ucd = STORAGE_ROOT / "users" / user_id / "cameras"
        if ucd.exists():
            return len([d for d in ucd.iterdir() if d.is_dir() and (d / "camera.json").exists()])
    return 0

def _load_session(session_id: str) -> Optional[Dict]:
    s = _sessions.get(session_id)
    if s:
        return s
    for user_dir in STORAGE_ROOT.glob("users/*"):
        for cam_dir in user_dir.glob("cameras/*"):
            sf = cam_dir / "eva_session_v2.json"
            if sf.exists():
                try:
                    d = json.loads(sf.read_text())
                    if d.get("session_id") == session_id:
                        d.setdefault("msgs", [])
                        d.setdefault("os_greeted", True)
                        _sessions[session_id] = d
                        return d
                except Exception: pass
    return None

def _pending_session_for_user(user_id: str) -> Optional[Dict]:
    best = None
    best_time = 0.0
    for s in _sessions.values():
        if s.get("user_id") != user_id:
            continue
        if s.get("phase") in {"done", "os"}:
            continue
        if s.get("phase") not in ("hardware", "wait_image", "analyze", "context", "prompt_build", "confirm"):
            continue
        t = float(s.get("created_at", 0) or 0)
        if t >= best_time:
            best_time = t
            best = s
    for user_dir in STORAGE_ROOT.glob("users/*"):
        for cam_dir in user_dir.glob("cameras/*"):
            sf = cam_dir / "eva_session_v2.json"
            if not sf.exists():
                continue
            try:
                d = json.loads(sf.read_text())
            except Exception:
                continue
            if d.get("user_id") != user_id:
                continue
            if d.get("phase") in {"done", "os"}:
                continue
            if d.get("phase") not in ("hardware", "wait_image", "analyze", "context", "prompt_build", "confirm"):
                continue
            t = float(d.get("created_at", 0) or sf.stat().st_mtime or 0)
            if t >= best_time:
                best_time = t
                best = d
    if best:
        best.setdefault("msgs", [])
        _sessions[best.get("session_id", f"pending_{user_id}")] = best
    return best

def _save_session_to_disk(session: Dict):
    try:
        cam = session.get("camera_id", "")
        uid = session.get("user_id", "")
        if not cam or not uid:
            return
        sf = STORAGE_ROOT / "users" / uid / "cameras" / cam / "eva_session_v2.json"
        sf.parent.mkdir(parents=True, exist_ok=True)
        sf.write_text(json.dumps({k: v for k, v in session.items() if k != "msgs"}, indent=2, ensure_ascii=False))
    except Exception as e:
        logger.error(f"Error guardando sesión: {e}")

def _resize(img: bytes, max_px: int = 500) -> bytes:
    try:
        from gateway_resize import resize_image
        return resize_image(img, max_size=max_px)
    except Exception:
        return img

# =============================================================================
# EJEMPLOS SEGÚN TIPO DE NEGOCIO
# =============================================================================

def _get_zone_examples(biz_type: str) -> str:
    bt = (biz_type or "").lower().strip()
    if bt in ("restaurant","restaurante","bar","comedor"):
        return "cocina, caja, comedor, entrada, almacén"
    if bt in ("finca","agricultura","granja","campo"):
        return "corral, entrada, patio, galpón, zona de animales"
    if bt in ("retail","colmado","tienda","supermercado"):
        return "caja, entrada, pasillos, almacén, mostrador"
    return "entrada, zona principal, caja, almacén, patio"

def _get_concern_examples(biz_type: str, zone: str) -> str:
    bt = (biz_type or "").lower().strip()
    if bt in ("restaurant","restaurante","bar","comedor"):
        return "robo, que despachen sin facturar, incendio..."
    if bt in ("finca","agricultura","granja","campo"):
        return "que se escapen animales, intrusos, robo..."
    if bt in ("retail","colmado","tienda","supermercado"):
        return "robo, hurto, que falte mercadería..."
    return "robo, que entren personas sospechosas, que falte mercadería..."

# =============================================================================
# LLM HELPERS
# =============================================================================

async def _call_qwen(messages: list, max_tokens: int = 600, tools: list = None, temperature: float = 0.3) -> dict:
    try:
        payload = {
            "model": "qwen",
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if tools:
            payload["tools"] = tools
        async with httpx.AsyncClient(timeout=QWEN_TIMEOUT) as cl:
            r = await cl.post(QWEN_URL, json=payload)
            if r.status_code == 200:
                resp = r.json()
                msg = resp["choices"][0]["message"]
                result = {
                    "content": msg.get("content", "") or "",
                    "tool_calls": msg.get("tool_calls", []),
                    "finish_reason": resp["choices"][0].get("finish_reason", "")
                }
                return result
    except Exception as e:
        logger.error(f"Qwen error: {e}")
    return {"content": "", "tool_calls": [], "finish_reason": "error"}

async def _describe_frame(b64: str, zone: str = "", biz_type: str = "") -> str:
    try:
        small = _resize(base64.b64decode(b64), 640)
        small_b64 = base64.b64encode(small).decode()
        ctx = ""
        if zone: ctx += f"La cámara está instalada en la zona: {zone}. "
        if biz_type: ctx += f"El negocio es un {biz_type}. "
        prompt = (f"Analiza esta imagen de seguridad. {ctx}\n"
                  "Describe:\n1. Qué zona ves realmente\n2. Objetos, personas, animales\n"
                  "3. ¿Coincide con la zona indicada?\n4. Iluminación (buena/regular/mala)\n"
                  "5. ¿Recomendarías ajustar la posición?\nResponde en español, 5-7 líneas, específico y práctico.")
        msgs = [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{small_b64}"}},
            {"type": "text", "text": prompt}]}]
        result = await _call_qwen(msgs, 300)
        return result.get("content", "") if result.get("content") else "No pude analizar la imagen."
    except Exception as e:
        logger.error(f"Error describiendo frame: {e}")
        return "No pude analizar la imagen."

async def _analyze_frame_for_prompt(b64: str, zone: str, biz_type: str) -> Dict:
    try:
        small = _resize(base64.b64decode(b64), 640)
        small_b64 = base64.b64encode(small).decode()
        prompt = (f"Analiza esta imagen de un {biz_type}. Zona: {zone}.\n"
                  "¿Coincide lo que ves con la zona? Extrae JSON:\n"
                  '{"zona_real":"...","coincide_zona":true/false,"objetos":[...],'
                  '"personas_estimadas":n,"iluminacion":"buena/regular/mala",'
                  '"es_zona_correcta":true/false,"sugerencia_posicion":"..."}\n'
                  "Responde SOLO JSON.")
        msgs = [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{small_b64}"}},
            {"type": "text", "text": prompt}]}]
        result = await _call_qwen(msgs, 400)
        d = _parse_json_response(result.get("content", ""))
        return d if d else {}
    except Exception as e:
        logger.error(f"Error analizando: {e}")
        return {}

def _parse_json_response(content: str) -> dict:
    content = re.sub(r'^```json\s*', '', content)
    content = re.sub(r'\s*```$', '', content).strip()
    content = re.sub(r"\{[\s]*\.\.\.[\s]*\}", "{}", content)
    content = re.sub(r"\[[\s]*\.\.\.[\s]*\]", "[]", content)
    try:
        m = re.search(r'\{.*\}', content, re.DOTALL)
        if m: return json.loads(m.group())
    except Exception:
        pass
    for key in ("summary", "description"):
        key_match = re.search(rf'"{key}"\s*:\s*"((?:\\.|[^"\\])*)"', content)
        if key_match:
            try:
                return {key: json.loads('"' + key_match.group(1) + '"')}
            except Exception:
                return {key: key_match.group(1)}
    return {}


# =============================================================================
# SYSTEM PROMPT
# =============================================================================

async def _build_system_prompt(session: Dict) -> str:
    """Construye el prompt de testigo puro para una cámara (vía Qwen)."""
    cfg = {
        "zone": session.get("zone", "la zona"),
        "business_name": session.get("business_name", "el negocio"),
        "business_type": session.get("business_type", "negocio"),
        "schedule": session.get("schedule", {"open": "08:00", "close": "22:00"}),
        "concern": session.get("concern", "seguridad general"),
        "attention_phrases": session.get("attention_phrases", []),
        "owner_notes": session.get("owner_notes", []),
    }
    prompt = (
        f"Eres un experto en seguridad comercial en República Dominicana.\n"
        f"Diseña un system_prompt de observación para una cámara.\n\n"
        f"=== DATOS DEL NEGOCIO ===\n"
        f"- Nombre: {cfg['business_name']}\n- Tipo: {cfg['business_type']}\n"
        f"- Zona: {cfg['zone']}\n"
        f"- Horario: {cfg['schedule'].get('open','08:00')} a {cfg['schedule'].get('close','22:00')}\n"
        f"- Preocupación: {cfg['concern']}\n"
    )
    if cfg["attention_phrases"]:
        prompt += f"- Frases de atención del dueño:\n"
        for p in cfg["attention_phrases"]:
            prompt += f"  • {p}\n"
    prompt += (
        f"\n=== TU TAREA ===\n"
        f"Genera el system_prompt que usará un TESTIGO de IA para observar grids de imágenes.\n"
        f"El testigo NUNCA juzga — solo describe hechos visibles.\n"
        f"1. Rol del testigo (observador neutral)\n"
        f"2. Contexto del negocio y zona\n"
        f"3. Frases de atención del dueño (qué debe observar específicamente)\n"
        f"4. Qué describir en cada análisis\n"
        f"5. Qué NO decir (nunca juzgar, nunca decir 'violación', 'anomalía', 'sospechoso')\n"
        f"6. Formato de respuesta JSON\n\n"
        f"IMPORTANTE: El testigo solo narra hechos. No juzga. No acusa. No dice si está bien o mal.\n"
        f"Responde SOLO el system_prompt en texto plano. Máx 600 palabras."
    )
    msgs = [{"role": "system", "content": prompt}]
    img_b64 = session.get("image_b64", "")
    if img_b64:
        small = _resize(base64.b64decode(img_b64), 500)
        sb64 = base64.b64encode(small).decode()
        msgs.append({"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{sb64}"}},
            {"type": "text", "text": "Basándote en esta imagen y el contexto, genera el system_prompt de testigo."}]})
    else:
        msgs.append({"role": "user", "content": "Genera el system_prompt de testigo basándote en los datos."})
    result = await _call_qwen(msgs, 600)
    return result.get("content", "") if result.get("content") else _build_fallback_prompt(session)

def _build_fallback_prompt(session: Dict) -> str:
    concern = session.get("concern", "seguridad general")
    forbidden = session.get("forbidden_events", "actividad sospechosa o no autorizada")
    normal = session.get("normal_state", "actividad normal de la zona")
    authorized = session.get("authorized_people", "empleados autorizados")
    objects = session.get("important_objects", "dinero, productos, puerta, caja registradora")
    severity = session.get("severity_rules", "robo, incendio y persona no autorizada son críticos")
    return (f"Eres testigo observando {session.get('zone','zona')} de "
            f"{session.get('business_name','negocio')}. "
            f"Horario: {session.get('schedule',{}).get('open','08:00')}-"
            f"{session.get('schedule',{}).get('close','22:00')}. "
            f"Describe solo hechos visibles. Nunca juzgues."
            f"Responde SOLO JSON: {{'personas':[...],'transaccion':{{...}},"
            f"'counts':{{'clientes':0,'platos_visibles':0}},"
            f"'attention_hits':[...],'resumen':'...'}}")

# =============================================================================
# DETECCIÓN DE INTENCIONES
# =============================================================================

_YES = {"si","sí","dale","perfecto","exacto","correcto","bueno","bien",
        "ok","vale","claro","seguro","ya","ajá","aha","de acuerdo",
        "me parece","está bien","eso es","confirmar","va","listas","hecho"}
_NO = {"no","otra","cambiar","diferente","no me gusta","no sirve",
       "paso","otra opción","no esta bien","no quiero"}

async def _is_intent_confirmed(message: str, context: str, model_func) -> bool:
    """Usa Qwen para entender si el usuario confirmó la intención."""
    try:
        prompt = (
            f"Analiza si el usuario confirmó una acción. Responde SOLO con: 'si' (confirmado), 'no' (no confirmado), 'maybe' (incógnita).\n\n"
            f"Contexto de la conversación: {context}\n"
            f"Mensaje del usuario: '{message}'\n\n"
            f"¿El usuario confirmó la acción del sistema? Responde con 'si', 'no' o 'maybe'."
        )
        response = await model_func([{"role": "user", "content": prompt}])
        clean_response = response.strip().lower()
        return clean_response in ("si", "sí", "true", "yes")
    except Exception:
        return False

def _is_yes(m): m=m.lower().strip(); return m in _YES or any(m.startswith(w) for w in _YES)
def _is_no(m): m=m.lower().strip(); return m in _NO or any(m.startswith(w) for w in _NO)

def _is_position_confirm(m):
    m = m.lower().strip()
    words = {"mismo lugar","dejarla","dejarla así","dejala","dejala así",
             "la dejamos","dejarla en","dejemosla en","no mover","no la muevo",
             "no la movemos","seguir así","continuar así","quedarse","quedarnos",
             "ok","dale","listo","perfecto","bien","va","de acuerdo",
             "donde esta","se queda","dejarla donde esta","dejala donde esta",
             "si","sí","claro","correcto","excelente"}
    return any(w in m for w in words)

def _is_show_frame(m):
    return any(w in m.lower() for w in {"muestra","ver","enseña","qué ves","que ves",
             "frame","la cámara","imagen de la cámara","muestra el frame"})

def _is_frame_problem(m):
    return any(w in m.lower() for w in {"no se ve","no veo","no llega","se cayo",
             "se cayó","no funciona","no jala","desconecto","no hay imagen",
             "ya no se ve","ya no llega","se apagó","no prende"})

_CONTEXT_STEPS = [
    "concern",
    "attention_phrases",
    "owner_notes",
]

def _is_skippable_answer(m):
    m = m.lower().strip()
    return m in {"nose", "no se", "no sé", "cualquier cosa", "normal", "lo normal",
                 "lo de siempre", "no importa", "ninguno", "nada especial"}

def _clean_context_answer(m, default):
    m = m.strip()
    if not m or _is_yes(m) or _is_skippable_answer(m):
        return default
    return m

def _parse_attention_phrases(text):
    """Parsea frases de atención desde texto libre del usuario.
    Separa por comas, 'y', 'que', punto y coma.
    """
    if not text:
        return []
    text = text.replace(";", ",").replace("\n", ",")
    parts = re.split(r",\s*|\s+y\s+|\s+que\s+", text)
    phrases = []
    for p in parts:
        p = p.strip().strip(".").strip()
        if len(p) > 3 and len(p) < 200:
            phrases.append(p)
    return phrases[:10]

def _next_context_step(session):
    step = session.get("context_step") or "concern"
    if step in _CONTEXT_STEPS:
        idx = _CONTEXT_STEPS.index(step)
        if idx + 1 < len(_CONTEXT_STEPS):
            return _CONTEXT_STEPS[idx + 1]
    return "prompt_build"

def _context_question(session, step, first):
    zone = session.get("zone","esa zona")
    biz_type = session.get("business_type","")
    examples = _get_concern_examples(biz_type, zone)
    if step == "concern":
        return f"Perfecto.\n\n¿Qué es lo que más te preocupa de seguridad en {zone}?\nPor ejemplo: {examples}"
    if step == "attention_phrases":
        return (
            f"Perfecto. Ya anoté tu preocupación: {session.get('concern','')}\n\n"
            f"Ahora dime QUÉ QUIERES QUE VIGILE en {zone}.\n"
            f"Escríbelo con tus palabras, como si me contaras:\n"
            f"• ¿Qué hace el cajero que no debería hacer?\n"
            f"• ¿Qué acción quieres que te notifique?\n"
            f"• ¿Qué comportamiento te parece raro ver aquí?\n\n"
            f"Por ejemplo: 'que el cajero se meta la mano en el bolsillo después de cobrar', "
            f"'que el dinero no entre en la caja', 'que un cliente se lleve algo sin pagar'..."
        )
    if step == "owner_notes":
        return (
            f"Perfecto. Ya tengo lo que quieres vigilar.\n\n"
            f"¿Hay algo que me quieras aclarar? Algo que veas a veces y NO sea falta.\n"
            f"Por ejemplo: 'el cajero se toca el bolsillo para sacar el teléfono, eso es normal'.\n\n"
            f"Si no hay nada más, solo di 'no' o 'no más' si terminamos."
        )
    return f"Gracias {first}. Ya tengo el contexto. Voy a crear el sistema de observación para tu {zone}..."

def _context_default(step, zone):
    if step == "concern":
        return f"seguridad general en {zone}"
    if step == "attention_phrases":
        return ""
    if step == "owner_notes":
        return ""
    return "seguridad general"

def _is_edit_request(m):
    m = m.lower().strip()
    return any(w in m for w in {"modificar", "cambiar", "editar", "ajustar", "corregir"})

def _parse_schedule(m):
    nums = re.findall(r"\b\d{1,2}:\d{2}\b", m)
    if len(nums) >= 2:
        return {"open": nums[0], "close": nums[1]}
    words = m.lower()
    if "mañana" in words or "manana" in words or "am" in words:
        am = re.search(r"(\d{1,2})(?::(\d{2}))?\s*(?:am|a\.m\.|mañana|manana)", words)
        pm = re.search(r"(\d{1,2})(?::(\d{2}))?\s*(?:pm|p\.m\.|noche|tarde)", words)
        if am and pm:
            oh = int(am.group(1)); om = int(am.group(2) or 0)
            ch = int(pm.group(1)); cm = int(pm.group(2) or 0)
            if ch < 12: ch += 12
            return {"open": f"{oh:02d}:{om:02d}", "close": f"{ch:02d}:{cm:02d}"}
    return None


def _vigilance_camera_id(message):
    m = message.lower()
    if "caja" in m or "register" in m or "cash" in m:
        return "cam_1781039699"
    match = re.search(r"cam[_-]?\d+", m)
    return match.group(0) if match else ""


async def _pick_best_camera_id(user_id):
    camera_base = STORAGE_ROOT / "users" / user_id / "cameras"
    if camera_base.exists():
        cams = sorted([p for p in camera_base.iterdir() if p.is_dir()], key=lambda p: p.stat().st_mtime, reverse=True)
        if cams:
            return cams[0].name
    ud = _load_user_data(user_id)
    cams = [c for c in ud.get("cameras", []) if c.get("active")]
    if not cams:
        return ""
    return cams[0].get("camera_id") or cams[0].get("id") or ""


def _extract_camera_id_from_message(message):
    return _vigilance_camera_id(message)


def _build_vigilance_update_from_message(user_id, camera_id, message):
    from eva.tools import _load_camera_config, normalize_camera_vigilance_config
    m = _normalize_text(message)
    vigilance = {}
    schedule = None
    mode = None
    if any(w in m for w in ("centinela", "modo centinela", "activar centinela", "activa centinela")):
        mode = "sentinel"
        vigilance = {"sentinel_mode": {"enabled": True}, "enabled": True}
    elif any(w in m for w in ("modo estandar", "modo estándar", "desactiva centinela", "desactivar centinela", "modo normal")):
        mode = "normal"
        vigilance = {"normal_mode": {"enabled": True}, "enabled": True}
    if any(w in m for w in ("sensibilidad", "sensible", "critica", "alta", "media", "baja")):
        sensitivity = "alta"
        if any(w in m for w in ("critica", "crítica")):
            sensitivity = "critica"
        elif "baja" in m:
            sensitivity = "baja"
        elif "media" in m:
            sensitivity = "media"
        vigilance["normal_mode"] = {**(vigilance.get("normal_mode") or {}), "sensitivity": sensitivity}
    for marker in ("quita falsas alarmas", "quitar falsas alarmas", "no alertes por", "no alertar por", "es normal", "no es falta"):
        behavior = _clean_behavior(_extract_behavior_after(message, [marker]))
        if behavior:
            current = _load_current_vigilance_normal(user_id, camera_id)
            existing = current.get("owner_notes", [])
            note = f"Nota del dueño: cuando pasa '{behavior}', es normal, no lo menciones."
            if note not in existing:
                existing.append(note)
            vigilance["owner_notes"] = existing
            break
    for marker in ("solo alerta si", "alerta si", "alertar si", "notifícame cuando", "vigila cuando"):
        behavior = _clean_behavior(_extract_behavior_after(message, [marker]))
        if behavior:
            current = _load_current_vigilance_normal(user_id, camera_id)
            existing = current.get("attention_phrases", [])
            if behavior not in existing:
                existing.append(behavior)
            vigilance["attention_phrases"] = existing
            break
    if any(w in m for w in ("horario", "abre", "cierra", "apertura", "cierre")):
        schedule = _parse_schedule(message)
        if not schedule:
            return {"needs_clarification": True, "text": "Para cambiar el horario necesito la hora de apertura y cierre. Por ejemplo: “abre 8:00am y cierra 10:00pm”."}
    return {"vigilance": vigilance, "schedule": schedule, "mode": mode}


def _load_current_vigilance_normal(user_id, camera_id):
    try:
        from eva.tools import _load_camera_config, normalize_camera_vigilance_config
        current = normalize_camera_vigilance_config(_load_camera_config(user_id, camera_id))
        normal = current.get("vigilance", {}).get("normal_mode", {})
        return normal if isinstance(normal, dict) else {}
    except Exception:
        return {}


def _list_from_config(value):
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    if isinstance(value, str):
        return [v.strip() for v in value.split("\n") if v.strip()]
    return []


def _extract_behavior_after(message, markers):
    text = message.lower()
    for marker in markers:
        idx = text.find(marker)
        if idx >= 0:
            value = text[idx + len(marker):].strip(" .,-;:")
            return value[:180] if value else ""
    return ""


def _clean_behavior(value):
    value = (value or "").strip().strip(" .,-;:¡!¿?")
    if not value:
        return ""
    value = value.replace("que ", "", 1) if value.startswith("que ") else value
    return value[:180]

def _config_summary_text(session, first):
    zone = session.get("zone","")
    biz = session.get("business_name","")
    concern = session.get("concern","")
    sched = session.get("schedule",{"open":"08:00","close":"22:00"})
    attention_phrases = session.get("attention_phrases", [])
    owner_notes = session.get("owner_notes", [])
    lines = [
        f"Perfecto {first}. Configuración 📋",
        f"",
        f"📷 Cámara: {zone}",
        f"🏢 Negocio: {biz}",
        f"🔒 Preocupación: {concern}",
        f"⏰ Horario: {sched.get('open','08:00')} a {sched.get('close','22:00')}",
    ]
    if attention_phrases:
        lines.append(f"")
        lines.append(f"🔍 Frases de atención ({len(ph_attention_phrases)}):")
        for p in attention_phrases[:5]:
            lines.append(f"  • {p}")
    if owner_notes:
        lines.append(f"")
        lines.append(f"📝 Notas del dueño:")
        for n in owner_notes[:3]:
            lines.append(f"  • {n}")
    lines.append(f"")
    lines.append(f"¿Apruebas esta configuración?")
    return "\n".join(lines)

# =============================================================================
# HANDLER PRINCIPAL
# =============================================================================

def _normalize_text(text: str) -> str:
    if not text:
        return ""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return text.lower().strip()

def _is_new_camera_intent(text: str) -> bool:
    text = _normalize_text(text)
    if any(w in text for w in ("no se pudo", "fallo", "falló", "error", "confund", "desubic")):
        return False
    if text in NEW_CAMERA_INTENTS:
        return True
    return any(phrase in text for phrase in (
        "instalar camara", "agregar camara", "anadir camara", "poner camara",
        "montar camara", "crear camara", "nueva camara",
        "configurar una camara nueva", "configurar camara nueva",
        "quiero una camara", "quiero instalar", "quiero agregar", "quiero anadir",
        "quiero crear", "quiero poner", "quiero montar",
    ))

def _is_os_intent(text: str) -> bool:
    text = _normalize_text(text)
    if text.startswith("__") and any(token in text for token in (
        "daily_summary", "yesterday_summary", "search_events", "adjust_protection",
        "live_status", "alert_review",
    )):
        return True
    if _is_new_camera_intent(text):
        return False
    if len(text) < 3:
        return False
    return any(phrase in text for phrase in (
        "resumen", "cierre", "balance", "que paso", "que ha pasado", "que ha visto",
        "ha visto algo", "visto algo", "que hay hoy", "que esta pasando",
        "pasando ahora", "que ocurre", "que sucede",
        "como va", "como esta", "todo bien", "sin novedad", "hay novedades", "alguna novedad",
        "novedades", "novedad", "alerta", "alertas", "sospechoso", "sospechos", "sospechosa",
        "actividad sospechosa", "anomalia", "anomalias", "violacion", "violaciones",
        "ajustar proteccion", "centinela", "vigilancia", "sensibilidad", "falsas alarmas",
        "busca en el diario", "buscar en el diario", "investiga en el diario", "buscame",
        "encuentra", "quien vino", "persona con", "gorra", "camisa", "color",
        "cuantos clientes", "clientes hay", "clientes entraron", "hay clientes",
        "clientes ahora", "cuantas personas", "personas hay", "gente hay", "cuanta gente",
        "quien eres", "que eres", "para que sirves", "eres una persona", "eres humano",
        "como te llamas", "confund", "desubic", "sigue en configuracion",
        "no se pudo instalar", "fallo la instalacion", "cuanto gane", "ganancia", "ventas",
        "ingresos", "incendio", "humo", "fuego", "riesgo", "diario", "eventos",
        "ultimos analisis", "que ha pasado hoy", "que ha pasado ayer", "historial",
        "ultimo evento", "ultimos eventos", "hubo algo", "paso algo",
    ))


async def handle_eva_v2(user_id, message, session_id, cam_id=None, include_frame=False, storage_root=STORAGE_ROOT):
    msg_norm = _normalize_text(message or "")
    pending_setup = _pending_session_for_user(user_id)
    ud = _load_user_data(user_id)
    cam_count = _count_configured_cameras(user_id, ud)
    if pending_setup and pending_setup.get("phase") not in (SetupPhase.DONE.value, "os") and not _is_os_intent(msg_norm) and cam_count == 0:
        return await _resume_pending_setup(pending_setup, user_id, session_id, message, storage_root)
    if _is_os_intent(msg_norm) and not _is_new_camera_intent(msg_norm):
        sid = session_id or f"chat_{user_id}_{int(time.time())}"
        session = _load_session(sid)
        if not session or session.get("user_id") != user_id or session.get("phase") not in (SetupPhase.DONE.value, "os"):
            session = _make_os_session(user_id, sid)
            _sessions[sid] = session
        return await _handle_os_mode(session, user_id, message, sid)
    if _is_new_camera_intent(msg_norm):
        pending = _pending_session_for_user(user_id)
        if pending and cam_count == 0:
            return await _resume_pending_setup(pending, user_id, session_id, message, storage_root)
        ud = _load_user_data(user_id)
        cam_count = _count_configured_cameras(user_id, ud)
        session = {
            "session_id": session_id, "user_id": user_id,
            "phase": SetupPhase.ZONE.value,
            "owner_name": _owner_name(ud),
            "business_name": ud.get("business_name","") or "negocio",
            "business_type": ud.get("business_type","") or "negocio",
            "schedule": ud.get("schedule",{"open":"08:00","close":"22:00"}),
            "cameras_count": cam_count, "zone":"", "concern":"",
            "expected_count":"", "employees":"", "business_answers":[],
            "camera_id": cam_id or "", "camera_connected": False,
            "image_b64":"", "image_sent": False, "image_desc":"", "image_analysis":{},
            "context_step": "position", "position_confirmed": False,
            "normal_state": "", "authorized_people": "", "important_objects": "",
            "forbidden_events": "", "severity_rules": "", "risk_hours": "",
            "system_prompt":"", "wait_attempts":0, "msgs":[], "created_at":time.time(),
        }
        _sessions[session_id] = session
        first = session["owner_name"].split()[0] if session.get("owner_name") else "amigo"
        examples = _get_zone_examples(session.get("business_type",""))
        return _mk_resp(session, f"Dale, {first}. Vamos a instalar una camara nueva.\n\nDonde la vas a poner? Por ejemplo: {examples}...")
    session = _load_session(session_id)
    if session and session.get("user_id") != user_id:
        session = None
    if not session:
        ud = _load_user_data(user_id)
        cam_count = _count_configured_cameras(user_id, ud)
        session = {
            "session_id": session_id, "user_id": user_id,
            "phase": SetupPhase.GREET.value,
            "owner_name": _owner_name(ud),
            "business_name": ud.get("business_name",""),
            "business_type": ud.get("business_type",""),
            "schedule": ud.get("schedule",{"open":"08:00","close":"22:00"}),
            "cameras_count": cam_count, "zone":"", "concern":"",
            "expected_count":"", "employees":"", "business_answers":[],
            "camera_id": cam_id or "", "camera_connected": False,
            "image_b64":"", "image_sent": False, "image_desc":"", "image_analysis":{},
            "context_step": "position", "position_confirmed": False,
            "normal_state": "", "authorized_people": "", "important_objects": "",
            "forbidden_events": "", "severity_rules": "", "risk_hours": "",
            "system_prompt":"", "wait_attempts":0, "msgs":[], "created_at":time.time(),
        }
        _sessions[session_id] = session
        if cam_count > 0:
            session["phase"] = SetupPhase.DONE.value
            return await _handle_os_mode(session, user_id, message, session_id)

    phase = session["phase"]
    first = session["owner_name"].split()[0] if session.get("owner_name") else "amigo"
    if phase == SetupPhase.DONE.value:
        return await _handle_os_mode(session, user_id, message, session_id)
    return await _handle_setup(session, user_id, message, session_id, cam_id, storage_root, include_frame)


def _make_os_session(user_id: str, session_id: str) -> dict:
    ud = _load_user_data(user_id)
    cam_count = _count_configured_cameras(user_id, ud)
    return {
        "session_id": session_id or f"chat_{user_id}_{int(time.time())}",
        "user_id": user_id,
        "phase": SetupPhase.DONE.value,
        "owner_name": _owner_name(ud),
        "business_name": ud.get("business_name",""),
        "business_type": ud.get("business_type",""),
        "schedule": ud.get("schedule",{"open":"08:00","close":"22:00"}),
        "cameras_count": cam_count, "zone":"", "concern":"",
        "expected_count":"", "employees":"", "business_answers":[],
        "camera_id": "", "camera_connected": False,
        "image_b64":"", "image_sent": False, "image_desc":"", "image_analysis":{},
        "context_step": "", "position_confirmed": False,
        "normal_state": "", "authorized_people": "", "important_objects": "",
        "forbidden_events": "", "severity_rules": "", "risk_hours": "",
        "system_prompt":"", "wait_attempts":0, "msgs":[], "created_at":time.time(),
        "os_greeted": True,
    }


async def _resume_pending_setup(pending, user_id, session_id, message, storage_root):
    sid = pending.get("session_id") or session_id or f"chat_{pending.get('user_id','user')}_{int(time.time())}"
    pending["session_id"] = sid
    _sessions[sid] = pending
    return await _handle_setup(pending, user_id, message, sid, pending.get("camera_id", ""), storage_root, False)


async def _handle_daily_summary(session, user_id, message, session_id):
    first = session["owner_name"].split()[0] if session.get("owner_name") else "amigo"
    ud = _load_user_data(user_id)
    cam_count = len([c for c in ud.get("cameras",[]) if c.get("active")]) if ud.get("cameras") else 0
    suggestions = _get_business_suggestions_list(ud.get("business_type",""), cam_count, first)

    from eva.tools import tool_get_activity_summary
    result = await tool_get_activity_summary(user_id, "today")
    total = result.get("total_events", 0)
    attention_events = result.get("attention_events", 0)
    persons_total = result.get("persons_total", 0)
    last_summary = result.get("last_summary", "")
    details = result.get("details", {})
    last_yolo = result.get("last_yolo", {})
    counts_total = result.get("counts_total", {})

    if total == 0:
        text = f"Hoy no se han registrado análisis todavía. La cámara está activa y Eva está atenta a cualquier actividad."
        session["msgs"].append({"role":"assistant","content":text})
        _sessions[session_id] = session
        return _mk_resp(session, text, suggestions=suggestions)

    lines = []
    lines.append(f"Resumen del día: Hoy se realizaron {total} análisis de seguridad.")
    lines.append("")

    if attention_events > 0:
        lines.append(f"🔍 {attention_events} evento(s) coincidieron con lo que me pediste vigilar.")
    else:
        lines.append("✅ No se detectaron coincidencias con lo que me pediste vigilar.")

    if persons_total > 0:
        lines.append(f"👥 Personas en la escena: hasta {persons_total} a la vez (según tracker).")

    platos = counts_total.get("platos", 0)
    bebidas = counts_total.get("bebidas", 0)
    fundas = counts_total.get("fundas", 0)
    clientes = counts_total.get("clientes_estimado", 0)

    if platos > 0:
        lines.append(f"🍽️ Platos visibles en total: ~{platos}.")
    if bebidas > 0:
        lines.append(f"🥤 Bebidas visibles: ~{bebidas}.")
    if fundas > 0:
        lines.append(f"🛍️ Fundas utilizadas: ~{fundas}.")
    if clientes > 0:
        lines.append(f"🧑‍🤝‍🧑 Clientes observados: ~{clientes}.")

    lines.append(f"📊 Objetos detectados en último análisis: {last_yolo.get('count', 0)}.")
    classes = last_yolo.get("classes", [])
    if classes:
        lines.append(f"   Tipos: {', '.join(classes[:5])}.")

    if last_summary:
        lines.append(f"")
        lines.append(f"📝 Último análisis: {last_summary[:200]}")

    scene = details.get("scene_context", "")
    if scene:
        lines.append(f"")
        lines.append(f"🏪 Contexto: {scene}")

    text = "\n".join(lines)

    notable_events = result.get("notable_events", [])

    session["msgs"].append({"role":"assistant","content":text})
    _sessions[session_id] = session
    return _mk_resp(session, text, suggestions=suggestions, events_found=notable_events)


def _parse_hermes_tool_call(content):
    """Parsea respuestas de hermes-style tool calls del modelo Qwen."""
    if not content:
        return None
    import re
    pattern = r'<tool_call>\s*(\{.*?\})\s*</tool_call>'
    match = re.search(pattern, content, re.DOTALL)
    if match:
        try:
            data = json.loads(match.group(1))
            if "name" in data:
                return data
        except json.JSONDecodeError:
            pass
    stripped = content.strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        try:
            data = json.loads(stripped)
            if "name" in data:
                return data
            if "tool" in data:
                return {"name": data.pop("tool"), "arguments": data.get("params", data.get("arguments", {}))}
        except (json.JSONDecodeError, TypeError):
            pass
    json_match = re.search(r'\{[^{}]*"name"[^{}]*\}', content)
    if json_match:
        try:
            data = json.loads(json_match.group())
            if "name" in data:
                return data
        except json.JSONDecodeError:
            pass
    return None


async def _detect_intent_and_route(user_id, message, first, recent, cam_count, session):
    """Detecta intenciones comunes y las responde directamente sin pasar por el LLM."""
    msg_norm = _normalize_text(message)

    if any(p in msg_norm for p in ["cuantas personas", "cuántas personas", "cuantas personas hoy", "cuántas personas hoy", "cuantas personas has", "cuántas personas has"]):
        from eva.tools import tool_get_activity_summary
        result = await tool_get_activity_summary(user_id, "today")
        total = result.get("total_events", 0)
        persons = result.get("persons_total", 0)
        if total == 0:
            return {"text": f"Hoy no se han registrado personas todavía. La cámara está activa y Eva está atenta."}
        return {"text": f"Hoy se detectaron {persons} persona(s) en {total} análisis. Último análisis: {result.get('last_summary', '')[:150]}"}

    if any(p in msg_norm for p in ["cuantos clientes", "cuántos clientes", "cuantas clientes", "cuántas clientes", "cliente hoy"]):
        from eva.tools import tool_search_events
        result = await tool_search_events(user_id, query="cliente", date="today", limit=20)
        found = result.get("found", 0)
        events = result.get("events", [])
        if found == 0:
            return {"text": f"No se han registrado clientes específicos hoy según el registro disponible."}
        return {"text": f"Se detectaron {found} evento(s) relacionado(s) con clientes hoy.", "events": events[:5]}

    if any(p in msg_norm for p in ["cuantas mulheres", "cuántas mujeres", "mujeres hoy", "mujeres detectadas", "cuantas mujeres", "cuanta mujer"]):
        from eva.tools import tool_search_events
        result = await tool_search_events(user_id, person_class="mujer", date="today", limit=20)
        found = result.get("found", 0)
        # Count real women from qwen_details using event metadata
        events = result.get("events", [])
        total_mujeres = sum((e.get("qwen_json", {}).get("details", {}).get("count_mujeres", 0) or 0) for e in events)
        if found == 0:
            return {"text": f"No se han registrado mujeres hoy según el registro disponible."}
        return {"text": f"Se detectaron {total_mujeres} mujer(es) en {found} evento(s) hoy.", "events": events[:5]}

    if any(p in msg_norm for p in ["cuantos hombres", "cuántos hombres", "hombres hoy", "cuanto hombre"]):
        from eva.tools import tool_search_events
        result = await tool_search_events(user_id, person_class="hombre", date="today", limit=20)
        found = result.get("found", 0)
        events = result.get("events", [])
        total_hombres = sum((e.get("qwen_json", {}).get("details", {}).get("count_hombres", 0) or 0) for e in events)
        if found == 0:
            return {"text": f"No se han registrado hombres hoy según el registro disponible."}
        return {"text": f"Se detectaron {total_hombres} hombre(s) en {found} evento(s) hoy.", "events": events[:5]}

    if any(p in msg_norm for p in ["polocher blanco", "polocher", "camiseta blanca", "camisa blanca", "ropa blanca", "polo blanco", "con gorra", "gorra", "con camisa", "con camiseta"]):
        from eva.tools import tool_search_events
        # Extraer clothing del mensaje
        cl_parts = []
        if "blanca" in msg_norm or "blanco" in msg_norm: cl_parts.append("blanco")
        if "negra" in msg_norm or "negro" in msg_norm: cl_parts.append("negro")
        if "verde" in msg_norm: cl_parts.append("verde")
        if "rojo" in msg_norm or "roja" in msg_norm: cl_parts.append("rojo")
        if "azul" in msg_norm: cl_parts.append("azul")
        if "gorra" in msg_norm: cl_parts.append("gorra")
        if "polo" in msg_norm or "polocher" in msg_norm: cl_parts.append("polo")
        if "camisa" in msg_norm: cl_parts.append("camisa")
        if "camiseta" in msg_norm: cl_parts.append("camiseta")
        clothing_str = " ".join(cl_parts) if cl_parts else "blanco"
        result = await tool_search_events(user_id, clothing=clothing_str, date="today", limit=20)
        found = result.get("found", 0)
        events = result.get("events", [])
        if found == 0:
            return {"text": f"No se han registrado personas con {clothing_str} hoy."}
        return {"text": f"Se detectaron {found} evento(s) con personas vistiendo {clothing_str} hoy.", "events": events[:5]}

    if any(p in msg_norm for p in ["cuantos empleados", "cuántos empleados", "empleados hoy"]):
        from eva.tools import tool_search_events
        result = await tool_search_events(user_id, query="empleado", date="today", limit=20)
        found = result.get("found", 0)
        events = result.get("events", [])
        if found == 0:
            return {"text": f"No se han registrado empleados específicos hoy."}
        return {"text": f"Se detectaron {found} evento(s) con empleados hoy.", "events": events[:5]}

    if any(p in msg_norm for p in ["hubo alertas", "hubo alguna alerta", "alguna alerta hoy", "alertas hoy", "cuantas alertas", "cuántas alertas"]):
        from eva.tools import tool_find_anomalies
        result = await tool_find_anomalies(user_id, min_severity="baja", date="today", limit=10)
        found = result.get("found", 0)
        events = result.get("anomalies", [])
        if found == 0:
            return {"text": f"No se han registrado alertas de actividad sospechosa hoy. Todo está tranquilo."}
        return {"text": f"Hubo {found} alerta(s) hoy. ¿Quieres ver los detalles?", "events": events[:5]}

    # Detectar FILTROS semánticos para enrutar a tool_search_events (P4)
    has_class_filter = any(w in msg_norm for w in ["hombre", "mujer", "niño", "niña", "nino", "anciano", "anciana"])
    has_clothing_filter = any(w in msg_norm for w in ["camisa", "camiseta", "pantalon", "pant", "short", "vestido", "blanca", "blanco", "negro", "negra", "verde", "rojo", "roja", "azul", "amarillo", "gris"])
    has_minp = any(p in msg_norm for p in ["mas de", "más de", "minimo", "mínimo"])
    has_accessory_filter = any(w in msg_norm for w in ["gorra", "gorrita", "gorro", "sombrero", "gafas", "lentes", "anteojos", "casco"])

    if any(p in msg_norm for p in ["ultimo evento", "ultimos eventos", "último evento", "últimos eventos", "ultimo análisis", "último análisis", "ultima actividad", "últimas actividades", "ultimo activity", "muestrame", "muéstrame", "busca", "enseñame", "ense\u00f1ame"]) and (has_class_filter or has_clothing_filter or has_minp or has_accessory_filter):
        # Filtros + listado → usar tool_search_events con extracción heurística
        from eva.tools import tool_search_events
        # Detectar fecha: ayer / hoy
        date_param = "yesterday" if any(w in msg_norm for w in ("ayer", "anoche", "dia anterior", "día anterior")) else "today"
        kwargs = {"user_id": user_id, "date": date_param, "limit": 10}
        # persona
        if "hombre" in msg_norm or "señor" in msg_norm or "caballero" in msg_norm:
            kwargs["person_class"] = "hombre"
        elif "mujer" in msg_norm or "señora" in msg_norm:
            kwargs["person_class"] = "mujer"
        elif any(w in msg_norm for w in ["niño", "niña", "nino"]):
            kwargs["person_class"] = "nino"
        elif any(w in msg_norm for w in ["anciano", "anciana"]):
            kwargs["person_class"] = "anciano"
        # ropa (color + prenda) — toma el primero que matchee
        for w in ["camisa", "camiseta", "pantalon", "vestido"]:
            if w in msg_norm: kwargs["clothing"] = w; break
        for color in ["blanca","blanco","negro","negra","verde","rojo","roja","azul","amarillo","gris"]:
            if color in msg_norm:
                kwargs["clothing"] = (kwargs.get("clothing","") + " " + color).strip()
                break
        # head accessory — gorra/sombrero/gafas PRIMERO (más discriminante que la ropa)
        if "gorra" in msg_norm or "gorro" in msg_norm or "gorrita" in msg_norm:
            kwargs["head_accessory"] = "gorra"
        elif "sombrero" in msg_norm:
            kwargs["head_accessory"] = "sombrero"
        elif "gafas" in msg_norm or "lentes" in msg_norm or "anteojos" in msg_norm:
            kwargs["head_accessory"] = "gafas"
        elif "casco" in msg_norm:
            kwargs["head_accessory"] = "casco"
        # Por defecto usar color del accesorio si lo hay (gorra negra)
        for color in ["negra", "negro", "blanca", "blanco", "roja", "rojo"]:
            if color in msg_norm:
                ha = kwargs.get("head_accessory", "")
                if ha and not any(c in ha for c in ["negra","blanca","roja","negro","blanco","rojo"]):
                    kwargs["head_accessory"] = ha + " " + color.rstrip("o").rstrip("a") + "a"  # naive plural
                break
        # rango personas
        import re as _re
        m = _re.search(r"mas de (\d+)|m[áa]s de (\d+)|min[íi]mo (\d+)", msg_norm)
        if m:
            num = int([g for g in m.groups() if g][0])
            kwargs["min_persons"] = num + 1
        # activity
        for act in ["trabajando", "hablando", "entrando", "comiendo", "sentado", "caminando", "leyendo"]:
            if act in msg_norm:
                kwargs["activity"] = act
                break
        result = await tool_search_events(**kwargs)
        found = result.get("found", 0)
        events = result.get("events", [])
        used_kwargs = {k: v for k, v in kwargs.items() if k not in ("user_id", "limit")}
        if found == 0:
            return {"text": f"No encontré eventos con esos filtros ({', '.join([f'{k}={v}' for k,v in used_kwargs.items()])}). Intenta quitar alguno.", "events": []}
        parts = [f"Encontré {found} evento(s) con {[k for k in used_kwargs]}:"]
        for item in events[:5]:
            parts.append(f"- {item.get('datetime', '')} · {item.get('camera_name', '')}: {item.get('description', '')[:100]}")
        return {"text": "\n".join(parts), "events": events[:5]}

    # ── Fase 2: tools basadas en zonas (hardcoded intents) ──
    # Hora pico / horas pico / cuando hay mas gente / cuando es mas tranquilo
    if any(p in msg_norm for p in ["hora pico", "horas pico", "cuando hay mas gente", "cuando es mas tranqui", "a que hora abunda", "a que hora hay mas", "cual es la hora con mas"]):
        from eva.tools import tool_peak_hours
        date_param = "yesterday" if any(w in msg_norm for w in ("ayer", "anoche", "dia anterior", "día anterior")) else "today"
        result = await tool_peak_hours(user_id, date=date_param, top_n=3)
        if result.get("success"):
            return {"text": result.get("message", ""), "events": []}
        return {"text": f"No pude calcular las horas pico: {result.get('error', '')}", "events": []}

    # Cuantos entraron / cuantos salieron / cuanta gente hay / ocupacion / visitantes unicos
    if any(p in msg_norm for p in ["cuantos entraron", "cuántos entraron", "cuantas personas entraron", "cuántas personas entraron",
                                    "cuantos salieron", "cuántos salieron", "flujo de gente", "cuanta gente hay",
                                    "cuánta gente hay", "ocupacion actual", "ocupacion del local", "visitantes unicos",
                                    "visitantes únicos", "personas unicas hoy"]):
        from eva.tools import tool_traffic_flow
        date_param = "yesterday" if any(w in msg_norm for w in ("ayer", "anoche")) else "today"
        result = await tool_traffic_flow(user_id, date=date_param)
        if result.get("success"):
            return {"text": result.get("message", ""), "events": []}
        return {"text": f"No pude calcular el flujo: {result.get('error', '')}", "events": []}

    # Donde se acumula la gente / heatmap / zonas mas transitadas / mapa de calor
    if any(p in msg_norm for p in ["donde se acumula", "dónde se acumula", "mapa de calor", "heatmap", "heat map",
                                    "zonas mas transitadas", "zonas más transitadas", "que zonas son mas", "que parte del local"]):
        from eva.tools import tool_heatmap_data
        date_param = "yesterday" if any(w in msg_norm for w in ("ayer", "anoche")) else "today"
        result = await tool_heatmap_data(user_id, date=date_param, grid_size=16)
        if result.get("success"):
            return {"text": result.get("message", ""), "events": []}
        return {"text": f"No pude generar el heatmap: {result.get('error', '')}", "events": []}

    # Cuanto tiempo en X / permanencia / quien estuvo mas de N minutos / dwell
    if any(p in msg_norm for p in ["cuanto tiempo en ", "cuánto tiempo en ", "cuanto tiempo estuvieron", "permanencia",
                                    "quien estuvo mas de", "quién estuvo más de", "mas de 30 minutos", "alguien sospechoso en"]):
        from eva.tools import tool_zone_dwell
        date_param = "yesterday" if any(w in msg_norm for w in ("ayer", "anoche")) else "today"
        anomaly = 30
        # Extraer número del mensaje si existe
        import re as _re2
        m = _re2.search(r"(mas|m[áa]s)\s+de\s+(\d+)", msg_norm)
        if m:
            try:
                anomaly = int(m.group(2))
            except Exception:
                pass
        # Extraer zona del mensaje si existe (ej "en caja", "en cocina")
        extracted_zone = None
        for zw in ["caja", "cocina", "entrada", "mostrador", "almacen", "almacén", "comedor",
                    "oficina", "bodega", "pasillo", "produccion", "producción", "restringida",
                    "parqueo", "estacionamiento", "sala", "inventario"]:
            if f"en {zw}" in msg_norm or f" en {zw}" in msg_norm:
                extracted_zone = zw.capitalize()
                break
        result = await tool_zone_dwell(user_id, date=date_param, anomaly_min_minutes=anomaly, zone_id=extracted_zone)
        if result.get("success"):
            return {"text": result.get("message", ""), "events": []}
        return {"text": f"No pude calcular permanencia: {result.get('error', '')}", "events": []}

    if any(p in msg_norm for p in ["ultimo evento", "último evento", "ultimo análisis", "último análisis", "ultima actividad", "última actividad"]):
        from eva.tools import tool_latest_events
        result = await tool_latest_events(user_id, limit=3, date="today")
        found = result.get("found", 0)
        events = result.get("events", [])
        if found == 0:
            return {"text": f"No hay análisis recientes registrados hoy."}
        parts = [f"Últimos análisis de hoy:"]
        for item in events[:3]:
            parts.append(f"- {item.get('datetime', '')} · {item.get('camera_name', '')}: {item.get('description', '')[:100]}")
        return {"text": "\n".join(parts), "events": events[:3]}

    return None


async def _handle_os_mode(session, user_id, message, session_id):
    return await _handle_os_mode_v2(session, user_id, message, session_id)


async def _handle_os_mode_v2(session, user_id, message, session_id):
    session["msgs"].append({"role":"user","content":message})
    first = session["owner_name"].split()[0] if session.get("owner_name") else "amigo"
    ud = _load_user_data(user_id)
    cam_count = len([c for c in ud.get("cameras",[]) if c.get("active")]) if ud.get("cameras") else 0

    if message == "__daily_summary__":
        return await _handle_daily_summary(session, user_id, message, session_id)

    suggestions = _get_business_suggestions_list(ud.get("business_type",""), cam_count, first)
    recent = await _get_recent_summary(user_id)

    intent_result = await _detect_intent_and_route(user_id, message, first, recent, cam_count, session)
    if intent_result:
        session["msgs"].append({"role":"assistant","content":intent_result["text"]})
        _sessions[session_id] = session
        return _mk_resp(session, intent_result["text"], suggestions=suggestions, events_found=intent_result.get("events", []))

    sys_p = (
        f"Eres Eva, asistente de seguridad de OjoIA en República Dominicana.\n"
        f"Dueño: {session.get('owner_name','el dueño')}\n"
        f"Negocio: {ud.get('business_name','')} ({ud.get('business_type','')})\n"
        f"Cámaras activas: {cam_count}\n\n"
        f"=== RESUMEN RECIENTE DEL DIARIO ===\n{recent}\n\n"
        f"=== HERRAMIENTAS DISPONIBLES ===\n"
        f"- get_activity_summary: Resume actividad diaria (total análisis, personas, alertas)\n" +
        f"- search_events: Busca eventos con filtros semánticos. Filtros opcionales: person_class (hombre|mujer|nino|anciano), clothing (ej 'camisa blanca'), min_persons, max_persons, activity (trabajando|hablando|entrando), importance (baja|media|alta|critica), date (today|yesterday|YYYY-MM-DD), camera_id, query\n" +
        f"- event_book: Indice cronologico agrupable. 'Que paso entre 2 y 4 pm?' Parametros: date, group_by (hour|camera|ten_minute), only_importance, camera_id\n" +
        f"- find_anomalies: Eventos relevantes segun gravedad (media/alta)\n" +
        f"- latest_events: Lista ultimos análisis cronologicos\n" +
        f"- count_people: Conteo de personas unicas hoy/ayer\n" +
        f"- traffic_flow: Flujo entrada/salida con zonas entrance. 'cuantos entraron hoy', 'cuanta gente hay ahora'\n" +
        f"- peak_hours: Top horas por trafico. 'cuales son las horas pico', 'cuando hay mas gente'\n" +
        f"- heatmap_data: Datos de densidad por celda. 'donde se acumula mas gente', 'mapa de calor'\n" +
        f"- zone_dwell: Permanencia por zona. 'cuanto tiempo en caja', 'quien estuvo mas de 30 min'\n" +
        f"- is_open_hours: Horario negocio abierto/cerrado\n" +
        f"- list_employees: Empleados registrados con face_id, rol y horario\n\n"
        f"Para usar una herramienta, responde SOLO con:\n<tool_call>\n{{\"name\": \"nombre_herramienta\", \"arguments\": {{\"param\": \"valor\"}}}}\n</tool_call>\n\n"
        f"Si no necesitas herramientas, responde directamente al usuario.\n\n"
        f"Responde en español, natural y dominicano. NO inventa datos."
    )
    msgs = [{"role":"system","content":sys_p}]
    for h in session.get("msgs",[])[-6:]:
        if isinstance(h,dict) and "role" in h:
            msgs.append({"role":h["role"],"content":h.get("content","")[:200]})
    msgs.append({"role":"user","content":message})

    response = await _call_qwen(msgs, max_tokens=500)
    content = response.get("content", "").strip()

    tool_call = _parse_hermes_tool_call(content)
    if tool_call:
        tool_name = tool_call.get("name", "")
        tool_args = tool_call.get("arguments", {})
        if tool_name in ("get_activity_summary", "search_events", "find_anomalies", "latest_events", "find_risks", "count_people", "is_open_hours", "list_employees", "identify_face", "event_book", "traffic_flow", "zone_dwell", "heatmap_data", "peak_hours"):
            result = await _execute_os_tool_v2(user_id, tool_name, tool_args, message, first, recent, cam_count, session)
            tool_result_msg = json.dumps(result, ensure_ascii=False)[:800]
            msgs.append({"role":"assistant","content":content})
            msgs.append({"role":"tool","tool_call_id":"hermes","content":tool_result_msg})
            biz = ud.get('business_name','')
            biz_type = session.get('business_type','')
            final_sys_p = (
                f"Eres Eva, asistente de seguridad de OjoIA en República Dominicana.\n"
                f"Dueño: {session.get('owner_name','el dueño')}\n"
                f"Negocio: {biz} ({biz_type})\n\n"
                f"=== RESULTADO DE HERRAMIENTA ===\n{tool_result_msg}\n\n"
                f"Responde al usuario de forma natural con estos datos. Sé específico y útil."
            )
            final_msgs = [{"role":"system","content":final_sys_p}]
            for h in session.get("msgs",[])[-4:]:
                if isinstance(h,dict) and "role" in h:
                    final_msgs.append({"role":h["role"],"content":h.get("content","")[:200]})
            final_msgs.append({"role":"user","content":message})
            final = await _call_qwen(final_msgs, max_tokens=400)
            text = final.get("content", "").strip() or result.get("text", f"No pude procesarlo, {first}.")
            session["msgs"].append({"role":"assistant","content":text})
            _sessions[session_id] = session
            return _mk_resp(session, text, suggestions=suggestions, events_found=result.get("events", []))

    if not content:
        text = f"No pude procesarlo, {first}."
    else:
        text = content
    session["msgs"].append({"role":"assistant","content":text})
    _sessions[session_id] = session
    return _mk_resp(session, text, suggestions=suggestions)

# =============================================================================
# SETUP MODE
# =============================================================================

async def _handle_setup(session, user_id, message, session_id, cam_id, storage_root, include_frame=False):
    phase = session["phase"]
    first = session["owner_name"].split()[0] if session.get("owner_name") else "amigo"

    # ── GREET ─────────────────────────────────────────────────────────────────
    if phase == SetupPhase.GREET.value:
        owner = session.get("owner_name","")
        biz = session.get("business_name","")
        btype = session.get("business_type","")
        msg = message.strip()

        if msg == "__greet__":
            extra = f" ({btype})" if btype else ""
            if owner and biz:
                session["phase"] = SetupPhase.ZONE.value
                examples = _get_zone_examples(btype)
                text = (f"¡Hola {owner.split()[0]}! 👋 Soy Eva.\n\n"
                        f"Vigilaré {biz}{extra}. Vamos a conectar tu primera cámara.\n\n"
                        f"¿Dónde la vas a poner? Por ejemplo: {examples}...")
                return _mk_resp(session, text)
            elif owner:
                session["phase"] = SetupPhase.ZONE.value
                return _mk_resp(session, f"¡Hola {owner.split()[0]}! 👋 Eva aquí.\n\n¿Cómo se llama tu negocio?")
            return _mk_resp(session, "¡Hola! 👋 Soy Eva.\n\n¿Cómo te llamas y cómo se llama tu negocio?")

        extracted = await _extract_business_data(user_id, msg, session)
        if extracted.get("owner_name"): session["owner_name"] = extracted["owner_name"]
        if extracted.get("business_name"): session["business_name"] = extracted["business_name"]
        if extracted.get("business_type"): session["business_type"] = extracted["business_type"]

        session["phase"] = SetupPhase.ZONE.value
        first = session["owner_name"].split()[0] if session.get("owner_name") else "amigo"
        _sessions[session_id] = session
        if session.get("business_name"):
            examples = _get_zone_examples(session.get("business_type",""))
            return _mk_resp(session, f"¡Genial, {first}! 👍 Vamos a conectar la cámara.\n\n¿Dónde la vas a poner? Por ejemplo: {examples}...")
        return _mk_resp(session, f"¡Genial, {first}! 👍 ¿Cómo se llama tu negocio?")

    # ── ZONE ─────────────────────────────────────────────────────────────────
    if phase == SetupPhase.ZONE.value:
        if not session.get("business_name"):
            session["business_name"] = message.strip()
            _sessions[session_id] = session
            examples = _get_zone_examples(session.get("business_type",""))
            return _mk_resp(session, f"Perfecto. 👍\n\n¿Dónde la vas a poner? Por ejemplo: {examples}...")

        zone_raw = message.strip()
        for p in ["en el ","en la ","en los ","en las ","el ","la ","los ","las "]:
            if zone_raw.lower().startswith(p):
                zone_raw = zone_raw[len(p):]; break
        session["zone"] = zone_raw.strip()
        session["phase"] = SetupPhase.HARDWARE.value
        session["wait_attempts"] = 0
        _sessions[session_id] = session
        return _mk_resp(session,
            f"Perfecto, {first}. La cámara va en **{session['zone']}**.\n\n"
            f"Vamos a conectarla:\n\n"
            f"1. 🔌 Conéctala a la corriente (LED azul parpadeando)\n"
            f"2. 📱 Ve a WiFi → busca 'OJO-XXXX' y conéctate\n"
            f"3. 🌐 Elige tu WiFi y ponle la clave\n"
            f"4. ✅ Cuando el LED esté fijo, escríbeme **listo**\n\n"
            f"Tómate tu tiempo, estoy aquí. 👍")

    # ── HARDWARE ─────────────────────────────────────────────────────────────
    if phase == SetupPhase.HARDWARE.value:
        session["msgs"].append({"role":"user","content":message})
        # Usar el modelo para entender la intención del usuario
        if await _is_intent_confirmed(message, "Usuario completando pasos de conexión", _qwen):
            session["phase"] = SetupPhase.WAIT_IMAGE.value
            _sessions[session_id] = session
            return await _handle_wait_image(session, session_id, user_id, first, message, storage_root, include_frame)
        return _mk_resp(session, "¿Dame un momento... Voy a revisar si el LED está fijo. Dime 'ok' cuando termines.")

    if phase == SetupPhase.WAIT_IMAGE.value:
        return await _handle_wait_image(session, session_id, user_id, first, message, storage_root, include_frame)

    if phase == SetupPhase.CONTEXT.value:
        return await _handle_context(session, session_id, user_id, message, first)

    if phase == SetupPhase.PROMPT_BUILD.value:
        return await _handle_prompt_build(session, session_id, user_id, message, first)

    if phase == SetupPhase.CONFIRM.value:
        return await _handle_confirm(session, session_id, user_id, message, first, storage_root)

    return _mk_resp(session, f"No entendí, {first}. ¿Puedes repetir?")

# =============================================================================
# WAIT_IMAGE
# =============================================================================

async def _handle_wait_image(session, session_id, user_id, first, message, storage_root, include_frame=False):
    if session.get("phase") == SetupPhase.WAIT_IMAGE.value:
        session["wait_attempts"] = session.get("wait_attempts", 0) + 1

    frame = None
    frame_camera_id = ""
    if session.get("camera_id"):
        frame = _get_frame(session.get("camera_id"), user_id)
        frame_camera_id = session.get("camera_id", "")
    elif include_frame:
        frame, frame_camera_id = _get_unconfigured_frame(user_id)

    if frame and not session.get("image_b64"):
        b64 = base64.b64encode(_resize(frame, 640)).decode()
        session["image_b64"] = b64
        session["camera_connected"] = True
        session["image_desc"] = await _describe_frame(b64, session.get("zone",""), session.get("business_type",""))
        session["image_analysis"] = await _analyze_frame_for_prompt(b64, session.get("zone",""), session.get("business_type","negocio"))
        session["phase"] = SetupPhase.ANALYZE.value
        session["wait_attempts"] = 0
        try:
            cam = session.get("camera_id") or (f"cam_{int(time.time())}" if (frame_camera_id or "").startswith("pending_") else frame_camera_id) or f"cam_{int(time.time())}"
            session["camera_id"] = cam
            fd = storage_root / "users" / user_id / "cameras" / cam / "frames"
            fd.mkdir(parents=True, exist_ok=True)
            (fd / "first.jpg").write_bytes(frame)
            (fd / "eva_frame.jpg").write_bytes(_resize(frame, 640))
        except Exception: pass

    if session["phase"] == SetupPhase.ANALYZE.value:
        return await _handle_analyze(session, session_id, first)

    attempts = session.get("wait_attempts", 0)
    if await _is_intent_confirmed(message, f"Esperando imagen. Usuario dice: {message}", _qwen) and not frame:
        session["phase"] = SetupPhase.CONTEXT.value
        session["manual_image_confirmed"] = True
        session["position_confirmed"] = True
        session["context_step"] = "concern"
        session["image_desc"] = "Usuario confirmó conexión sin imagen."
        zone = session.get("zone","esa zona")
        _sessions[session_id] = session
        _save_session_to_disk(session)
        return _mk_resp(session, _context_question(session, "concern", first))

    if attempts > 5:
        text = "Llevo rato esperando... 📷\n\n¿Ya está conectada? Escríbeme 'ok', 'listo' o dime que sí."
    elif attempts > 2:
        text = f"Esperando imagen... 📷 (intento {attempts})\n\n¿La cámara está conectada? Solo escribe 'ok' o 'listo' para confirmar."
    else:
        text = "Dame un momento... 📷"
    _sessions[session_id] = session
    return _mk_resp(session, text)

# =============================================================================
# ANALYZE
# =============================================================================

async def _handle_analyze(session, session_id, first):
    session["msgs"].append({"role":"user","content":""})
    session["image_sent"] = False
    img_desc = session.get("image_desc","")
    img_analysis = session.get("image_analysis",{})
    zone = session.get("zone","la zona")

    position_ok = img_analysis.get("es_zona_correcta", True)
    coincide = img_analysis.get("coincide_zona", True)
    zona_real = img_analysis.get("zona_real","")
    if not coincide or (zona_real and zona_real.lower() != zone.lower()):
        position_ok = False

    lines = ["📷 Cámara conectada ✅\n", f"Zona configurada: {zone}"]
    if zona_real: lines.append(f"Zona detectada: {zona_real}")
    lines.append(f"\nDescripción: {img_desc}")
    if position_ok:
        lines.append(f"\n✅ La posición se ve bien para vigilar **{zone}**.")
    else:
        lines.append(f"\n⚠️ La imagen no coincide con la zona **{zone}**.")
        if zona_real: lines.append(f"Parece enfocando **{zona_real}**.")
    lines.append("\n\n¿La dejamos en el mismo lugar, la movemos, o qué opinas?")

    session["phase"] = SetupPhase.CONTEXT.value
    session["position_confirmed"] = position_ok
    _sessions[session_id] = session
    _save_session_to_disk(session)
    resp = _mk_resp(session, "\n".join(lines), img_b64=session.get("image_b64",""))
    session["image_sent"] = True
    _save_session_to_disk(session)
    return resp

# =============================================================================
# CONTEXT — FLUJO DE CONTEXTO PARA PROMPT DE VIGILANCIA
# =============================================================================

async def _handle_context(session, session_id, user_id, message, first):
    session.setdefault("msgs", []).append({"role":"user","content":message})

    if _is_show_frame(message) and session.get("image_b64"):
        return _mk_resp(session, f"Imagen de la cámara:\n{session.get('image_desc','')}", img_b64=session.get("image_b64",""), force_image=True)

    if session.get("context_step") == "edit":
        if session.get("awaiting_schedule") or any(w in message.lower() for w in ["horario", "hora", "abre", "cierra"]):
            sched = _parse_schedule(message)
            if sched:
                session["schedule"] = sched
                session["awaiting_schedule"] = False
                sp = await _build_system_prompt(session)
                session["system_prompt"] = sp
                ud = _load_user_data(user_id)
                ud["schedule"] = sched
                if session.get("concern"): ud["main_concerns"] = [session.get("concern")]
                ud["vigilance_context"] = {
                    "zone": session.get("zone",""),
                    "concern": session.get("concern",""),
                    "attention_phrases": session.get("attention_phrases", []),
                    "owner_notes": session.get("owner_notes", []),
                }
                _save_user_data(user_id, ud)
                session["phase"] = SetupPhase.CONFIRM.value
                session["context_step"] = "confirm"
                _sessions[session_id] = session
                _save_session_to_disk(session)
                return _mk_resp(session, _config_summary_text(session, first), ready_to_confirm=True)
            session["awaiting_schedule"] = True
            _sessions[session_id] = session
            _save_session_to_disk(session)
            return _mk_resp(session, f"Dale, {first}. Dime el horario nuevo, por ejemplo: 08:00 a 22:00")
        _sessions[session_id] = session
        _save_session_to_disk(session)
        return _mk_resp(session, f"Claro. Por ahora puedo modificar el horario. Dime el horario nuevo, por ejemplo: 08:00 a 22:00")

    zone = session.get("zone","esa zona")
    msg = message.strip()

    if session.get("context_step") == "position":
        if _is_no(msg):
            _sessions[session_id] = session
            _save_session_to_disk(session)
            return _mk_resp(session, f"Entendido. Dime qué quieres cambiar:\n\n• La zona\n• La posición de la cámara\n• Volver a revisar la imagen")
        if _is_position_confirm(msg) or session.get("position_confirmed"):
            session["position_confirmed"] = True
            session["context_step"] = "concern"
            _sessions[session_id] = session
            _save_session_to_disk(session)
            return _mk_resp(session, _context_question(session, "concern", first))
        _sessions[session_id] = session
        _save_session_to_disk(session)
        return _mk_resp(session, f"No estoy segura si eso confirma la posición.\n\n¿La dejamos en el mismo lugar o quieres moverla?")

    step = session.get("context_step") or "concern"

    if step in _CONTEXT_STEPS:
        if _is_yes(msg) and not session.get(step):
            _sessions[session_id] = session
            _save_session_to_disk(session)
            return _mk_resp(session, f"Dímelo concreto, {first}.\n\n{_context_question(session, step, first)}")
        default = _context_default(step, zone)
        if step == "attention_phrases":
            answer = _parse_attention_phrases(msg)
            if not answer:
                answer = default
        else:
            answer = _clean_context_answer(msg, default)
        session[step] = answer
        next_step = _next_context_step(session)
        session["context_step"] = next_step
        _sessions[session_id] = session
        _save_session_to_disk(session)
        if next_step == "prompt_build":
            session["phase"] = SetupPhase.PROMPT_BUILD.value
            _sessions[session_id] = session
            _save_session_to_disk(session)
            return _mk_resp(session, f"Gracias {first}. Ya tengo todo el contexto. Voy a crear el sistema de observación para tu {zone}...")
        return _mk_resp(session, _context_question(session, next_step, first))

    session["phase"] = SetupPhase.PROMPT_BUILD.value
    _sessions[session_id] = session
    _save_session_to_disk(session)
    return _mk_resp(session, f"Gracias {first}. Ya tengo todo el contexto. Voy a crear el sistema de protección para tu {zone}...")

# =============================================================================
# PROMPT_BUILD
# =============================================================================

async def _handle_prompt_build(session, session_id, user_id, message, first):
    session["msgs"].append({"role":"user","content":message})
    ud = _load_user_data(user_id)
    ud["business_type"] = session.get("business_type", ud.get("business_type",""))
    ud["schedule"] = session.get("schedule", ud.get("schedule",{"open":"08:00","close":"22:00"}))
    if session.get("concern"): ud["main_concerns"] = [session["concern"]]
    ud["vigilance_context"] = {
        "zone": session.get("zone",""),
        "concern": session.get("concern",""),
        "attention_phrases": session.get("attention_phrases", []),
        "owner_notes": session.get("owner_notes", []),
    }
    _save_user_data(user_id, ud)
    sp = await _build_system_prompt(session)
    session["system_prompt"] = sp
    zone = session.get("zone","")
    biz = session.get("business_name","")
    concern = session.get("concern","")
    sched = session.get("schedule",{"open":"08:00","close":"22:00"})
    session["phase"] = SetupPhase.CONFIRM.value
    _sessions[session_id] = session
    _save_session_to_disk(session)
    return _mk_resp(session, _config_summary_text(session, first), ready_to_confirm=True)

# =============================================================================
# CONFIRM
# =============================================================================

async def _handle_confirm(session, session_id, user_id, message, first, storage_root):
    session["msgs"].append({"role":"user","content":message})
    if _is_edit_request(message):
        wants_schedule = any(w in message.lower() for w in ["horario", "hora", "abre", "cierra"])
        session["phase"] = SetupPhase.CONTEXT.value
        session["context_step"] = "edit"
        session["awaiting_schedule"] = wants_schedule
        _sessions[session_id] = session
        _save_session_to_disk(session)
        return _mk_resp(session, f"Claro, {first}. ¿Qué quieres modificar?\n\n• Horario\n• Zona\n• Preocupación\n\nPor ahora puedo cambiar el horario. Dime: horario 08:00 a 22:00")
    if _is_yes(message):
        cam = session.get("camera_id") or f"cam_{int(time.time())}"
        session["camera_id"] = cam
        cfg = {
            "camera_id": cam, "name": f"Cámara {session.get('zone','zona')}",
            "zone": session.get("zone",""),
            "business_type": session.get("business_type","negocio"),
            "business_name": session.get("business_name",""),
            "conversation_context": {
                "concern": session.get("concern",""),
            },
            "attention_phrases": session.get("attention_phrases", []),
            "owner_notes": session.get("owner_notes", []),
            "schedule": session.get("schedule",{"open":"08:00","close":"22:00"}),
            "yolo_triggers": ["person"],
            "grid_size": 12, "cooldown_min": 5, "active": True, "configured_at": int(time.time()),
        }
        from eva.camera_builder import save_camera_config
        save_camera_config(user_id, cfg, storage_root)
        session["phase"] = SetupPhase.DONE.value
        _sessions[session_id] = session
        _save_session_to_disk(session)
        return _mk_resp(session, f"🎉 ¡Listo {first}! Tu cámara está configurada y vigilando.\n\n¿Qué quieres saber?", camera_saved=True)
    session["phase"] = SetupPhase.CONTEXT.value
    _sessions[session_id] = session
    return _mk_resp(session, f"Entendido. ¿Qué quieres cambiar?\n\n• La zona\n• La preocupación\n• El horario\n• Empezar de nuevo")

# =============================================================================
# OS MODE
# =============================================================================

async def _handle_os_mode(session, user_id, message, session_id):
    return await _handle_os_mode_v2(session, user_id, message, session_id)


async def _handle_os_mode_v2(session, user_id, message, session_id):
    session["msgs"].append({"role":"user","content":message})
    first = session["owner_name"].split()[0] if session.get("owner_name") else "amigo"
    ud = _load_user_data(user_id)
    cam_count = len([c for c in ud.get("cameras",[]) if c.get("active")]) if ud.get("cameras") else 0

    if message == "__daily_summary__":
        suggestions = _get_business_suggestions_list(ud.get("business_type",""), cam_count, first)
        from eva.tools import tool_get_activity_summary
        result = await tool_get_activity_summary(user_id, "today")
        total = result.get("total_events", 0)
        attention_events = result.get("attention_events", 0)
        persons_total = result.get("persons_total", 0)
        last_summary = result.get("last_summary", "")
        details = result.get("details", {})
        last_yolo = result.get("last_yolo", {})
        counts_total = result.get("counts_total", {})
        notable_events = result.get("notable_events", [])
        if total == 0:
            text = "Hoy no se han registrado análisis todavía. La cámara está activa y Eva está atenta."
        else:
            lines = []
            lines.append(f"Resumen del día: Hoy se realizaron {total} análisis de seguridad.")
            lines.append("")
            if attention_events > 0:
                lines.append(f"🔍 {attention_events} evento(s) coincidieron con lo que me pediste vigilar.")
            else:
                lines.append("✅ No se detectaron coincidencias con lo que me pediste vigilar.")
            if persons_total > 0:
                lines.append(f"👥 Personas en la escena: hasta {persons_total} a la vez (según tracker).")
            if counts_total:
                platos = counts_total.get("platos", 0)
                fundas = counts_total.get("fundas", 0)
                if platos > 0:
                    lines.append(f"🍽️ Platos visibles: ~{platos}.")
                if fundas > 0:
                    lines.append(f"🛍️ Fundas: ~{fundas}.")
            lines.append(f"📊 Objetos detectados en último análisis: {last_yolo.get('count', 0)}.")
            classes = last_yolo.get("classes", [])
            if classes:
                lines.append(f"   Tipos: {', '.join(classes[:5])}.")
            if last_summary:
                lines.append(f"")
                lines.append(f"📝 Último análisis: {last_summary[:200]}")
            scene = details.get("scene_context", "")
            if scene:
                lines.append(f"")
                lines.append(f"🏪 Contexto: {scene}")
            text = "\n".join(lines)
        session["msgs"].append({"role":"assistant","content":text})
        _sessions[session_id] = session
        return _mk_resp(session, text, suggestions=suggestions, events_found=notable_events)

    suggestions = _get_business_suggestions_list(ud.get("business_type",""), cam_count, first)
    recent = await _get_recent_summary(user_id)

    # Hardcoded intents para herramientas críticas
    msg_lower = message.lower()
    if any(k in msg_lower for k in ("cuántas personas", "cuantas personas", "cuanta gente", "cuánta gente", "afluencia", "tráfico de personas", "han venido", "vinieron hoy", "personas vinieron")):
        best_cam = await _pick_best_camera_id(user_id) or ""
        tool_result = await _execute_os_tool_v2(user_id, "count_people", {"date": "today", "camera_id": best_cam}, message, first, recent, cam_count, session)
        session["msgs"].append({"role": "assistant", "content": tool_result.get("text", "")})
        _sessions[session_id] = session
        return _mk_resp(session, tool_result.get("text", ""), suggestions=suggestions, events_found=tool_result.get("events", []))
    
    if any(k in msg_lower for k in ("está abierto", "esta abierto", "estamos abiertos", "horario de apertura", "cerrado ahora", "abierto ahora")):
        tool_result = await _execute_os_tool_v2(user_id, "is_open_hours", {}, message, first, recent, cam_count, session)
        session["msgs"].append({"role": "assistant", "content": tool_result.get("text", "")})
        _sessions[session_id] = session
        return _mk_resp(session, tool_result.get("text", ""), suggestions=suggestions, events_found=tool_result.get("events", []))
    
    if any(k in msg_lower for k in ("reporte diario", "reporte del día", "envía el reporte", "mandar reporte", "ver reporte", "mostrar reporte", "daily report", "reporte diario automático", "auto reporte")):
        from reportes.daily_report import send_daily_report_to_chat
        best_cam = await _pick_best_camera_id(user_id) or ""
        report_result = await send_daily_report_to_chat(user_id, best_cam, "yesterday")
        if report_result.get("success"):
            report_text = report_result.get("message", "Reporte generado")
            session["msgs"].append({"role": "assistant", "content": report_text})
            _sessions[session_id] = session
            return _mk_resp(session, report_text, suggestions=suggestions, events_found=[])
        else:
            error_text = f"Error generando reporte: {report_result.get('error', 'Unknown')}"
            session["msgs"].append({"role": "assistant", "content": error_text})
            _sessions[session_id] = session
            return _mk_resp(session, error_text, suggestions=suggestions, events_found=[])

    intent_result = await _detect_intent_and_route(user_id, message, first, recent, cam_count, session)
    if intent_result:
        session["msgs"].append({"role":"assistant","content":intent_result["text"]})
        _sessions[session_id] = session
        return _mk_resp(session, intent_result["text"], suggestions=suggestions, events_found=intent_result.get("events", []))

    sys_p = (
        f"Eres Eva, asistente de seguridad de OjoIA en República Dominicana.\n"
        f"Dueño: {session.get('owner_name','el dueño')}\n"
        f"Negocio: {ud.get('business_name','')} ({ud.get('business_type','')})\n"
        f"Cámaras activas: {cam_count}\n\n"
        f"=== RESUMEN RECIENTE DEL DIARIO ===\n{recent}\n\n"
        f"=== HERRAMIENTAS DISPONIBLES ===\n"
        f"- get_activity_summary: Resume actividad diaria (total análisis, personas, alertas)\n" +
        f"- search_events: Busca eventos con filtros semánticos. Filtros opcionales: person_class (hombre|mujer|nino|anciano), clothing (ej 'camisa blanca'), min_persons, max_persons, activity (trabajando|hablando|entrando), importance (baja|media|alta|critica), date (today|yesterday|YYYY-MM-DD), camera_id, query\n" +
        f"- event_book: Indice cronologico agrupable. 'Que paso entre 2 y 4 pm?' Parametros: date, group_by (hour|camera|ten_minute), only_importance, camera_id\n" +
        f"- find_anomalies: Eventos relevantes segun gravedad (media/alta)\n" +
        f"- latest_events: Lista ultimos análisis cronologicos\n" +
        f"- count_people: Conteo de personas unicas hoy/ayer\n" +
        f"- traffic_flow: Flujo entrada/salida con zonas entrance. 'cuantos entraron hoy', 'cuanta gente hay ahora'\n" +
        f"- peak_hours: Top horas por trafico. 'cuales son las horas pico', 'cuando hay mas gente'\n" +
        f"- heatmap_data: Datos de densidad por celda. 'donde se acumula mas gente', 'mapa de calor'\n" +
        f"- zone_dwell: Permanencia por zona. 'cuanto tiempo en caja', 'quien estuvo mas de 30 min'\n" +
        f"- is_open_hours: Horario negocio abierto/cerrado\n" +
        f"- list_employees: Empleados registrados con face_id, rol y horario\n\n"
        f"Para usar una herramienta, responde SOLO con:\n<tool_call>\n{{\"name\": \"nombre_herramienta\", \"arguments\": {{\"param\": \"valor\"}}}}\n</tool_call>\n\n"
        f"Si no necesitas herramientas, responde directamente al usuario.\n\n"
        f"Responde en español, natural y dominicano. NO inventes datos."
    )
    msgs = [{"role":"system","content":sys_p}]
    for h in session.get("msgs",[])[-6:]:
        if isinstance(h,dict) and "role" in h:
            msgs.append({"role":h["role"],"content":h.get("content","")[:200]})
    msgs.append({"role":"user","content":message})

    response = await _call_qwen(msgs, max_tokens=500)
    content = response.get("content", "").strip()

    tool_call = _parse_hermes_tool_call(content)
    if tool_call:
        tool_name = tool_call.get("name", "")
        tool_args = tool_call.get("arguments", {})
        if tool_name in ("get_activity_summary", "search_events", "find_anomalies", "latest_events", "find_risks", "count_people", "is_open_hours", "list_employees", "identify_face", "event_book", "traffic_flow", "zone_dwell", "heatmap_data", "peak_hours"):
            result = await _execute_os_tool_v2(user_id, tool_name, tool_args, message, first, recent, cam_count, session)
            tool_result_msg = json.dumps(result, ensure_ascii=False)[:800]
            msgs.append({"role":"assistant","content":content})
            msgs.append({"role":"tool","tool_call_id":"hermes","content":tool_result_msg})
            biz = ud.get('business_name','')
            biz_type = session.get('business_type','')
            final_sys_p = (
                f"Eres Eva, asistente de seguridad de OjoIA en República Dominicana.\n"
                f"Dueño: {session.get('owner_name','el dueño')}\n"
                f"Negocio: {biz} ({biz_type})\n\n"
                f"=== RESULTADO DE HERRAMIENTA ===\n{tool_result_msg}\n\n"
                f"Responde al usuario de forma natural con estos datos. Sé específico y útil."
            )
            final_msgs = [{"role":"system","content":final_sys_p}]
            for h in session.get("msgs",[])[-4:]:
                if isinstance(h,dict) and "role" in h:
                    final_msgs.append({"role":h["role"],"content":h.get("content","")[:200]})
            final_msgs.append({"role":"user","content":message})
            final = await _call_qwen(final_msgs, max_tokens=400)
            text = final.get("content", "").strip() or result.get("text", f"No pude procesarlo, {first}.")
            session["msgs"].append({"role":"assistant","content":text})
            _sessions[session_id] = session
            return _mk_resp(session, text, suggestions=suggestions, events_found=result.get("events", []))

    if not content:
        text = f"No pude procesarlo, {first}."
    else:
        text = content
    session["msgs"].append({"role":"assistant","content":text})
    _sessions[session_id] = session
    return _mk_resp(session, text, suggestions=suggestions)



def _build_os_chat_messages(session, ud, cam_count, recent, fallback_instruction):
    sys_p = (
        f"Eres Eva, asistente de OjoIA. Encargada de proteger el negocio.\n"
        f"Dueño: {session.get('owner_name','el dueño')}\n"
        f"Negocio: {ud.get('business_name','')} ({ud.get('business_type','')})\n"
        f"Cámaras activas: {cam_count}\n\n"
        f"=== RESUMEN RECIENTE ===\n{recent}\n\n"
        f"Responde en español, natural y dominicana.\n"
        f"{fallback_instruction}\n"
        f"Máximo 4 líneas."
    )
    msgs = [{"role":"system","content":sys_p}]
    for h in session.get("msgs",[])[-8:]:
        if isinstance(h,dict) and "role" in h:
            msgs.append({"role":h["role"],"content":h.get("content","")[:200]})
    return msgs


async def _route_os_message(user_id, session, message, recent, cam_count, ud=None):
     if ud is None:
         ud = _load_user_data(user_id)
     tools = _OS_TOOL_DEFINITIONS
     sys_p = (
         "Eres el router de Eva. Decide la acción exacta que debe tomar Eva en modo operativo.\n"
         "Responde SOLO JSON válido con esta forma:\n"
         "{\n"
         "  \"tool\": \"nombre_de_la_herramienta\",\n"
         "  \"params\": {...},\n"
         "  \"reason\": \"explicacion muy breve\"\n"
         "}\n"
         "Herramientas disponibles:\n"
     )
     sys_p += "\n".join(
         f"- {name}: {meta['description']}. Parametros: {json.dumps(meta['parameters'], ensure_ascii=False)}"
         for name, meta in tools.items()
     )
     sys_p += (
         "\nReglas:\n"
         "- Si el usuario pregunta qué es Eva, quién eres o se queja de que Eva sigue instalando/configurando, usa tool='none' y explica en reason.\n"
         "- Si pide ajustar, activar, desactivar o cambiar protección, usa update_vigilance_config o get_vigilance_config.\n"
         "- Si pregunta por alertas, sospechas, anomalías, riesgos, incendio, humo, actividad actual o última actividad, elige la herramienta de diario que corresponda.\n"
         "- Si pregunta por personas, conteo, cuántas personas vinieron, afluencia o tráfico de clientes, usa count_people.\n"
         "- Si pregunta por empleados o quién está en cámara, usa identify_face/list_employees según corresponda.\n"
         "- Si pregunta por horario de apertura, si está abierto o cerrado, usa is_open_hours.\n"
         "- Si no hay intención clara o es conversación general, usa tool='none'.\n"
         "- params debe contener solo los campos necesarios y valores concretos.\n"
         "Contexto del negocio:\n"
         f"Dueño: {session.get('owner_name','el dueño')}\n"
         f"Negocio: {ud.get('business_name','')} ({ud.get('business_type','')})\n"
         f"Cámaras activas: {cam_count}\n"
         f"Resumen reciente:\n{recent}\n"
         "Mensaje del usuario:\n"
         f"{message}"
     )
     msgs = [{"role":"system","content":sys_p}, {"role":"user","content":message}]
     result = await _call_qwen(msgs, 180)
     parsed = _parse_json_response(result.get("content", ""))
     tool = str(parsed.get("tool") or "").strip()
     if tool not in tools and tool != "none":
         return {"tool": "none", "reason": "router inválido", "params": {}}
     return {"tool": tool, "params": parsed.get("params") or {}, "reason": parsed.get("reason", "")}


async def _execute_os_tool_v2(user_id, tool_name, params, message, first, recent, cam_count, session):
    """Ejecuta herramientas del nuevo schema (function-calling)."""
    from eva.tools import TOOLS_REGISTRY

    if tool_name == "respond_directly":
        return {"text": params.get("message", ""), "events": []}

    if tool_name == "save_event":
        data = await _tool_call("save_event", user_id, params)
        if data.get("success"):
            return {"text": f"Listo, {first}. Guardé: {params.get('summary', 'evento')}", "events": []}
        return {"text": f"No pude guardar: {data.get('error', 'error')}", "events": []}

    if tool_name == "get_vigilance_config":
        params = {**params}
        if not params.get("camera_id"):
            params["camera_id"] = _extract_camera_id_from_message(message) or await _pick_best_camera_id(user_id)
        data = await _tool_call("get_vigilance_config", user_id, params)
        if not data.get("success"):
            return {"text": f"No pude cargar la configuración: {data.get('error', 'error')}", "events": []}
        v = data.get("vigilance", {})
        normal = v.get("normal_mode", {}) if isinstance(v.get("normal_mode"), dict) else {}
        sentinel = v.get("sentinel_mode", {}) if isinstance(v.get("sentinel_mode"), dict) else {}
        text = (
            f"{first}, configuración de observación de {data.get('camera_id') or 'la cámara'}:\n"
            f"Modo: {'centinela' if data.get('mode') == 'sentinel' else 'estándar'}.\n"
            f"Frases de atención ({len(data.get('attention_phrases', []))}):\n"
        )
        for p in data.get("attention_phrases", [])[:5]:
            text += f"  • {p}\n"
        if data.get("owner_notes"):
            text += f"Notas del dueño:\n"
            for n in data.get("owner_notes", [])[:3]:
                text += f"  • {n}\n"
        return {"text": text, "events": []}

    if tool_name == "update_vigilance_config":
        params = {**params}
        if not params.get("camera_id"):
            params["camera_id"] = _extract_camera_id_from_message(message) or await _pick_best_camera_id(user_id)
        inferred = _build_vigilance_update_from_message(user_id, params.get("camera_id", ""), message)
        if inferred.get("needs_clarification"):
            return {"text": inferred.get("text", "Necesito más datos."), "events": []}
        vigilance = params.get("vigilance") or inferred.get("vigilance") or {}
        schedule = params.get("schedule") or inferred.get("schedule")
        mode = params.get("mode") or inferred.get("mode")
        if not vigilance and mode:
            if mode == "sentinel":
                vigilance = {"sentinel_mode": {"enabled": True}, "enabled": True}
            else:
                vigilance = {"normal_mode": {"enabled": True}, "enabled": True}
        if "sensitivity" in params:
            vigilance.setdefault("normal_mode", {})["sensitivity"] = params["sensitivity"]
        if "attention_phrases" in params:
            vigilance["attention_phrases"] = params["attention_phrases"]
        if "owner_notes" in params:
            vigilance["owner_notes"] = params["owner_notes"]
        data = await _tool_call("update_vigilance_config", user_id, {
            "camera_id": params.get("camera_id", ""),
            "vigilance": vigilance or None,
            "schedule": schedule,
            "mode": mode,
        })
        if not data.get("success"):
            return {"text": f"No pude actualizar: {data.get('error', 'error')}", "events": []}
        v = data.get("vigilance", {})
        normal = v.get("normal_mode", {}) if isinstance(v.get("normal_mode"), dict) else {}
        sentinel = v.get("sentinel_mode", {}) if isinstance(v.get("sentinel_mode"), dict) else {}
        mode_text = 'modo centinela' if data.get('mode') == 'sentinel' else 'modo estándar'
        return {"text": f"{first}, protección actualizada: {mode_text}. Sensibilidad: {normal.get('sensitivity', '—')}. Centinela: {'activo' if sentinel.get('enabled', False) else 'inactivo'}.", "events": []}

    if tool_name == "get_latest_frame":
        params = {**params}
        if not params.get("camera_id"):
            params["camera_id"] = _extract_camera_id_from_message(message) or await _pick_best_camera_id(user_id)
        data = await _tool_call("get_latest_frame", user_id, params)
        if data.get("has_frame"):
            return {"text": f"Tengo el último frame de {data.get('camera_id', 'la cámara')}.", "events": []}
        return {"text": "No hay imagen reciente disponible.", "events": []}

    if tool_name == "analyze_frame":
        params = {**params}
        if not params.get("camera_id"):
            params["camera_id"] = _extract_camera_id_from_message(message) or await _pick_best_camera_id(user_id)
        data = await _tool_call("analyze_frame", user_id, {
            "camera_id": params.get("camera_id", ""),
            "prompt": params.get("prompt", "Describe qué está pasando en la escena."),
        })
        if data.get("success"):
            return {"text": data.get("analysis", "Analicé el frame."), "events": []}
        return {"text": f"No pude analizar: {data.get('error', 'error')}", "events": []}

    if tool_name == "search_events":
        params.setdefault("date", "today")
        params.setdefault("limit", 5)
        data = await _tool_call("search_events", user_id, params)
        if not data.get("success", True):
            return {"text": f"No pude buscar: {data.get('error', 'error')}", "events": []}
        if not data.get("found"):
            return {"text": f"No encontré nada en el diario con: {params.get('query', '')}.", "events": []}
        parts = [f"Encontré {data['found']} resultado(s):"]
        for item in data.get("events", [])[:5]:
            parts.append(f"- {item.get('datetime', '')} · {item.get('camera_name', '')}: {item.get('description', '')[:140]}")
        return {"text": "\n".join(parts), "events": data.get("events", [])}

    if tool_name == "get_activity_summary":
        params.setdefault("date", "today")
        data = await _tool_call("get_activity_summary", user_id, params)
        if not data.get("success", True):
            return {"text": f"No pude consultar: {data.get('error', 'error')}", "events": []}
        summary = data.get("summary", "")
        if summary == "Sin eventos registrados.":
            summary = "No hay actividad registrada. La cámara sigue enviando imagen; Eva está atenta."
        return {"text": summary, "events": []}

    if tool_name == "find_anomalies":
        params.setdefault("min_severity", "baja")
        params.setdefault("date", "today")
        params.setdefault("limit", 5)
        data = await _tool_call("find_anomalies", user_id, params)
        if not data.get("success", True):
            return {"text": f"No pude consultar alertas: {data.get('error', 'error')}", "events": []}
        if not data.get("found"):
            return {"text": "No hay actividad sospechosa registrada.", "events": []}
        anomalies = data.get("anomalies", [])
        if "cantidad" in _normalize_text(message) or "cuántas" in _normalize_text(message) or "cuantas" in _normalize_text(message) or "hubo" in _normalize_text(message):
            last = anomalies[0]
            return {"text": f"Hubo {data.get('found')} alerta(s). Última: {last.get('datetime', '')} en {last.get('camera_name', '')}: {last.get('descripcion', '')}.", "events": anomalies[:5]}
        parts = [f"Encontré {data.get('found')} alerta(s):"]
        for item in anomalies[:3]:
            parts.append(f"- {item.get('datetime', '')} · {item.get('camera_name', '')}: {item.get('descripcion', '')}.")
        return {"text": "\n".join(parts), "events": anomalies[:5]}

    if tool_name == "latest_events":
        params.setdefault("limit", 5)
        params.setdefault("date", "today")
        data = await _tool_call("latest_events", user_id, params)
        if not data.get("success", True):
            return {"text": f"No pude consultar: {data.get('error', 'error')}", "events": []}
        if not data.get("found"):
            return {"text": "No hay análisis recientes.", "events": []}
        parts = [f"Últimos {data['found']} análisis:"]
        for item in data.get("events", [])[:5]:
            parts.append(f"- {item.get('datetime', '')} · {item.get('camera_name', '')}: {item.get('description', '')[:120]}")
        return {"text": "\n".join(parts), "events": data.get("events", [])}

    if tool_name == "find_risks":
        params.setdefault("date", "today")
        params.setdefault("limit", 5)
        data = await _tool_call("find_risks", user_id, params)
        if not data.get("success", True):
            return {"text": f"No pude consultar riesgos: {data.get('error', 'error')}", "events": []}
        if not data.get("found"):
            return {"text": "No encontré riesgo de incendio, humo o fuego.", "events": []}
        parts = [f"Encontré {data['found']} posible(s) riesgo(s):"]
        for item in data.get("risks", [])[:3]:
            parts.append(f"- {item.get('datetime', '')} · {item.get('camera_name', '')}: {item.get('description', '')[:120]}")
        return {"text": "\n".join(parts), "events": data.get("risks", [])}


    if tool_name == "count_people":
        params = {**params}
        if not params.get("camera_id"):
            params["camera_id"] = await _pick_best_camera_id(user_id) or ""
        if not params.get("date"):
            params["date"] = "today"
        data = await _tool_call("count_people", user_id, params)
        if not data.get("success"):
            return {"text": f"No pude contar personas: {data.get('error', 'error')}", "events": []}
        total = data.get("total_people", 0)
        sessions = data.get("sessions", 0)
        peak = data.get("peak_count", 0)
        peak_time = data.get("peak_time", "")
        cameras = ", ".join(data.get("cameras", [])) or "cámara principal"
        text = f"Detecté **{total} persona(s)** hoy en {cameras}."
        if sessions > 1:
            text += f" Fueron {sessions} visitas distintas."
        if peak > 0 and peak_time:
            text += f" El pico fue de {peak} persona(s) a las {peak_time}."
        return {"text": text, "events": []}

    if tool_name == "is_open_hours":
        data = await _tool_call("is_open_hours", user_id, params)
        if not data.get("success"):
            return {"text": f"No pude consultar horario: {data.get('error', 'error')}", "events": []}
        status = "abierto" if data.get("is_open") else "cerrado"
        hours = data.get("business_hours", "08:00–18:00")
        return {"text": f"El negocio está **{status}**. Horario de hoy: {hours}.", "events": []}

    if tool_name == "identify_face":
        params = {**params}
        if not params.get("camera_id"):
            params["camera_id"] = _extract_camera_id_from_message(message) or await _pick_best_camera_id(user_id)
        data = await _tool_call("identify_face", user_id, params)
        if not data.get("success"):
            return {"text": f"No pude identificar: {data.get('error', 'error')}", "events": []}
        return {"text": data.get("message", "No pude identificar a la persona."), "events": []}

    if tool_name == "list_employees":
        data = await _tool_call("list_employees", user_id, {})
        return {"text": data.get("message", "No hay empleados registrados."), "events": []}

    if tool_name == "event_book":
        data = await _tool_call("event_book", user_id, params)
        total = data.get("total_events", 0)
        groups = data.get("groups", [])
        period = data.get("period", "")
        if not data.get("success"):
            return {"text": f"No pude consultar el libro de eventos: {data.get('error', 'error')}", "events": []}
        if total == 0:
            return {"text": f"No hay eventos registrados para {period}.", "events": []}
        parts = [f"📖 {total} evento(s) en {period}. {len(groups)} grupo(s) por {data.get('group_by','hora')}:"]
        for g in groups[:8]:
            imp = g.get('importancia_max', 'baja')
            parts.append(f"  • {g['label']}: {g['events_count']} eventos (imp: {imp})")
        return {"text": "\n".join(parts), "events": []}

    # ── Fase 2: tools basadas en zonas ──
    if tool_name == "traffic_flow":
        data = await _tool_call("traffic_flow", user_id, params)
        if not data.get("success"):
            return {"text": f"No pude calcular el flujo: {data.get('error', 'error')}", "events": []}
        return {"text": data.get("message", "Sin datos de trafico."), "events": []}

    if tool_name == "zone_dwell":
        data = await _tool_call("zone_dwell", user_id, params)
        if not data.get("success"):
            return {"text": f"No pude calcular permanencia: {data.get('error', 'error')}", "events": []}
        return {"text": data.get("message", "Sin datos de permanencia."), "events": []}

    if tool_name == "heatmap_data":
        data = await _tool_call("heatmap_data", user_id, params)
        if not data.get("success"):
            return {"text": f"No pude generar el heatmap: {data.get('error', 'error')}", "events": []}
        return {"text": data.get("message", "Sin datos de heatmap."), "events": []}

    if tool_name == "peak_hours":
        data = await _tool_call("peak_hours", user_id, params)
        if not data.get("success"):
            return {"text": f"No pude calcular horas pico: {data.get('error', 'error')}", "events": []}
        return {"text": data.get("message", "Sin datos de horas pico."), "events": []}

    return {"text": f"Herramienta no reconocida: {tool_name}", "events": []}


def _sanitize_os_tool_params(params):
    allowed_dates = {"today", "yesterday", "recent"}
    date = str(params.get("date") or "today")
    if date not in allowed_dates and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        params["date"] = "today"

    if "limit" in params:
        try:
            params["limit"] = max(1, min(int(params["limit"]), MAX_OS_TOOL_LIMIT))
        except Exception:
            params["limit"] = 5
    if "camera_id" in params:
        params["camera_id"] = str(params.get("camera_id") or "")[:120]
    if "query" in params:
        params["query"] = str(params.get("query") or "")[:240]
    if "prompt" in params:
        params["prompt"] = str(params.get("prompt") or "Describe qué está pasando en la escena.")[:500]
    if "min_severity" in params and params.get("min_severity") not in ("baja", "media", "alta", "critica"):
        params["min_severity"] = "baja"
    if "mode" in params and params.get("mode") not in ("normal", "sentinel"):
        params.pop("mode", None)
    params.pop("system_prompt", None)
    if "schedule" in params:
        schedule = params.get("schedule") or {}
        if isinstance(schedule, dict):
            params["schedule"] = {k: str(v)[:5] for k, v in schedule.items() if k in ("open", "close")}
        else:
            params.pop("schedule", None)
    return params


def _normalize_os_tool_params(tool_name, params, message):
    params = {k: v for k, v in (params or {}).items() if v not in (None, "")}
    m = _normalize_text(message)
    if tool_name in ("get_activity_summary", "find_anomalies", "latest_events", "search_events", "find_risks", "count_people", "event_book", "traffic_flow", "zone_dwell", "heatmap_data", "peak_hours"):
        if not params.get("date"):
            if any(w in m for w in ("ayer", "dia anterior", "día anterior", "anoche")):
                params["date"] = "yesterday"
            elif any(w in m for w in ("últimas 24", "ultimas 24", "reciente", "recientes", "ahora", "actual")):
                params["date"] = "recent"
            else:
                params["date"] = "today"
    if tool_name == "find_anomalies":
        if not params.get("min_severity"):
            if any(w in m for w in ("critica", "crítica", "máxima", "maxima")):
                params["min_severity"] = "critica"
            elif any(w in m for w in ("alta importancia", "importancia alta")):
                params["min_severity"] = "alta"
            elif "media importancia" in m:
                params["min_severity"] = "media"
            else:
                params["min_severity"] = "baja"
        params.setdefault("limit", 5)
    if tool_name in ("latest_events", "search_events", "find_risks"):
        params.setdefault("limit", 5)
    return params


async def _tool_call(name, user_id, params):
    from eva.tools import TOOLS_REGISTRY
    fn = TOOLS_REGISTRY.get(name, {}).get("function")
    if not fn:
        return {"success": False, "error": f"Herramienta no disponible: {name}"}
    try:
        return await fn(user_id, **{k: v for k, v in params.items() if v not in (None, "")})
    except TypeError as e:
        return {"success": False, "error": f"Parametros invalidos para {name}: {e}"}
    except Exception as e:
        logger.error(f"Error herramienta {name}: {e}")
        return {"success": False, "error": str(e)}


_OS_TOOL_DEFINITIONS = {
    "get_vigilance_config": {
        "description": "Lee la configuración actual de protección de una cámara: modo, sensibilidad, horarios, alertar si y no alertar por.",
        "parameters": {"camera_id": "string"},
    },
    "update_vigilance_config": {
        "description": "Actualiza protección: activar/desactivar centinela, sensibilidad, horario, alertar si o no alertar por.",
        "parameters": {"camera_id": "string", "vigilance": "object", "schedule": "object", "mode": "normal|sentinel"},
    },
    "get_latest_frame": {
        "description": "Obtiene el último frame disponible de una cámara.",
        "parameters": {"camera_id": "string"},
    },
    "analyze_frame": {
        "description": "Analiza el último frame con Eva usando un prompt concreto.",
        "parameters": {"camera_id": "string", "prompt": "string"},
    },
    "search_events": {
        "description": "Busca en el diario de eventos por texto, fecha o cámara. Usa query='persona' si el usuario pregunta por alguien o algo específico.",
        "parameters": {"query": "string", "date": "today|yesterday|YYYY-MM-DD|recent", "camera_id": "string", "limit": "integer"},
    },
    "get_activity_summary": {
        "description": "Resume actividad del día: eventos, personas, cámaras y análisis.",
        "parameters": {"date": "today|yesterday|YYYY-MM-DD|recent", "camera_id": "string"},
    },
    "find_anomalies": {
        "description": "Busca alertas, anomalías, actividad sospechosa o violaciones por severidad.",
        "parameters": {"min_severity": "baja|media|alta|critica", "date": "today|yesterday|YYYY-MM-DD|recent", "camera_id": "string", "limit": "integer"},
    },
    "latest_events": {
        "description": "Lista los últimos análisis guardados en el diario.",
        "parameters": {"limit": "integer", "date": "today|yesterday|YYYY-MM-DD|recent", "camera_id": "string"},
    },
    "find_risks": {
        "description": "Busca riesgos de incendio, humo o fuego.",
        "parameters": {"date": "today|yesterday|YYYY-MM-DD|recent", "camera_id": "string", "limit": "integer"},
    },
    "identify_face": {
        "description": "Identifica quién aparece en el frame actual usando face recognition.",
        "parameters": {"camera_id": "string"},
    },
    "count_people": {
        "description": "Cuenta personas únicas detectadas por cámara. '¿Cuántas personas han venido hoy?'",
        "parameters": {"camera_id": "string", "date": "today|yesterday"},
    },
    "event_book": {
        "description": "Indice cronologico agrupable. 'Que paso entre 2 y 4 pm?' Parametros: date, group_by (hour|camera|ten_minute), only_importance, camera_id",
        "parameters": {"date": "string", "group_by": "string", "only_importance": "string", "camera_id": "string", "max_entries": "integer"},
    },
    "is_open_hours": {
        "description": "Consulta si el negocio está abierto según horario registrado.",
        "parameters": {"timestamp": "number"},
    },
    "list_employees": {
        "description": "Lista empleados registrados con faceid.",
        "parameters": {},
    },
}

def _get_business_suggestions_list(biz_type, cam_count, first):
    base = [f"👥 {first}, ¿cuántas personas hay?","🚨 ¿Viste algo sospechoso?","📊 Últimos análisis del diario","📋 Resumen del día","⚙️ Ajustar protección"]
    bt = (biz_type or "").lower().strip()
    if bt in ("restaurant","restaurante","bar","comedor"):
        base.append("🔥 ¿Hay riesgo de incendio o humo?")
    elif bt in ("retail","colmado","tienda","supermercado"):
        base.append("🛍️ ¿Falta mercadería o hay hurto?")
    elif bt in ("finca","agricultura","granja","campo"):
        base.append("🐄 ¿Hay animales o intrusos fuera de lugar?")
    return base

def _event_desc_for_summary(e: dict) -> str:
    qj = e.get("qwen_json", {}) if isinstance(e.get("qwen_json"), dict) else {}
    desc = (e.get("description") or qj.get("summary") or qj.get("description") or e.get("summary") or "sin descripción")
    parsed = _parse_json_response(str(desc))
    if parsed.get("summary"):
        desc = parsed["summary"]
    elif parsed.get("description"):
        desc = parsed["description"]
    return str(desc).strip()[:100]


def _event_ts_from_file(path: Path) -> int:
    try:
        return int(json.loads(path.read_text()).get("timestamp", 0) or 0)
    except Exception:
        return 0


async def _get_recent_summary(user_id):
    try:
        events = []
        base = STORAGE_ROOT / "users" / user_id / "cameras"
        if base.exists():
            for cam_dir in base.iterdir():
                if not cam_dir.is_dir():
                    continue
                events_dir = cam_dir / "events"
                if not events_dir.exists():
                    continue
                for jf in sorted(events_dir.glob("*.json"), key=lambda p: _event_ts_from_file(p), reverse=True)[:20]:
                    try:
                        events.append(json.loads(jf.read_text()))
                    except Exception:
                        pass
        if not events:
            edir = STORAGE_ROOT / "users" / user_id / "events"
            if edir.exists():
                for jf in sorted(edir.glob("*.json"), key=lambda p: _event_ts_from_file(p), reverse=True)[:20]:
                    try:
                        events.append(json.loads(jf.read_text()))
                    except Exception:
                        pass
        if not events:
            return "No hay actividad sospechosa reciente. La cámara sigue enviando imagen; reviso el diario apenas haya un análisis nuevo."
        lines = []
        for e in events[:5]:
            dt = e.get("datetime", e.get("timestamp",""))
            if isinstance(dt,(int,float)):
                from datetime import datetime as dtm
                dt = dtm.fromtimestamp(dt).strftime("%H:%M")
            lines.append(f"  [{dt}] {_event_desc_for_summary(e)}")
        return "\n".join(lines)
    except Exception as e:
        logger.error(f"Error resumen: {e}")
        return "No pude consultar el diario."

# =============================================================================
# EXTRACTOR
# =============================================================================

async def _extract_business_data(user_id, message, session):
    biz = session.get("business_name","")
    btype = session.get("business_type","")
    sys_p = ("Eres extractor de datos. Extrae datos del negocio del mensaje.\n"
             f"Datos conocidos: negocio={biz}, tipo={btype}\n"
             "Busca: owner_name, business_name, business_type, zone, concern.\n"
             "Responde SOLO JSON válido.")
    msgs = [{"role":"system","content":sys_p},{"role":"user","content":message}]
    result = await _call_qwen(msgs, 100)
    return _parse_json_response(result.get("content", ""))

# =============================================================================
# RESP BUILDER
# =============================================================================

def _mk_resp(session, text, img_b64="", ready_to_confirm=False, camera_saved=False, suggestions=None, force_image=False, events_found=None):
    img_url = ""
    if img_b64 and (force_image or not session.get("image_sent")):
        try:
            small = _resize(base64.b64decode(img_b64), 640)
            uid = session.get("user_id","")
            cam = session.get("camera_id", f"cam_{int(time.time())}")
            if uid:
                img_dir = STORAGE_ROOT / "users" / uid / "cameras" / cam / "frames"
                img_dir.mkdir(parents=True, exist_ok=True)
                (img_dir / "eva_frame.jpg").write_bytes(small)
                img_url = f"/eva-frame/{uid}/{cam}"
            else:
                tmp = Path("/tmp") / f"eva_img_{int(time.time())}.jpg"
                tmp.write_bytes(small)
                img_url = f"/eva-image/{tmp.name}"
        except Exception: img_url = ""
    return {
        "success": True, "response": text, "image_url": img_url,
        "session_id": session["session_id"], "phase": session["phase"],
        "zone": session.get("zone",""), "has_image": bool(session.get("image_b64")),
        "ready_to_confirm": ready_to_confirm, "camera_saved": camera_saved,
        "suggestions": suggestions or [], "events_found": events_found or [],
    }
