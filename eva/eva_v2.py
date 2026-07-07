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

async def _call_qwen(messages: list, max_tokens: int = 300, tools: list = None) -> dict:
    try:
        payload = {"model": "qwen", "messages": messages, "max_tokens": max_tokens}
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
_HW_DONE = {"ya","listo","conectad","encendid","terminé","termine",
            "hecho","prendido","wifi","internet","siguiente","luz",
            "led","azul","parpade","ok","bien"}
_CONNECTED = {"conectada","conectado","conectad","prendida","prendido",
              "prendio","prendió","funcionando","funciona","lista",
              "luz fija","led fijo","ya esta","ya está","ya prendio"}

def _is_yes(m): m=m.lower().strip(); return m in _YES or any(m.startswith(w) for w in _YES)
def _is_no(m): m=m.lower().strip(); return m in _NO or any(m.startswith(w) for w in _NO)
def _is_hw_done(m): return any(w in m.lower() for w in _HW_DONE)
def _is_connected(m): return any(w in m.lower() for w in _CONNECTED)

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
            f"Si no hay nada más, dime 'no' o 'listo'."
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


def _get_business_suggestions_list(biz_type, cam_count, first):
    base = [f"👥 {first}, ¿cuántas personas hay?","🚨 ¿Viste algo sospechoso?","📊 Últimos análisis del diario","📋 Resumen del día"]
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


async def _get_recent_summary(user_id):
    try:
        edir = STORAGE_ROOT / "users" / user_id / "events"
        if not edir.exists():
            edir = STORAGE_ROOT / "users" / user_id / "cameras"
        if not edir.exists():
            return "Sin eventos registrados."
        events = []
        for jf in sorted(edir.rglob("*.json"), reverse=True)[:20]:
            if jf.name == "eva_session_v2.json": continue
            try: events.append(json.loads(jf.read_text()))
            except Exception: pass
        if not events: return "Sin eventos registrados hoy."
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

    if any(p in msg_norm for p in ["cuantas mujeres", "cuántas mujeres", "mujeres hoy", "mujeres detectadas"]):
        from eva.tools import tool_search_events
        result = await tool_search_events(user_id, query="mujer", date="today", limit=20)
        found = result.get("found", 0)
        events = result.get("events", [])
        if found == 0:
            return {"text": f"No se han registrado mujeres específicas hoy según el registro disponible."}
        return {"text": f"Se detectaron {found} evento(s) con mujeres hoy.", "events": events[:5]}

    if any(p in msg_norm for p in ["cuantos hombres", "cuántos hombres", "hombres hoy"]):
        from eva.tools import tool_search_events
        result = await tool_search_events(user_id, query="hombre", date="today", limit=20)
        found = result.get("found", 0)
        events = result.get("events", [])
        if found == 0:
            return {"text": f"No se han registrado hombres específicos hoy según el registro disponible."}
        return {"text": f"Se detectaron {found} evento(s) con hombres hoy.", "events": events[:5]}

    if any(p in msg_norm for p in ["polocher blanco", "polocher", "camiseta blanca", "camisa blanca", "ropa blanca", "polo blanco"]):
        from eva.tools import tool_search_events
        result = await tool_search_events(user_id, query="blanco", date="today", limit=20)
        found = result.get("found", 0)
        events = result.get("events", [])
        if found == 0:
            return {"text": f"No se han registrado personas con ropa blanca hoy según el registro disponible."}
        return {"text": f"Se detectaron {found} persona(s) con ropa blanca hoy.", "events": events[:5]}

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



async def _handle_morning_greeting(session, user_id, message, session_id):
    """Saludo matutino con resumen del día anterior."""
    from eva.daily_summary import load_summary, generate_daily_summary
    from datetime import date, timedelta
    first = session.get("owner_name", "amigo").split()[0] if session.get("owner_name") else "amigo"
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    summary = load_summary(user_id, yesterday)
    if not summary or not summary.get("totals", {}).get("events"):
        summary = await generate_daily_summary(user_id, yesterday)
    
    lines = []
    lines.append(f"¡Buenos días, {first}! 👋")
    lines.append("")
    
    if summary and summary.get("totals", {}).get("events", 0) > 0:
        t = summary.get("totals", {})
        p = summary.get("people", {})
        items = summary.get("items", {})
        time_info = summary.get("time", {})
        comp = summary.get("comparison", {})
        
        lines.append(f"📊 Resumen de ayer ({yesterday}):")
        lines.append(f"• {t.get('events', 0)} análisis de seguridad")
        
        if t.get("alerts", 0) > 0:
            lines.append(f"• ⚠️ {t.get('alerts', 0)} alertas")
        else:
            lines.append("• Sin alertas ✅")
        
        if p.get("clientes_estimado", 0) > 0:
            lines.append(f"• 🧑‍🤝‍🧑 ~{p.get('clientes_estimado', 0)} clientes")
        if p.get("empleados", 0) > 0:
            lines.append(f"• 👤 ~{p.get('empleados', 0)} empleados")
        if items.get("platos", 0) > 0:
            lines.append(f"• 🍽️ ~{items.get('platos', 0)} platos")
        if items.get("bebidas", 0) > 0:
            lines.append(f"• 🥤 ~{items.get('bebidas', 0)} bebidas")
        
        peak = time_info.get("peak_hour")
        if peak is not None:
            lines.append(f"• Hora pico: {peak}:00")
        
        if comp.get("delta_events") is not None:
            delta = comp["delta_events"]
            if delta > 0:
                lines.append(f"• ↑ {delta} más que antier")
            elif delta < 0:
                lines.append(f"• ↓ {abs(delta)} menos que antier")
        
        lines.append("")
        lines.append("¿Qué quieres revisar hoy?")
    else:
        lines.append("Ayer no hubo actividad registrada. Todo tranquilo. ✅")
        lines.append("")
        lines.append("¿En qué te ayudo hoy?")
    
    text = "\n".join(lines)
    session["msgs"].append({"role": "assistant", "content": text})
    _sessions[session_id] = session
    return _mk_resp(session, text, suggestions=_get_business_suggestions_list(session.get("business_type", ""), session.get("cameras_count", 0), first))

async def _handle_os_mode(session, user_id, message, session_id):
    return await _handle_os_mode_v2(session, user_id, message, session_id)


async def _handle_os_mode_v2(session, user_id, message, session_id):
    session["msgs"].append({"role":"user","content":message})
    first = session["owner_name"].split()[0] if session.get("owner_name") else "amigo"
    ud = _load_user_data(user_id)
    cam_count = len([c for c in ud.get("cameras",[]) if c.get("active")]) if ud.get("cameras") else 0

    if message == "__daily_summary__":
        return await _handle_daily_summary(session, user_id, message, session_id)

    if message == "__morning_greeting__":
        return await _handle_morning_greeting(session, user_id, message, session_id)

    if "resumen del dia" in message.lower() or "resumen del día" in message.lower():
        return await _handle_daily_summary(session, user_id, message, session_id)

    _event_keywords = ["que ha pasado", "qué ha pasado", "que paso", "qué paso", "resumen", "eventos", "alertas", "personas", "actividad", "hoy", "detecciones", "análisis", "analisis", "fuera de horario", "centinela", "vigilancia", "qué pasó", "que pasó", "hubo", "ocurrio", "ocurrió"]
    _msg_lower = message.lower()
    _needs_tools = any(kw in _msg_lower for kw in _event_keywords)

    if _needs_tools:
        from eva.tools import tool_get_activity_summary, tool_search_events
        try:
            summary = await tool_get_activity_summary(user_id, "today")
            search = await tool_search_events(user_id, query="", date="today")
            recent_events = search.get("events", [])[:5]
            notable = summary.get("notable_events", [])
            total = summary.get("total_events", 0)
            attention = summary.get("attention_events", 0)
            persons = summary.get("persons_total", 0)
            last_summary = summary.get("last_summary", "")
            scene = summary.get("details", {}).get("scene_context", "")
            lines = []
            first_name = first
            lines.append(f"¡Hola, {first_name}!")
            if total > 0:
                lines.append(f"Resumen del día: Hoy se realizaron {total} análisis de seguridad.")
                if attention > 0:
                    lines.append(f"🚨 Alertas detectadas: {attention}.")
                if persons > 0:
                    lines.append(f"👥 Personas en la escena: hasta {persons} a la vez (según tracker).")
                if summary.get("counts_total", {}).get("empleados", 0) > 0:
                    lines.append(f"👤 Empleados detectados: {summary['counts_total']['empleados']}.")
                if summary.get("counts_total", {}).get("clientes_estimado", 0) > 0:
                    lines.append(f"🧑‍🤝‍🧑 Clientes estimados: {summary['counts_total']['clientes_estimado']}.")
                if last_summary:
                    lines.append(f"")
                    lines.append(f"📝 Último análisis: {last_summary[:200]}")
                if scene:
                    lines.append(f"")
                    lines.append(f"🏪 Contexto: {scene}")
                if recent_events:
                    lines.append(f"")
                    lines.append(f"📸 Eventos recientes:")
                    for evt in recent_events[:3]:
                        lines.append(f"  • {evt.get('datetime', '?')[:16]} — {evt.get('camera_name', '?')}: {evt.get('description', '')[:80]}")
            else:
                lines.append("Hoy no se han registrado análisis todavía. La cámara está activa y Eva está atenta.")
            session["msgs"].append({"role":"user","content":message})
            session["msgs"].append({"role":"assistant","content":"\n".join(lines)})
            _sessions[session_id] = session
            return _mk_resp(session, "\n".join(lines), suggestions=_get_business_suggestions_list(ud.get("business_type",""), cam_count, first), events_found=recent_events + notable)
        except Exception as e:
            logger.error(f"Error en detección de intención: {e}")

    suggestions = _get_business_suggestions_list(ud.get("business_type",""), cam_count, first)
    recent = await _get_recent_summary(user_id)

    intent_result = await _detect_intent_and_route(user_id, message, first, recent, cam_count, session)
    if intent_result:
        session["msgs"].append({"role":"assistant","content":intent_result["text"]})
        _sessions[session_id] = session
        return _mk_resp(session, intent_result["text"], suggestions=suggestions, events_found=intent_result.get("events", []))

    sys_p = (
        f"Eres Eva, asistente de seguridad de OjoIA en República Dominicana.\n"
        f"Dueño: {session.get('owner_name','el dueño')} — SIEMPRE saluda por su primer nombre.\n"
        f"Negocio: {ud.get('business_name','')} ({ud.get('business_type','')})\n"
        f"Cámaras activas: {cam_count}\n\n"
        f"=== RESUMEN RECIENTE DEL DIARIO ===\n{recent}\n\n"
        f"=== HERRAMIENTAS DISPONIBLES ===\n"
        f"- get_activity_summary: Resume la actividad del día (total análisis, alertas, personas)\n"
        f"- search_events: Busca eventos por palabra clave (personas, clientes, empleados, etc.)\n"
        f"- find_anomalies: Busca alertas o actividad sospechosa\n"
        f"- latest_events: Lista los últimos análisis\n\n"
        f"Para usar una herramienta, responde SOLO con:\n<tool_call>\n{{\"name\": \"nombre_herramienta\", \"arguments\": {{\"param\": \"valor\"}}}}\n</tool_call>\n\n"
        f"Si no necesitas herramientas, responde directamente al usuario.\n\n"
        f"IMPORTANTE: Siempre saluda usando el primer nombre del dueño. Ej: '¡Hola, Samuel!' no '¡Hola!'\n"
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
        if tool_name in ("get_activity_summary", "search_events", "find_anomalies", "latest_events", "find_risks"):
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

async def _extract_business_data(user_id: str, message: str, session: dict) -> dict:
    """Extrae owner_name, business_name y business_type del mensaje del usuario."""
    import re
    result = {"owner_name": None, "business_name": None, "business_type": None}
    msg = message.strip()

    # Heurística 1: "Mi nombre es X" / "Me llamo X" / "Soy X"
    name_patterns = [
        r"(?:mi nombre es|me llamo|soy|yo soy)\s+([A-Z\xc1\xc9\xcd\xd3\xda\xd1][a-z\xe1\xe9\xed\xf3\xfa\xf1]+(?:\s+[A-Z\xc1\xc9\xcd\xd3\xda\xd1][a-z\xe1\xe9\xed\xf3\xfa\xf1]+)?)",
    ]
    for pat in name_patterns:
        m = re.search(pat, msg, re.IGNORECASE)
        if m:
            result["owner_name"] = m.group(1).strip().title()
            break

    # Heurística 2: "Mi negocio es X" / "Tengo un X"
    biz_patterns = [
        r"(?:mi negocio es|mi empresa es|se llama)\s+[\x27\x22]?([^\x27\x22\n,]+)[\x27\x22]?",
        r"tengo\s+(?:un|una)\s+([a-z\xe1\xe9\xed\xf3\xfa\xf1]+(?:\s+[a-z\xe1\xe9\xed\xf3\xfa\xf1]+){0,3})",
    ]
    for pat in biz_patterns:
        m = re.search(pat, msg, re.IGNORECASE)
        if m:
            candidate = m.group(1).strip()
            skip_words = {"que", "para", "por", "con", "sin", "muy", "bien", "mal"}
            if candidate.lower() not in skip_words and len(candidate) > 2:
                result["business_name"] = candidate.title()
                break

    # Heurística 3: Detectar tipo de negocio desde el nombre
    if result["business_name"]:
        biz_lower = result["business_name"].lower()
        type_keywords = {
            "restaurante": ["restaurante", "restaurant", "comida", "cocina"],
            "bar": ["bar", "cantina", "pub", "cerveza"],
            "tienda": ["tienda", "store", "shop", "venta", "comercio"],
            "farmacia": ["farmacia", "pharmacy", "medicina"],
            "supermercado": ["supermercado", "super", "mercado", "grocery"],
            "oficina": ["oficina", "office", "despacho"],
            "almacen": ["almacen", "bodega", "warehouse"],
            "panaderia": ["panaderia", "bakery", "pan"],
            "cafeteria": ["cafeteria", "cafe"],
            "peluqueria": ["peluqueria", "barberia", "salon"],
        }
        for biz_type, keywords in type_keywords.items():
            if any(kw in biz_lower for kw in keywords):
                result["business_type"] = biz_type
                break

    # Si no se encontró nada con heurísticas y el mensaje es largo, usar LLM
    if not result["owner_name"] and not result["business_name"] and len(msg) > 10:
        try:
            from eva.eva_v2 import _call_qwen
            extraction_prompt = (
                "Del siguiente mensaje, extrae SOLO estos datos si existen:\n"
                "- owner_name: nombre de la persona\n"
                "- business_name: nombre del negocio\n"
                "- business_type: tipo de negocio\n\n"
                f"Mensaje: {msg!r}\n\n"
                "Responde SOLO en JSON: {\"owner_name\": \"...\", \"business_name\": \"...\", \"business_type\": \"...\"}"
            )
            llm_result = await _call_qwen(
                [{"role": "system", "content": "Extrae datos del mensaje. Responde solo JSON."},
                 {"role": "user", "content": extraction_prompt}],
                max_tokens=150
            )
            if llm_result.get("success"):
                content = llm_result.get("content", "{}")
                json_match = re.search(r"\{[^}]+\}", content)
                if json_match:
                    extracted = json.loads(json_match.group())
                    result["owner_name"] = extracted.get("owner_name")
                    result["business_name"] = extracted.get("business_name")
                    result["business_type"] = extracted.get("business_type")
        except Exception:
            pass

    return result




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
        if _is_hw_done(message) or _is_yes(message):
            session["phase"] = SetupPhase.WAIT_IMAGE.value
            _sessions[session_id] = session
            return await _handle_wait_image(session, session_id, user_id, first, message, storage_root, include_frame)
        return _mk_resp(session, "¿Necesitas ayuda?\n\n• Conecta a la corriente\n• Espera LED azul\n• Ve a WiFi 'OJO-XXXX'\n\nEscríbeme **listo** cuando el LED esté fijo. ✅")

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
    if _is_connected(message) and not frame:
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
        text = "Llevo rato esperando... 📷\n\nRevisa:\n• ¿LED fijo?\n• ¿WiFi 'OJO-XXXX'?\n\nSi ya está conectada, dime **la cámara está conectada**."
    elif attempts > 2:
        text = f"Esperando imagen... 📷 (intento {attempts})\n\nAsegúrate de que el LED esté fijo. Escríbeme **listo**."
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

