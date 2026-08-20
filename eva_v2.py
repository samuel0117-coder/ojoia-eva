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
    # 1) Buffer en RAM (alimentado por ingest_frame_for_eva desde /ingest/frame).
    # Es la vía preferida: el frame llega incluso si la cámara physical no está
    # asociada al user_id del wizard (puede caer en users/default mientras otra
    # cuenta levanta la cámara).
    for cid, frame in _latest_frame.items():
        if cid in ids:
            continue
        ts = _latest_frame_time.get(cid, 0.0)
        if now - ts < 120 and ts > best[2]:
            best = (frame, cid, ts)
    # 2) FS search EXPANDIDO a TODOS los user-dirs (no solo el del user_id del
    # wizard). La cámara física nueva puede estar postando a users/default
    # (porque resolve_user_id la asoció a "default" al no tener user_id explícito)
    # mientras el wizard lo levanta para otro user. Antes, el FS search estaba
    # restringido a STORAGE_ROOT/users/<user_id>/cameras/ y por eso el wizard
    # nunca encontraba el frame.
    try:
        users_root = STORAGE_ROOT / "users"
        if users_root.exists():
            for latest in users_root.glob("*/cameras/*/frames/latest_raw.jpg"):
                try:
                    cid = latest.parent.parent.name
                except Exception:
                    continue
                if cid in ids:
                    continue
                try:
                    mtime = latest.stat().st_mtime
                except Exception:
                    continue
                if now - mtime < 300 and mtime > best[2]:
                    try:
                        best = (latest.read_bytes(), cid, mtime)
                    except Exception:
                        pass
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
    # Si user.json no lista cámaras (o lista vacía), verificar el FS
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

# ── Ranking de avance de fase del wizard ─────────────────────────────────────
# Sirve para que _pending_session_for_user ELIJA la sesión MÁS AVANZADA (no la
# más nueva). Antes solo comparaba por created_at, lo que permitía a una sesión
# nueva en fase "zone" pisar una existente en "context" (wizard reiniciado).
_PHASE_RANK = {
    "greet": 1, "zone": 2, "hardware": 3, "wait_image": 4,
    "analyze": 5, "context": 6, "prompt_build": 7, "confirm": 8,
}

def _phase_rank(phase: str) -> int:
    return _PHASE_RANK.get((phase or "").lower(), 0)


def _pending_session_for_user(user_id: str) -> Optional[Dict]:
    """Devuelve la sesión de SETUP pendiente más avanzada para el usuario.
    
    UN SOLO CHAT: ahora todas las sesiones usan el mismo sid "chat_<uid>", así
    que normalmente solo hay UNA entrada en _sessions. Pero por compatibilidad
    con datos legacy/sessiones guardadas en disco, este helper elige la sesión
    MÁS AVANZADA en el wizard (no la más nueva), para no regresar el flujo.
    """
    best = None
    best_score = (0, 0.0)  # (phase_rank, created_at)
    for s in _sessions.values():
        if s.get("user_id") != user_id:
            continue
        if s.get("phase") in {"done", "os"}:
            continue
        if s.get("phase") not in ("hardware", "wait_image", "analyze", "context", "prompt_build", "confirm"):
            continue
        score = (_phase_rank(s.get("phase")), float(s.get("created_at", 0) or 0))
        if score > best_score:
            best_score = score
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
            score = (_phase_rank(d.get("phase")), float(d.get("created_at", 0) or sf.stat().st_mtime or 0))
            if score > best_score:
                best_score = score
                best = d
    if best:
        best.setdefault("msgs", [])
        # Solo indexar si no hay ya una sesión MÁS AVANZADA para el MISMO sid en
        # memoria (para no pisar una sesión en progreso con una del disco menos
        # avanzada). Si el sid del best es distinto a _sessions en memoria, OK.
        best_sid = best.get("session_id") or f"pending_{user_id}"
        existing = _sessions.get(best_sid)
        if existing and existing.get("user_id") == user_id and _phase_rank(existing.get("phase")) > _phase_rank(best.get("phase")):
            # La sesión en memoria está más avanzada: devolver esa en vez del disco.
            return existing
        _sessions[best_sid] = best
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

async def _call_qwen(messages: list, max_tokens: int = 600, tools: list = None, temperature: float = 0.3, tool_choice = None) -> dict:
    try:
        payload = {
            "model": "qwen",
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if tools:
            payload["tools"] = tools
            # [Fix D] En fallback retry con tool_choice="required" forzamos
            # al modelo a emitir un tool_call en vez de narrar la respuesta.
            if tool_choice:
                payload["tool_choice"] = tool_choice
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
                  "Eres un asistente de instalación de cámaras. Describe:\n"
                  "1. Qué zona ves realmente\n"
                  "2. Objetos, personas, animales visibles\n"
                  "3. ¿Coincide con la zona indicada?\n"
                  "4. Iluminación (buena/regular/mala) — ¿hay contraluz?\n"
                  "5. ¿Se ve bien lo que se quiere vigilar en esta zona?\n"
                  "6. ¿Recomendarías ajustar la posición, el ángulo o la iluminación?\n"
                  "Responde en español, 5-7 líneas, específico y práctico.")
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
                  '"contraluz":true/false,"orientacion":"correcta/torcida/invertida",'
                  '"visibilidad_objetivo":"buena/regular/mala",'
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

async def _is_intent_confirmed(message: str, context: str, model_func=None) -> bool:
    """Usa Qwen para entender si el usuario confirmó la intención.

    model_func es opcional: si es None o falla, usamos _call_qwen (que siempre
    existe en este módulo). Esto quita la dependencia de un nombre `_qwen` que
    nunca se definió y causaba NameError en HARDWARE/WAIT_IMAGE.
    """
    try:
        prompt = (
            f"Analiza si el usuario confirmó una acción. Responde SOLO con: 'si' (confirmado), 'no' (no confirmado), 'maybe' (incógnita).\n\n"
            f"Contexto de la conversación: {context}\n"
            f"Mensaje del usuario: '{message}'\n\n"
            f"¿El usuario confirmó la acción del sistema? Responde con 'si', 'no' o 'maybe'."
        )
        msgs = [{"role": "user", "content": prompt}]
        response = ""
        if model_func is not None:
            raw = await model_func(msgs)
            # Soporta ambas formas: function que devuelve str o dict con 'content'.
            if isinstance(raw, dict):
                response = str(raw.get("content", ""))
            else:
                response = str(raw or "")
        else:
            r = await _call_qwen(msgs, max_tokens=10, temperature=0.0)
            response = str(r.get("content", ""))
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
    Las palabras cortas ('no', 'ok', 'si', 'sí', 'listo') NO son frases de
    atención válidas y se ignoran (el usuario dijo 'no' para pasar al paso
    siguiente, no para agregar una frase de atención literal).
    """
    if not text:
        return []
    text = text.replace(";", ",").replace("\n", ",")
    # Palabras cortas de confirmación/negación — no son frases de vigilancia.
    _CANCEL_WORDS = {"no","sí","si","listo","ok","dale","vale","hecho","ya","na","nada","ninguna","ninguno"}
    parts = re.split(r",\s*|\s+y\s+|\s+que\s+", text)
    phrases = []
    for p in parts:
        p = p.strip().strip(".").strip()
        if len(p) < 3 or p.lower() in _CANCEL_WORDS:
            # El usuario no está agregando una frase de atención real
            continue
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


def _detect_phrase_zone(user_id, camera_id, phrase):
    """Detecta si una frase de atencion menciona una zona configurada por el dueño.
    Devuelve el nombre de la zona (str) o None si no hay match.
    Usa camera_zones.get_camera_zones y empareja por nombre/tipo keywords en la frase.
    """
    if not user_id or not camera_id or not phrase:
        return None
    try:
        import camera_zones
        zones = camera_zones.get_camera_zones(user_id, camera_id) or []
    except Exception:
        return None
    if not zones:
        return None
    pl = _normalize_text(phrase)
    for z in zones:
        zname = (z.get("name") or "").strip()
        ztype = (z.get("type") or "").strip()
        if zname and _normalize_text(zname) in pl:
            return zname
    # Emparejar por tipo keyword (cajero->cashier, cocina->kitchen...)
    type_keywords = {
        "cashier": ["caja", "cajero", "cobro", "registradora", "punto de venta"],
        "entrance": ["entrada", "puerta", "acceso", "porton"],
        "kitchen": ["cocina", "estufa", "fogon"],
        "dining": ["comedor", "mesa", "cliente"],
        "inventory": ["inventario", "almacen", "bodega", "estante"],
        "counter": ["mostrador", "barra", "mostrador"],
        "restricted": ["restringid", "prohibid"],
        "office": ["oficina"],
        "storage": ["bodega", "deposito"],
        "hallway": ["pasillo"],
        "production": ["produccion", "fabrica"],
        "parking": ["parqueo", "estacionamiento"],
        "hall": ["sala", "hall", "recepcion"],
    }
    for z in zones:
        ztype = _normalize_text(z.get("type") or "")
        kws = type_keywords.get(ztype, [])
        if any(kw in pl for kw in kws):
            return (z.get("name") or "").strip() or ztype
    return None


def _build_vigilance_update_from_message(user_id, camera_id, message):
    from eva.tools import _load_camera_config, normalize_camera_vigilance_config
    m = _normalize_text(message)
    vigilance = {}
    schedule = None
    mode = None
    result_note = ""

    # ── Modo centinela / estándar (no toca frases) ──
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

    current_cfg = normalize_camera_vigilance_config(_load_camera_config(user_id, camera_id)) if camera_id else {}
    current_phrases = list((current_cfg.get("attention_phrases") or []))
    current_notes = list((current_cfg.get("owner_notes") or []))
    # attention_phrases_zones: dict {frase_text: zone_name}. Persistido en vigilance.
    current_zones_map = (current_cfg.get("vigilance", {}) or {}).get("attention_phrases_zones", {}) or {}
    if not isinstance(current_zones_map, dict):
        current_zones_map = {}

    # ── QUITAR una frase de atención ──
    # Enfoque tolerante (no determinista): reconocemos VERBOS de intención
    # de quitar en cualquier forma, y extraemos el resto del mensaje como el
    # texto objetivo. Si no coincide con ninguna frase, Eva PREGUNTA al usuario
    # enumerando las frases activas para que confirme cuál quitar, en vez de
    # fallar en silencio.
    import re as _re
    remove_verbs = (r"\b(?:quita|quitar|elimina|eliminar|borra|borrar|saca|sacar|suprime|suprimir)\b",
                    r"\b(?:deja\s+de|dejar\s+de)\s+vigilar",
                    r"\b(?:no\s+alertes|no\s+alertar|no\s+me\s+alertes|no\s+me\s+alertar)\b",
                    r"\b(?:quita|quitar|elimina|eliminar|borra|borrar)\b.*\b(?:regla|frase|vigilancia)\b")
    is_remove_intent = any(_re.search(p, m) for p in remove_verbs)

    # No treat "no alertes por X" como remoción directa: puede ser "es normal".
    is_normal_flag = any(w in m for w in ("es normal", "no es falta", "no me hace falta"))
    is_no_alert_explicit = any(pt in m for pt in ("no alertes por", "no alertar por", "no alertes de", "no alertar de", "no me alertes", "no me alertar"))

    remove_value = None
    remove_index = None  # selección numérica ("la 3", "quita la 2", "número 1")
    if is_remove_intent and current_phrases:
        # Si el usuario respondió a la pregunta de Eva con un número, usarlo.
        # Patrones: "la 3", "el 2", "número 1", "num 1", "quita la 2", "la frase 3".
        num_match = _re.search(r"\b(?:n[uú]mero|num)\s*(\d{1,2})\b", m) or \
                    _re.search(r"\b(?:la|el|los|las)?\s*(\d{1,2})\b", m)
        if num_match:
            idx = int(num_match.group(1))
            if 1 <= idx <= len(current_phrases):
                remove_index = idx - 1  # 0-based

    if remove_index is not None:
        r = current_phrases[remove_index]
        current_phrases = [p for p in current_phrases if p.lower() != r.lower()]
        current_zones_map.pop(r, None)
        vigilance["attention_phrases"] = current_phrases[-20:]
        if not current_zones_map:
            vigilance.pop("attention_phrases_zones", None)
        else:
            vigilance["attention_phrases_zones"] = current_zones_map
        result_note = f"Listo, quité la frase “{r}”. Te quedan {len(current_phrases)} frase(s) activa(s)."
    elif is_remove_intent or (is_no_alert_explicit and not is_normal_flag):
        # Extraer el texto objetivo: quitamos el segmento que contiene el verbo
        # de intención y la palabra puente (regla/frase/de/vigilar/por/de).
        cleaned = m
        for pattern in remove_verbs + (r"\b(?:regla|frase|vigilancia|para|por|de|la|el|los|las|que)\b",):
            cleaned = _re.sub(pattern, " ", cleaned)
        cleaned = _re.sub(r"\s+", " ", cleaned).strip(" .,-;:¿?¡!")
        # El resto SI contiene palabras significativas es el objetivo.
        if len(cleaned.split()) >= 1:
            remove_value = _normalize_text(message).strip()  # fallback: usar mensaje limpio
            # Mejor: tomar words que sobran tras quitar verbo+puente del original.
            # Reconstruir extraído sobre mensaje original manteniendo acentos.
            orig = message
            for pat in remove_verbs:
                orig = _re.sub(pat, " ", orig, flags=_re.IGNORECASE)
            orig = _re.sub(r"\b(?:la|el|los|las|regla|frase|vigilancia|de\s+la\s+regla|de\s+la\s+frase|regla\s+de|frase\s+de|de|por|para|que|vigilar)\b", " ", orig, flags=_re.IGNORECASE)
            orig = _re.sub(r"\s+", " ", orig).strip(" .,-;:¿?¡!,")
            # Conservar palabras con acento del mensaje original (no normalizado).
            target_words = [w for w in orig.split() if len(w) >= 3]
            if target_words:
                remove_value = " ".join(target_words)
            else:
                remove_value = cleaned if cleaned else None
            remove_value = remove_value[:180] if remove_value else None

    # Matching fuzzy bidireccional entre remove_value y cada frase activa.
    def _phrase_matches(phrase, target):
        if not target:
            return False
        p = _normalize_text(phrase).lower()
        t = _normalize_text(target).lower()
        if p == t:
            return True
        # Coincidencia por palabras significativas (≥4 chars): ≥60% de las
        # palabras del target están en la frase, o viceversa.
        pw = [w for w in p.split() if len(w) >= 4]
        tw = [w for w in t.split() if len(w) >= 4]
        if not tw:
            return p in t or t in p
        in_phrase = sum(1 for w in tw if w in p)
        if in_phrase / len(tw) >= 0.6:
            return True
        in_target = sum(1 for w in pw if w in t)
        if in_target / len(pw) >= 0.6:
            return True
        return False

    if remove_value and current_phrases:
        value = remove_value
        removed = [p for p in current_phrases if _phrase_matches(p, value)]
        if len(removed) == 1:
            r = removed[0]
            current_phrases = [p for p in current_phrases if p.lower() != r.lower()]
            current_zones_map.pop(r, None)
            vigilance["attention_phrases"] = current_phrases[-20:]
            if not current_zones_map:
                vigilance.pop("attention_phrases_zones", None)
            else:
                vigilance["attention_phrases_zones"] = current_zones_map
            result_note = f"Listo, quité la frase “{r}”. Te quedan {len(current_phrases)} frase(s) activa(s)."
        elif len(removed) > 1:
            # Varias coinciden: pedir confirmación al usuario enumerándolas.
            vigilance.pop("attention_phrases", None)  # no modificar todavía
            opts = "\n".join(f"  {i+1}. {p}" for i, p in enumerate(removed))
            result_note = f"Varias frases coinciden con “{value}”. ¿Cuál quieres que quite?\n{opts}\nDime el número o el nombre exacto."
        else:
            # Ninguna frase coincide con lo que el usuario pidió quitar.
            if is_normal_flag or is_no_alert_explicit:
                # "no alertes por X es normal" → anotar como excepción, no quitar.
                note = f"Nota del dueño: cuando pasa “{value}”, es normal, no lo menciones."
                if note not in current_notes:
                    current_notes.append(note)
                vigilance["owner_notes"] = current_notes[-20:]
                result_note = f"Anotado: cuando pasa “{value}” lo considero normal y no te alerto."
            elif current_phrases:
                # Eva PREGUNTA: no falla en silencio. Enumera las frases activas
                # para que el usuario confirme cuál quiere quitar por número o nombre.
                vigilance.pop("attention_phrases", None)  # no modificar todavía
                active = "\n".join(f"  {i+1}. {p}" for i, p in enumerate(current_phrases[:10]))
                result_note = (f"No encontré ninguna frase que coincida con “{value}”. "
                               f"Tienes {len(current_phrases)} frase(s) activa(s):\n{active}\n"
                               f"Dime el número de la que quieres quitar (o repítela).")
            else:
                result_note = "No tienes ninguna frase de vigilancia configurada todavía. Usa “vigila que…” para añadir una."

    elif is_remove_intent and not current_phrases:
        result_note = "No tienes ninguna frase de vigilancia configurada todavía. Usa “vigila que…” para añadir una."

    # ── AGREGAR una frase de atención ──
    add_markers = ("vigila que", "vigilar que", "avísame si", "avisame si", "alerta si", "alertar si",
                   "alertame si", "notifícame cuando", "notificame cuando", "vigila cuando",
                   "afínate de", "afínate cuando", "ojo con", "pónme alerta si", "ponme alerta si")
    add_match = next((mk for mk in add_markers if mk in m), None)
    if add_match:
        value = _clean_behavior(_extract_behavior_after(message, [add_match]))
        if value and value.lower() not in [p.lower() for p in current_phrases]:
            current_phrases.append(value)
            vigilance["attention_phrases"] = current_phrases[-20:]
            # Fase 4: detectar si la frase menciona una zona configurada.
            zone_name = _detect_phrase_zone(user_id, camera_id, value)
            if zone_name:
                current_zones_map[value] = zone_name
                vigilance["attention_phrases_zones"] = current_zones_map
                result_note = f"Agregué la frase de vigilancia: “{value}” (zona: {zone_name}). Ahora vigilo {len(current_phrases)} frase(s)."
            else:
                result_note = f"Agregué la frase de vigilancia: “{value}”. Ahora vigilo {len(current_phrases)} frase(s)."
        elif value:
            result_note = f"Esa frase “{value}” ya la estaba vigilando."
    else:
        for marker in ("solo alerta si",):
            behavior = _clean_behavior(_extract_behavior_after(message, [marker]))
            if behavior and behavior not in current_phrases:
                current_phrases.append(behavior)
                vigilance["attention_phrases"] = current_phrases[-20:]
                zone_name = _detect_phrase_zone(user_id, camera_id, behavior)
                if zone_name:
                    current_zones_map[behavior] = zone_name
                    vigilance["attention_phrases_zones"] = current_zones_map
                result_note = f"Agregué la frase de vigilancia: “{behavior}”{(' (zona: ' + zone_name + ')') if zone_name else ''}. Ahora vigilo {len(current_phrases)} frase(s)."
                break

    # ── Horario ──
    if any(w in m for w in ("horario", "abre", "cierra", "apertura", "cierre")):
        schedule = _parse_schedule(message)
        if not schedule:
            return {"needs_clarification": True, "text": "Para cambiar el horario necesito la hora de apertura y cierre. Por ejemplo: “abre 8:00am y cierra 10:00pm”."}

    return {"vigilance": vigilance, "schedule": schedule, "mode": mode, "result_note": result_note}


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
        lines.append(f"🔍 Frases de atención ({len(attention_phrases)}):")
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
        "el pico", "muestrame el pico", "muestra el pico", "cual fue el pico",
        "cuando fue el pico", "a que hora fue el pico", "aforo pico", "trafico maximo",
        "maximo de personas", "peak",
        "quien eres", "que eres", "para que sirves", "eres una persona", "eres humano",
        "como te llamas", "confund", "desubic", "sigue en configuracion",
        "no se pudo instalar", "fallo la instalacion", "cuanto gane", "ganancia", "ventas",
        "ingresos", "incendio", "humo", "fuego", "riesgo", "diario", "eventos",
        "ultimos analisis", "que ha pasado hoy", "que ha pasado ayer", "historial",
        "ultimo evento", "ultimos eventos", "hubo algo", "paso algo",
        "regla de", "frase de", "frases de", "reglas de",
        "quita la", "quitar la", "quita de la", "quitar de la",
        "elimina la", "eliminar la", "elimina de la", "eliminar de la",
        "borra la", "borrar la", "borra de la", "borrar de la",
        "saca la", "sacar la",
        "no alertes", "no alertar", "deja de vigilar", "dejar de vigilar",
        "vigila que", "vigilar que", "avisame si", "avísame si", "alerta de",
    ))


async def handle_eva_v2(user_id, message, session_id, cam_id=None, include_frame=False, storage_root=STORAGE_ROOT):
    msg_norm = _normalize_text(message or "")
    pending_setup = _pending_session_for_user(user_id)
    ud = _load_user_data(user_id)
    cam_count = _count_configured_cameras(user_id, ud)
    if pending_setup and pending_setup.get("phase") not in (SetupPhase.DONE.value, "os") and not _is_os_intent(msg_norm) and cam_count == 0:
        return await _resume_pending_setup(pending_setup, user_id, session_id, message, storage_root)
    if _is_os_intent(msg_norm) and not _is_new_camera_intent(msg_norm):
        # Si hay un setup en progreso (pending), RESPECTARLO: el msg de OS intent
        # ("vigila que...") debe procesarse dentro del wizard del setup, no
        # pisarlo con una nueva OS session. Solo vamos a OS mode si no hay
        # pending (o ya terminó) o si el user NO tiene cameras y quiere setup.
        if pending_setup and pending_setup.get("phase") not in (SetupPhase.DONE.value, "os") and cam_count == 0:
            return await _resume_pending_setup(pending_setup, user_id, session_id, message, storage_root)
        sid = session_id or f"chat_{user_id}_{int(time.time())}"
        session = _load_session(sid)
        if not session or session.get("user_id") != user_id or session.get("phase") not in (SetupPhase.DONE.value, "os"):
            session = _make_os_session(user_id, sid)
            _sessions[sid] = session
        if cam_id:
            session["camera_id"] = cam_id
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


def _render_summary_lines(result, period_label):
    """Arma las líneas del resumen diario en ES desde el resultado de
    tool_get_activity_summary. Reutilizable para hoy/ayér/rango."""
    total = result.get("total_events", 0)
    attention_events = result.get("attention_events", 0)
    persons_total = result.get("persons_total", 0)
    tracking_max = result.get("tracking_unique_max", 0)
    peak_hour = result.get("peak_hour")
    tag_counts = result.get("tag_counts", {}) or {}
    top_phrases = result.get("top_attention_phrases", []) or []
    last_summary = result.get("last_summary", "")
    details = result.get("details", {}) or {}
    last_yolo = result.get("last_yolo", {}) or {}
    counts_total = result.get("counts_total", {}) or {}

    lines = []
    label = period_label or "Hoy"
    lines.append(f"Resumen: {label} se realizaron {total} análisis de seguridad.")
    lines.append("")

    if attention_events > 0:
        lines.append(f"🔍 {attention_events} análisis coincidieron con lo que me pediste vigilar.")
        for p in top_phrases[:3]:
            lines.append(f"   • “{p.get('frase','')}” ({p.get('count',0)}x)")
    else:
        lines.append("✅ No se detectaron coincidencias con lo que me pediste vigilar.")

    if tracking_max > 0:
        lines.append(f"👥 Personas distintas vistas: hasta {tracking_max} a la vez (según tracker).")
    elif persons_total > 0:
        lines.append(f"👥 Personas en la escena: hasta {persons_total} a la vez (según tracker).")

    if peak_hour is not None:
        lines.append(f"📈 Hora más activa: {peak_hour:02d}:00.")

    if tag_counts:
        top_tags = sorted(tag_counts.items(), key=lambda kv: kv[1], reverse=True)[:5]
        lines.append("🏷️ Objetos frecuentes: " + ", ".join(f"{t} ({c})" for t, c in top_tags) + ".")

    platos = counts_total.get("platos", 0)
    bebidas = counts_total.get("bebidas", 0)
    fundas = counts_total.get("fundas", 0)
    clientes = counts_total.get("clientes_estimado", 0)
    if platos > 0:
        lines.append(f"🍽️ Platos visibles: ~{platos}.")
    if bebidas > 0:
        lines.append(f"🥤 Bebidas visibles: ~{bebidas}.")
    if fundas > 0:
        lines.append(f"🛍️ Fundas utilizadas: ~{fundas}.")
    if clientes > 0:
        lines.append(f"🧑‍🤝‍🧑 Clientes observados: ~{clientes}.")

    lines.append(f"⚙️ Objetos en último análisis: {last_yolo.get('count', 0)}.")
    classes = last_yolo.get("classes", [])
    if classes:
        lines.append(f"   Tipos: {', '.join(classes[:5])}.")

    if last_summary:
        lines.append("")
        lines.append(f"📝 Último análisis: {last_summary[:200]}")

    scene = details.get("scene_context", "")
    if scene:
        lines.append("")
        lines.append(f"🏪 Contexto: {scene}")
    return lines


async def _handle_daily_summary(session, user_id, message, session_id):
    first = session["owner_name"].split()[0] if session.get("owner_name") else "amigo"
    ud = _load_user_data(user_id)
    cam_count = len([c for c in ud.get("cameras",[]) if c.get("active")]) if ud.get("cameras") else 0
    suggestions = _get_business_suggestions_list(ud.get("business_type",""), cam_count, first)

    from eva.tools import tool_get_activity_summary
    result = await tool_get_activity_summary(user_id, "today")
    total = result.get("total_events", 0)
    attention_events = result.get("attention_events", 0)
    top_phrases = result.get("top_attention_phrases", []) or []

    if total == 0:
        text = f"Hoy no se han registrado análisis todavía. La cámara está activa y Eva está atenta a cualquier actividad."
        session["msgs"].append({"role":"assistant","content":text})
        _sessions[session_id] = session
        return _mk_resp(session, text, suggestions=suggestions)

    lines = _render_summary_lines(result, "Hoy")
    if top_phrases and attention_events > 0:
        lines.append("")
        lines.append("¿Quieres que ajuste lo que vigilo o que marque alguna de esas observaciones como falsa alarma?")
    # Encabezado en estilo de tarjeta conservado
    lines[0] = f"Resumen del día: Hoy se realizaron {total} análisis de seguridad."
    text = "\n".join(lines)

    notable_events = result.get("notable_events", [])

    session["msgs"].append({"role":"assistant","content":text})
    _sessions[session_id] = session
    return _mk_resp(session, text, suggestions=suggestions, events_found=notable_events)


def _parse_hermes_tool_call(content):
    """Parsea respuestas de hermes-style tool calls del modelo Qwen.

    Detecta varios formatos que Qwen usa empiricamente:
    - <tool>{...}</tool>     (Hermes-classic)
    - ```json\n{...}\n```   (markdown code block)
    - ```tool_call\n{...}\n``` (Hermes v3)
    - {...} suelto (al inicio o final del content)
    - {"tool": "...", "params": ...} (route style)
    """
    if not content:
        return None
    import re

    # 1) <tool>...</tool>
    match = re.search(r'<tool>\s*(\{.*?\})\s*</tool>', content, re.DOTALL)
    if match:
        try:
            data = json.loads(match.group(1))
            if "name" in data:
                return data
            if "tool" in data:
                return {"name": data.pop("tool"), "arguments": data.get("params", data.get("arguments", {}))}
        except json.JSONDecodeError:
            pass

    # 2) ```json ... ``` o ```tool_call ... ``` con JSON dentro
    for fence_pat in [r'```json\s*(\{.*?\})\s*```', r'```tool_call\s*(\{.*?\})\s*```', r'```\s*(\{.*?\})\s*```']:
        m = re.search(fence_pat, content, re.DOTALL)
        if m:
            try:
                data = json.loads(m.group(1))
                if "name" in data:
                    return data
                if "tool" in data:
                    return {"name": data.pop("tool"), "arguments": data.get("params", data.get("arguments", {}))}
            except json.JSONDecodeError:
                pass

    # 3) stripped completo
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

    # 4) cualquier objeto JSON en el content que contenga "name" o "tool"
    for json_match in re.finditer(r'\{[^{]*"name"[^}]*\}', content, re.DOTALL):
        try:
            data = json.loads(json_match.group())
            if "name" in data:
                return data
        except json.JSONDecodeError:
            pass
    for json_match in re.finditer(r'\{[^{]*"tool"[^}]*\}', content, re.DOTALL):
        try:
            data = json.loads(json_match.group())
            if "tool" in data:
                return {"name": data.pop("tool"), "arguments": data.get("params", data.get("arguments", {}))}
        except json.JSONDecodeError:
            pass
    return None

async def _detect_intent_and_route(user_id, message, first, recent, cam_count, session):
    """Detecta intenciones comunes y las responde directamente sin pasar por el LLM."""
    msg_norm = _normalize_text(message)

    # ── "muéstrame el pico" / "pico" / "peak": reutiliza el conteo del turno previo si existe ──
    if any(p in msg_norm for p in [
        "muestrame el pico", "muéstrame el pico", "el pico", "muestra el pico",
        "cual fue el pico", "cuál fue el pico", "cuando fue el pico", "cuándo fue el pico",
        "a que hora fue el pico", "a qué hora fue el pico", "peak", "aforo pico",
        "trafico maximo", "tráfico máximo", "maximo de personas", "máximo de personas",
    ]):
        # 1) Intentar reutilizar el resultado raw del count_people del turno anterior
        reused = None
        asked_yesterday = any(w in msg_norm for w in ("ayer", "anoche", "dia anterior", "día anterior"))
        if isinstance(session, dict):
            for m in reversed(session.get("msgs", [])):
                if isinstance(m, dict) and m.get("role") == "tool_full" and m.get("tool") == "count_people":
                    r = m.get("result", {})
                    if r.get("peak_count") and r.get("peak_time"):
                        reused = r
                    break
        if reused:
            total = reused.get("total_people", 0)
            peak = reused.get("peak_count", 0)
            peak_time = reused.get("peak_time", "")
            # Heredar el día del resultado anterior; el usuario puede override con "ayer"
            dia = "ayer" if (asked_yesterday or reused.get("dia") == "ayer") else "hoy"
            text = f"El pico de {dia} fue de **{peak} persona(s)** a las **{peak_time}**."
            if total:
                text += f" En total detecté {total} persona(s) {dia}."
            return {"text": text, "events": []}
        # 2) Sin datos previos → calcular horas pico en el día pedido (sin inventar)
        from eva.tools import tool_peak_hours
        date_param = "yesterday" if asked_yesterday else "today"
        result = await tool_peak_hours(user_id, date=date_param, top_n=3)
        # Solo si el usuario pregunto por "hoy" y hoy esta vacio, recurrir a ventana 24h ("recent")
        # NUNCA suplantar hoy con ayer sin que el usuario lo pidiera.
        if (not result.get("success") or not result.get("top_peak")) and not asked_yesterday:
            result = await tool_peak_hours(user_id, date="recent", top_n=3)
        if result.get("success") and result.get("top_peak"):
            return {"text": result.get("message", ""), "events": []}
        dia = "ayer" if asked_yesterday else "hoy"
        return {"text": f"Aún no tengo un pico registrado para {dia}, {first}. En cuanto detecte movimiento te lo digo.", "events": []}

    # ── "qué ha pasado hoy" / "cuéntame" / "novedades": resumen accionable con tool_full persistente ──
    if any(p in msg_norm for p in [
        "que ha pasado hoy", "que paso hoy", "que ha pasado", "que paso",
        "cuentame que", "cuentame que ha pasado", "cuentame que paso", "cuentame",
        "dime que ha pasado", "dime que paso", "que tal el dia", "que tal el día",
        "novedades de hoy", "novedades hoy", "algunas novedades", "que hubo hoy", "que hubo",
        "como va el dia", "como va el día", "que se sabe", "dame un resumen",
    ]):
        asked_yesterday = any(w in msg_norm for w in ("ayer", "anoche", "dia anterior", "día anterior"))
        date_param = "yesterday" if asked_yesterday else "today"
        from eva.tools import tool_count_people, tool_latest_events
        cp = await tool_count_people(user_id, date=date_param)
        le = await tool_latest_events(user_id, limit=4, date=date_param)
        total_people = cp.get("total_people", 0)
        peak = cp.get("peak_count", 0)
        peak_time = cp.get("peak_time", "")
        events = le.get("events", [])
        # Persistir tool_full(count_people) para que "muéstrame el pico" pueda reutilizarlo
        _store_tool_full(session, "count_people", {
            "text": "", "events": [], "dia": ("ayer" if asked_yesterday else "hoy"),
            "total_people": total_people, "sessions": cp.get("sessions", 0),
            "peak_count": peak, "peak_time": peak_time,
            "cameras": cp.get("cameras", []),
        })
        dia_label = "ayer" if asked_yesterday else "hoy"
        lines = []
        if total_people or events:
            if asked_yesterday:
                lines.append("Resumen de ayer:")
            else:
                lines.append("Resumen de hoy:")
            if total_people:
                lines.append(f"  · {total_people} persona(s) detectadas"
                             + (f" en {cp.get('sessions',1)} visita(s)" if cp.get("sessions",1) > 1 else "")
                             + (f". Pico de {peak} a las {peak_time}." if peak and peak_time else "."))
            else:
                lines.append(f"  · No detecté personas {dia_label} (sin objetos contables).")
            if events:
                lines.append("  · Últimos registros:")
                for item in events[:4]:
                    mode_label = "🛡️ centinela" if item.get("mode") == "centinela" else "📋 normal"
                    lines.append(f"      - {mode_label} · {item.get('datetime','')[:5]} · {item.get('camera_name','')}: {item.get('description','')[:100]}")
        else:
            # HOY sin datos → NUNCA inventar de ayer. Decir honestamente que no hay actividad.
            if asked_yesterday:
                lines.append(f"Ayer no se registró actividad. La cámara pudo estar apagada.")
            else:
                lines.append(f"Hoy no he detectado actividad todavía. Si la cámara está apagada, no habrá registros hasta que se conecte. En cuanto capte movimiento te aviso.")
        return {"text": "\n".join(lines), "events": []}

    if any(p in msg_norm for p in ["cuantas personas", "cuántas personas", "cuantas personas hoy", "cuántas personas hoy", "cuantas personas has", "cuántas personas has"]):
        from eva.tools import tool_get_activity_summary
        result = await tool_get_activity_summary(user_id, "today")
        total = result.get("total_events", 0)
        tracking_max = result.get("tracking_unique_max", 0)
        persons = result.get("persons_total", 0)
        peak_hour = result.get("peak_hour")
        if total == 0:
            return {"text": f"Hoy no se han registrado personas todavía. La cámara está activa y Eva está atenta."}
        shown = tracking_max if tracking_max > 0 else persons
        extra = f" Hora más activa: {peak_hour:02d}:00." if peak_hour is not None else ""
        return {"text": f"Hoy se detectaron hasta {shown} persona(s) a la vez en {total} análisis.{extra} Último análisis: {result.get('last_summary', '')[:150]}"}

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

    # Resumen de ayer / "y ayer que tal" / "como estuvo ayer" / "ayer que paso"
    is_yesterday_query = any(w in msg_norm for w in ("ayer", "anoche", "dia anterior", "día anterior"))
    is_summary_intent = any(p in msg_norm for p in (
        "que tal", "como estuvo", "cómo estuvo", "que paso", "qué pasó", "como nos fue",
        "cómo nos fue", "resumen", "como nos fue ayer", "que tal ayer", "que paso ayer",
        "que hubo", "que hicimos", "novedad", "novedades", "algo que sepas", "reporte de ayer",
    ))
    if is_yesterday_query and is_summary_intent and not _is_new_camera_intent(msg_norm):
        from eva.tools import tool_get_activity_summary
        result = await tool_get_activity_summary(user_id, "yesterday")
        total = result.get("total_events", 0)
        if total == 0:
            return {"text": "Ayer no se registraron análisis. La cámara pudo estar inactiva o sin movimiento detectado."}
        lines = _render_summary_lines(result, "Ayer")
        return {"text": "\n".join(lines), "events": []}

    # Resumen de hoy / "y hoy que tal"
    if (any(w in msg_norm for w in ("hoy", "este dia", "este día")) and is_summary_intent
            and not is_yesterday_query and not _is_new_camera_intent(msg_norm)):
        from eva.tools import tool_get_activity_summary
        result = await tool_get_activity_summary(user_id, "today")
        total = result.get("total_events", 0)
        if total == 0:
            return {"text": "Hoy no se han registrado análisis todavía. La cámara está activa y Eva está atenta."}
        lines = _render_summary_lines(result, "Hoy")
        return {"text": "\n".join(lines), "events": []}

    # Donde se acumula la gente / heatmap / zonas mas transitadas / mapa de calor
    if any(p in msg_norm for p in ["donde se acumula", "dónde se acumula", "mapa de calor", "heatmap", "heat map",
                                    "zonas mas transitadas", "zonas más transitadas", "que zonas son mas", "que parte del local"]):
        from eva.tools import tool_heatmap_data
        date_param = "yesterday" if any(w in msg_norm for w in ("ayer", "anoche")) else "today"
        result = await tool_heatmap_data(user_id, date=date_param, grid_size=16)
        if result.get("success"):
            return {
                "text": result.get("message", ""),
                "events": [],
                "heatmap": result.get("heatmap"),
                "heatmap_meta": {
                    "grid_size": result.get("grid_size"),
                    "image_dimensions": result.get("image_dimensions"),
                    "hotspots": result.get("hotspots"),
                    "zone_counts": result.get("zone_counts"),
                    "total_points": result.get("total_points"),
                    "date": date_param,
                },
            }
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
        if await _is_intent_confirmed(message, "Usuario completando pasos de conexión"):
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
    if await _is_intent_confirmed(message, f"Esperando imagen. Usuario dice: {message}") and not frame:
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

    # ── v16: Evaluación de calidad de imagen (WOW #1 — asistente de colocación) ──
    iluminacion = img_analysis.get("iluminacion","")
    contraluz = img_analysis.get("contraluz", False)
    orientacion = img_analysis.get("orientacion","")
    visibilidad = img_analysis.get("visibilidad_objetivo","")
    sugerencia = img_analysis.get("sugerencia_posicion","")

    lines = ["📷 Cámara conectada ✅\n", f"Zona configurada: {zone}"]
    if zona_real: lines.append(f"Zona detectada: {zona_real}")
    lines.append(f"\nDescripción: {img_desc}")

    # Feedback de calidad (WOW #1)
    quality_notes = []
    if iluminacion and iluminacion != "buena":
        quality_notes.append(f"💡 Iluminación: {iluminacion}")
    if contraluz:
        quality_notes.append("⚠️ Veo contraluz — la cámara está apuntando hacia la luz")
    if orientacion and orientacion != "correcta":
        quality_notes.append(f"🔄 Orientación: {orientacion}")
    if visibilidad and visibilidad != "buena":
        quality_notes.append(f"👁️ Visibilidad del área: {visibilidad}")
    if sugerencia:
        quality_notes.append(f"🔍 Sugerencia: {sugerencia}")

    if quality_notes:
        lines.append("\n" + "\n".join(quality_notes))

    if position_ok:
        lines.append(f"\n✅ La posición se ve bien para vigilar **{zone}**.")
    else:
        lines.append(f"\n⚠️ La imagen no coincide con la zona **{zone}**.")
        if zona_real: lines.append(f"Parece enfocando **{zona_real}**.")

    # Pregunta abierta — el usuario decide
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
            # Si el usuario dice "sí" pero el campo no tiene valor aún, interpretar
            # como "paso al siguiente paso con valor default", NO "Dímelo concreto".
            # Esto evita que el wizard se quede trabado en owner_notes cuando el
            # usuario dijo "sí" para confirmar pero el campo quedó vacío.
            default = _context_default(step, zone)
            session[step] = default
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
        default = _context_default(step, zone)
        if step == "attention_phrases":
            answer = _parse_attention_phrases(msg)
            if not answer:
                answer = default
        elif step == "owner_notes":
            # "no", "ninguno", "nada" son respuestas válidas: el usuario NO quiere
            # agregar notas ni excepciones. Avanzamos con valor vacío.
            if _is_no(msg) or _is_skippable_answer(msg):
                answer = default
            else:
                answer = _clean_context_answer(msg, default)
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

def _store_tool_full(session, tool_name, result):
    """Persiste el resultado raw (JSON) de una herramienta para contexto del siguiente turno."""
    if not isinstance(session, dict):
        return
    msgs = session.setdefault("msgs", [])
    payload = {"role": "tool_full", "tool": tool_name, "result": {
        k: v for k, v in (result or {}).items() if k in (
            "success", "text", "message", "dia",
            "total_people", "sessions", "events_count", "peak_count", "peak_time", "cameras",
            "total_events", "persons_total", "attention_events",
            "found", "events", "is_open", "business_hours",
            "groups", "group_by", "period",
            "heatmap", "grid_size", "hotspots", "zone_counts", "total_points",
        ) and not isinstance(v, (bytes,))
    }}
    msgs.append(payload)
    # Limitar a los últimos 4 tool_full para evitar crecimiento indefinido
    tf = [m for m in msgs if isinstance(m, dict) and m.get("role") == "tool_full"]
    if len(tf) > 4:
        for m in tf[:-4]:
            try:
                msgs.remove(m)
            except ValueError:
                pass


def _format_prior_tool_context(session):
    """Construye un bloque de texto con los datos raw de las últimas herramientas ejecutadas."""
    if not isinstance(session, dict):
        return ""
    tf = [m for m in session.get("msgs", []) if isinstance(m, dict) and m.get("role") == "tool_full"]
    if not tf:
        return ""
    lines = ["=== DATOS DE HERRAMIENTAS ANTERIORES (turno previo) ==="]
    for m in tf[-3:]:
        tool = m.get("tool", "?")
        try:
            snippet = json.dumps(m.get("result", {}), ensure_ascii=False)[:600]
        except (TypeError, ValueError):
            snippet = str(m.get("result", ""))[:600]
        lines.append(f"[{tool}] {snippet}")
    lines.append("Si el usuario pregunta por estos datos (ej. 'el pico', 'cuántas personas', 'el total'), RESPONDE usando estos datos exactos. NO pidas fecha ni invoques de nuevo la herramienta.")
    return "\n".join(lines)


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
        top_phrases = result.get("top_attention_phrases", []) or []
        notable_events = result.get("notable_events", [])
        if total == 0:
            text = "Hoy no se han registrado análisis todavía. La cámara está activa y Eva está atenta."
        else:
            lines = _render_summary_lines(result, "Hoy")
            lines[0] = f"Resumen del día: Hoy se realizaron {total} análisis de seguridad."
            if top_phrases and attention_events > 0:
                lines.append("")
                lines.append("¿Quieres que ajuste lo que vigilo o que marque alguna de esas observaciones como falsa alarma?")
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
        date_param = "yesterday" if any(w in msg_lower for w in ("ayer", "anoche", "dia anterior", "día anterior")) else "today"
        tool_result = await _execute_os_tool_v2(user_id, "count_people", {"date": date_param, "camera_id": best_cam}, message, first, recent, cam_count, session)
        _store_tool_full(session, "count_people", {**tool_result, "dia": ("ayer" if date_param == "yesterday" else "hoy")})
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

    # ── Falsa alarma / confirmación de evento (Decision 2, vía chat) ──
    false_alert_kw = ("fue falsa alarma", "falsa alarma", "fue falsa", "no avises por eso",
                      "no alertes por eso", "eso fue normal", "eso no es nada",
                      "es confirmado", "si paso", "sí pasó", "eso si es real", "eso sí es real")
    if any(k in msg_lower for k in false_alert_kw):
        eid = session.get("last_event_id")
        user_uid = session.get("user_id") or user_id
        if not eid:
            text = f"{first}, no tengo ninguna alerta reciente en este chat para marcar. Abre una alerta y dime «fue falsa alarma» justo después, o dime el evento por su hora."
            session["msgs"].append({"role": "assistant", "content": text})
            _sessions[session_id] = session
            return _mk_resp(session, text, suggestions=suggestions, events_found=[])
        is_false = any(k in msg_lower for k in ("fue falsa alarma", "falsa alarma", "fue falsa",
                                                "no avises por eso", "no alertes por eso",
                                                "eso fue normal", "eso no es nada"))
        from eva.tools import tool_learn_from_feedback
        fb = await tool_learn_from_feedback(event_id=eid, is_real=not is_false,
                                            notes=(message if is_false else None), user_id=user_uid)
        if fb.get("success"):
            if is_false:
                text = f"{first}, anoté la alerta como falsa alarma. La voy a dejar de vigilar cuando pase algo parecido, y registro la nota para que el centinela afine. ✅"
            else:
                text = f"{first}, confirmé la alerta como real. Voy a seguir atento a casos parecidos. 👁️"
            # Limpio la marca para no releerla en cada turno
            session["last_event_id"] = None
            session["msgs"].append({"role": "assistant", "content": text})
            _sessions[session_id] = session
            return _mk_resp(session, text, suggestions=suggestions, events_found=[])
        logger.warning(f"[EVA false-alarm] feedback failed for {eid}: {fb.get('error')}")

    # ── Routing de frases de vigilancia ──
    # Detectamos INTENCIÓN de agregar/quitar frases de vigilancia por verbos
    # (no marcadores literales exactos — son frágiles). El router Qwen NO tiene
    # update_vigilance_config en su lista de tools (es OS-only), por lo que este
    # atajo es el único camino hacia _build_vigilance_update_from_message (que
    # hace matching fuzzy + desambiguación conversacional).
    #
    # GUARD: solo en modo operativo (phase DONE). Si el usuario está en setup
    # (GREET/ZONE/.../CONFIRM), NO interceptamos "vigila que..." aquí porque eso
    # rompería el context_step de attention_phrases del wizard de registro. En
    # setup esas frases las procesa _handle_context normalmente.
    is_done = session.get("phase") == SetupPhase.DONE.value or session.get("phase") == "os"
    list_kw = ("que estas vigilando", "qué estás vigilando", "muestrame las reglas", "muéstrame las reglas",
               "muestrame las frases", "muéstrame las frases", "muestrame que vigilas", "muéstrame qué vigilas",
               "muestra que vigilas", "muestra qué vigilas", "mostrame que vigilas", "selas frases", "selas reglas",
               "selas frases de", "como estan las frases", "cómo están las frases",
               "que vigilas", "qué vigilas", "lista de reglas", "lista de frases",
               "dime que vigilas", "dime qué vigilas", "dime las frases", "dime las reglas",
               "que estas observando", "qué estás observando", "como quedo la vigilancia", "cómo quedó la vigilancia")
    if is_done and any(k in msg_lower for k in list_kw):
        best_cam = session.get("camera_id") or await _pick_best_camera_id(user_id) or ""
        tool_result = await _execute_os_tool_v2(user_id, "get_vigilance_config", {"camera_id": best_cam}, message, first, recent, cam_count, session)
        session["msgs"].append({"role": "assistant", "content": tool_result.get("text", "")})
        _sessions[session_id] = session
        return _mk_resp(session, tool_result.get("text", ""), suggestions=suggestions, events_found=tool_result.get("events", []))

    # Intención de AGREGAR frase (verbos de vigilar/alertar).
    import re as _re_intent
    add_intent = any(_re_intent.search(p, msg_lower) for p in (
        r"\b(?:vigila|vigilar|avisa|avisame|avísame|alerta|alertame|notifícame|notificame|ojo con|alertame)\b.*\b(?:que|si|cuando|con|de|por)\b",
        r"\b(?:ponme|pónme)\s+alerta\b",
        r"\b(?:afínate|afinate)\b",
    ))
    # Intención de QUITAR frase (verbos de eliminar + mención de regla/frase, o
    # "no alertes"/"deja de vigilar"). Tolerante: acepta "quita de la regla de",
    # "elimina regla de", "borra la frase", "saca la vigilancia", etc.
    remove_intent = any(_re_intent.search(p, msg_lower) for p in (
        r"\b(?:quita|quitar|elimina|eliminar|borra|borrar|saca|sacar|suprime|suprimir)\b.*\b(?:regla|frase|vigilancia|vigilar|de|la|el)\b",
        r"\b(?:deja\s+de|dejar\s+de)\s+vigilar\b",
        r"\b(?:no\s+alertes|no\s+alertar|no\s+me\s+alertes|no\s+me\s+alertar)\b",
        r"\b(?:quita|quitar|elimina|eliminar|borra|borrar|saca|sacar)\b\s+(?:la|el|los|las|de\s+la|de\s+el)\s",
    ))
    if is_done and (add_intent or remove_intent):
        best_cam = session.get("camera_id") or await _pick_best_camera_id(user_id) or ""
        tool_result = await _execute_os_tool_v2(user_id, "update_vigilance_config", {"camera_id": best_cam}, message, first, recent, cam_count, session)
        session["msgs"].append({"role": "assistant", "content": tool_result.get("text", "")})
        _sessions[session_id] = session
        return _mk_resp(session, tool_result.get("text", ""), suggestions=suggestions, events_found=tool_result.get("events", []))

    if any(k in msg_lower for k in ("__adjust_protection__", "ajustar proteccion", "ajustar protección", "cambiar proteccion", "cambiar protección", "configurar proteccion")):
        best_cam = await _pick_best_camera_id(user_id) or ""
        tool_result = await _execute_os_tool_v2(user_id, "get_vigilance_config", {"camera_id": best_cam}, message, first, recent, cam_count, session)
        session["msgs"].append({"role": "assistant", "content": tool_result.get("text", "")})
        _sessions[session_id] = session
        return _mk_resp(session, tool_result.get("text", ""), suggestions=suggestions, events_found=tool_result.get("events", []))

    intent_result = await _detect_intent_and_route(user_id, message, first, recent, cam_count, session)
    if intent_result:
        session["msgs"].append({"role":"assistant","content":intent_result["text"]})
        _sessions[session_id] = session
        return _mk_resp(
            session,
            intent_result["text"],
            suggestions=suggestions,
            events_found=intent_result.get("events", []),
            heatmap=intent_result.get("heatmap"),
            heatmap_meta=intent_result.get("heatmap_meta"),
        )

    sys_p = (
        f"Eres Eva, asistente de seguridad inteligente de OjoIA en República Dominicana.\n"
        f"Trabajas para {session.get('owner_name','el dueño')} en su negocio "
        f"\"{ud.get('business_name','su negocio')}\" (giro: {ud.get('business_type','')}).\n"
        f"Tienes {cam_count} cámara(s) activa(s) observando el lugar.\n\n"
        f"=== ANTECEDENTES — actividad reciente del diario ===\n{recent}\n\n"
        f"=== TU PERSONALIDAD ===\n"
        f"- Hablas español dominicano, natural y friendly (pero profesional cuando importa).\n"
        f"- Llamas al usuario por su nombre cuando es posible ({first}).\n"
        f"- Eres proactiva: usas las herramientas para buscar respuestas REALES, no inventas.\n"
        f"- Si no sabes algo exacto, lo dices honestamente y propones qué SÍ puedes investigar.\n\n"
        f"=== TUS HERRAMIENTAS (usa una SOLO cuando necesites datos concretos) ===\n"
        "- get_activity_summary(date today|yesterday, camera_id?): Resume el día — total análisis, conteos de personas, alertas, tags de objetos detectados (platos, dinero, fundas, etc.).\n"
        "- search_events(query?, date today|yesterday|YYYY-MM-DD, camera_id?, person_class hombre|mujer|nino|anciano?, clothing?, min_persons?, max_persons?, activity trabajando|hablando|entrando?, importance baja|media|alta|critica?, limit 1-10): Busca eventos puntuales. El `query` permite texto natural como 'refresco', 'plato', 'dinero entra a la caja', 'empleado'. IMPORTANTE: YOLO detecta objetos como (platos, vasos, refrescos, fundas, dinero, datáfono, sillas, mesas, comida, bebidas...) y los busca aqui.\n"
        "- event_book(date today|yesterday|YYYY-MM-DD, group_by hour|camera|ten_minute, only_importance?, camera_id?): Indice cronologico agrupable. Ideal para 'que paso entre 2 y 4 pm', 'dame el diario de hoy', 'como estuvo la última hora'.\n"
        "- find_anomalies(min_severity baja|media|alta|critica, date, camera_id?, limit): Eventos con coincidencias de frases de vigilancia (realmente anormales).\n"
        "- latest_events(limit 1-10, date?, camera_id?): Lista los últimos análisis cronológicos. Útil para 'que esta pasando ahora', 'muéstrame lo último'.\n"
        "- count_people(date today|yesterday, camera_id?): Conteo de personas únicas, pico y total detectado. NO confundir con 'platos vendidos'.\n"
        "- traffic_flow(date, camera_id?): Cuántos entraron/salieron por la zona 'entrance'.\n"
        "- peak_hours(date, top_n 3?): Top horas con más gente.\n"
        "- heatmap_data(date, grid_size): Datos de densidad por celda — 'donde se acumula la gente'.\n"
        "- zone_dwell(date, anomaly_min_minutes?, zone_id, camera_id?): Permanencia por zona — 'cuánto en la caja', 'quién estuvo más de 30 min'.\n"
        "- is_open_hours(): Horario del negocio abierto/cerrado.\n"
        "- list_employees(): Empleados registrados con face_id, rol y horario.\n"
        "- identify_face(camera_id): Quién aparece ahora en la cámara.\n"
        "- get_latest_frame(camera_id?): Imagen reciente para que la observes.\n"
        "- analyze_frame(camera_id, prompt): Analizas un frame con questiones visuales.\n"
        "- save_event(camera_id, summary, importance): Registras un evento manualmente.\n"
        "- update_vigilance_config(camera_id, mode, schedule, attention_phrases, owner_notes): Cambias qué vigilar ('vigila que...', 'quita la regla de...').\n"
        "- get_vigilance_config(camera_id?): Lees qué frases estás vigilando actualmente.\n\n"
        "=== CÓMO USAR HERRAMIENTAS ===\n"
        "Si necesitas una herramienta, responde SOLO con JSON:\n"
        "```json\n{\"name\": \"nombre_herramienta\", \"arguments\": {\"param\": \"valor\"}}\n```\n"
        "Si no necesitas herramientas (charla general, saludo, aclaracion), responde directamente al usuario.\n\n"
        "=== INTENCIÓN → TOOL — ejemplos ===\n"
        "- 'cuántos platos se vendieron hoy' → search_events con query='plato' (NO digas 'no tengo acceso a ventas': busca los platos detectados por YOLO)\n"
        "- 'cuántos refrescos vendí' → search_events con query='refresco' (igual: YOLO detecta refrescos)\n"
        "- 'mande alguien al banco' → traffic_flow o count_people\n"
        "- 'hubo anomalías hoy' → find_anomalies min_severity='media'\n"
        "- 'qué pasó entre 3pm y 5pm' → event_book group_by='hour'\n"
        "- 'cuántos clientes vinieron' → count_people\n"
        "- 'cuánto tiempo estuvo alguien en la caja' → zone_dwell\n"
        "- 'dime las reglas que vigilas' → get_vigilance_config (NO hardcoded responses)\n\n"
        "Reglas IMPORTANTES:\n"
        f"- NO inventes datos. Si una herramienta devuelve 0 o vacío, dilo claramente.\n"
        f"- Si el usuario pregunta por algo que SÍ puede inferirse de los eventos YOLO (ventas ≈ detecciones de platos/dinero/coca-cola), USA la herramienta.\n"
        f"- NO te limites a 'asistente de seguridad' si la información es observable (ventas, clientes, movimiento).\n"
        f"- Responde en español dominicano. Máx 4-6 frases.\n"
    )
    msgs = [{"role":"system","content":sys_p}]
    tool_ctx = _format_prior_tool_context(session)
    if tool_ctx:
        msgs.insert(0, {"role":"system","content":tool_ctx})
    # v13: pasar historial COMPLETO (últimos 12 msgs) en vez de solo 3 truncados.
    # Antes h.content[:1200] cortaba respuestas largas con eventos+resumen; Eva
    # perdía el contexto de que el usuario ya había preguntado algo similar.
    # Ahora: más contexto (12 msgs), truncado por TOKENS aprox (4500 chars), no por msg.
    history_msgs = []
    total_chars = 0
    CHAR_BUDGET = 4500
    # [Fix B] Lista de frases que indican que Eva se rindió sin usar tools.
    # Si las dejamos en el historial, Qwen replica el patrón y vuelve a decir
    # "no tengo información" en vez de emitir un tool_call. Las omitimos para
    # que el modelo vea solo la pregunta del usuario (que es la señal útil).
    NEGATIVE_PATTERNS = (
        "no tengo información", "no tengo acceso", "no tengo datos",
        "no tengo acceso a ventas", "no puedo acceder a", "no puedo ver",
        "no tengo acceso a la información", "no tengo forma de saber",
        "no estoy conectada a", "no tengo manera de",
        # [Fix C] Patrones elusivos: Eva menciona una tool sin ejecutarla.
        "no tengo acceso directo", "no tengo acceso a los datos",
        "no puedo dar", "no tengo forma de",
        "sin embargo, puedo buscar", "puedo buscar eventos relacionados",
        "puedo usar la herramienta", "puedo usar las herramienta",
        "no puedo proporcionar datos específicos",
        "no tengo acceso a los registros", "no tengo registros de ventas",
    )
    def _is_negative_assistant(c: str) -> bool:
        cl = (c or "").lower()
        if not cl:
            return True
        return any(p in cl for p in NEGATIVE_PATTERNS)

    for h in session.get("msgs",[])[-12:]:
        if not isinstance(h,dict) or h.get("role") not in ("user","assistant"):
            continue
        c = h.get("content","")
        # [Fix B] Saltar respuestas negativas del assistant que ensenan a Qwen
        # a rendirse en vez de emitir tool_calls. Conservamos el mensaje del
        # usuario previo para mantener contexto conversacional.
        if h.get("role") == "assistant" and _is_negative_assistant(c):
            continue
        # Truncar msg individual solo si es absurdo (>1500 chars).
        # Recortamos al final (mas reciente) si el total se pasa del budget.
        if len(c) > 1500:
            c = c[:1490] + "…"
        history_msgs.append({"role": h["role"], "content": c})
        total_chars += len(c)
    # Si nos pasamos del budget, descartar los más viejos
    while total_chars > CHAR_BUDGET and history_msgs:
        removed = history_msgs.pop(0)
        total_chars -= len(removed["content"])
    msgs.extend(history_msgs)
    msgs.append({"role":"user","content":message})
    # [Fix A] Pasar tools nativas a Qwen para que pueda emitir tool_calls en
    # formato OpenAI-native (sin esto, el LLM solo ve el prompt textual y
    # responde en lenguaje natural con "no tengo información...").
    # El parser textual (_parse_hermes_tool_call) sigue como fallback para
    # el caso en que Qwen emita JSON suelto dentro del content.
    os_tools = _os_tools_openai()
    response = await _call_qwen(msgs, max_tokens=500, tools=os_tools)
    content = response.get("content", "").strip()
    native_tool_calls = response.get("tool_calls", []) or []
    # [DIAG] log para ver qué formato usa Qwen en su 1er reply
    try:
        tc_preview = ""
        if native_tool_calls:
            tc_preview = " native_tc=" + json.dumps(native_tool_calls, ensure_ascii=False)[:240]
        logger.info("[EVA_DIAG] 1st_llm_content_len=%d preview=%r%s", len(content), content[:300], tc_preview)
    except Exception:
        pass

    # Prioridad 1: tool_calls nativos de OpenAI
    tool_call = None
    if native_tool_calls:
        tc0 = native_tool_calls[0]
        fn = tc0.get("function", {}) if isinstance(tc0, dict) else {}
        name = fn.get("name", "")
        args = fn.get("arguments", {})
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except Exception:
                args = {}
        if name:
            tool_call = {"name": name, "arguments": args or {}}

    # Prioridad 2: parser textual Hermes (fallback)
    if not tool_call:
        tool_call = _parse_hermes_tool_call(content)

    # [Fix D] Retry con tool_choice="required": si el primer call respondio
    # en texto natural pero MENCION un tool por nombre (caso clasico: "puedo
    # usar search_events..."), forzamos un re-intento que SI emita tool_call.
    # Solo si el mensaje del usuario parece pedir informacion concreta.
    if not tool_call and content:
        intro_low = content.lower()
        info_request_markers = (
            "cuantos", "cuantas", "cuántos", "cuántas", "dime", "muestra",
            "muéstrame", "hubo", "cuál", "cual", "que paso", "qué pasó",
            "resumen", "diario", "alerta", "pico",
        )
        user_low = (message or "").lower()
        is_info_request = any(m in user_low for m in info_request_markers)
        # Buscar si content menciona nombres de tools reales
        known_tool_names = set(_OS_TOOL_DEFINITIONS.keys())
        mentioned_tools = [t for t in known_tool_names if t in intro_low]
        if is_info_request and mentioned_tools:
            logger.info("[EVA_DIAG] retry_required: content menciona %s sin emitir tc",
                        ",".join(mentioned_tools[:3]))
            try:
                retry = await _call_qwen(msgs, max_tokens=500, tools=os_tools, tool_choice="required")
                retry_tcs = retry.get("tool_calls", []) or []
                retry_content = retry.get("content", "").strip()
                logger.info("[EVA_DIAG] retry_result: tool_calls=%d content_len=%d",
                            len(retry_tcs), len(retry_content))
                if retry_tcs:
                    tc0 = retry_tcs[0]
                    fn = tc0.get("function", {}) if isinstance(tc0, dict) else {}
                    rn = fn.get("name", "")
                    ra = fn.get("arguments", {})
                    if isinstance(ra, str):
                        try: ra = json.loads(ra)
                        except Exception: ra = {}
                    if rn:
                        tool_call = {"name": rn, "arguments": ra or {}}
                        content = retry_content or content  # mantener content del retry si no es None
                else:
                    # intento Hermes parser sobre el retry content tambien
                    tc_text = _parse_hermes_tool_call(retry_content)
                    if tc_text:
                        tool_call = tc_text
                        content = retry_content
            except Exception as e:
                logger.warning("[EVA_DIAG] retry_required fallo: %s", e)

    try:
        logger.info("[EVA_DIAG] parsed_tool_call=%s", "NONE" if not tool_call else tool_call.get("name"))
    except Exception:
        pass
    if tool_call:
        tool_name = tool_call.get("name", "")
        tool_args = tool_call.get("arguments", {})
        if tool_name in ("get_activity_summary", "search_events", "find_anomalies", "latest_events", "find_risks", "count_people", "is_open_hours", "list_employees", "identify_face", "event_book", "traffic_flow", "zone_dwell", "heatmap_data", "peak_hours"):
            result = await _execute_os_tool_v2(user_id, tool_name, tool_args, message, first, recent, cam_count, session)
            tool_result_msg = json.dumps(result, ensure_ascii=False)[:800]
            msgs.append({"role":"assistant","content":content})
            msgs.append({"role":"tool","tool_call_id":"hermes","content":tool_result_msg})
            _store_tool_full(session, tool_name, result)
            biz = ud.get('business_name','')
            biz_type = session.get('business_type','')
            # [Fix C] Reescrito: Eva es asistente del NEGOCIO (no solo seguridad).
            # Refuerzo explícito de que el resultado del tool ES la verdad y
            # "0" es una respuesta válida, NO un error que requiera disculparse.
            final_sys_p = (
                f"Eres Eva, asistente del negocio de {session.get('owner_name','el dueño')} "
                f"({biz} — {biz_type}) en República Dominicana.\n\n"
                f"=== RESULTADO DE HERRAMIENTA ({tool_name}) ===\n{tool_result_msg}\n\n"
                f"=== REGLAS DE RESPUESTA ===\n"
                f"- El resultado de la herramienta ES la verdad. Úsalo literalmente.\n"
                f"- Si dice found=0, total=0 o events=[]: dilo como 'Detecté 0 ... hoy'. "
                f"NO digas 'no tengo información' ni te disculpes: 0 es una respuesta válida.\n"
                f"- NO agregues datos que no estén en el resultado.\n"
                f"- NO menciones que 'usaste una herramienta': responde al usuario directamente.\n"
                f"- Responde en español dominicano, máx 4 frases, natural y directo.\n"
                f"- NO ofrezcas 'buscar eventos' como alternativa: ya buscaste.\n"
            )
            final_msgs = [{"role":"system","content":final_sys_p}]
            # v13: pasar mismos 12 msgs de contexto para que Eva pueda referenciar
            # la pregunta del usuario sin perder el hilo conversacional.
            # [Fix B] Saltar respuestas negativas del assistant (mismo filtro de arriba).
            for h in session.get("msgs",[])[-12:]:
                if not isinstance(h,dict) or h.get("role") not in ("user","assistant"):
                    continue
                c = h.get("content","")
                if h.get("role") == "assistant" and _is_negative_assistant(c):
                    continue
                if len(c) > 1500:
                    c = c[:1490] + "…"
                final_msgs.append({"role":h["role"],"content":c})
            final_msgs.append({"role":"user","content":message})
            final = await _call_qwen(final_msgs, max_tokens=400)
            text = final.get("content", "").strip()
            # [DIAG] log del 2do LLM call (respuesta final) para depurar
            try:
                logger.info("[EVA_DIAG] 2nd_llm_final_text_len=%d preview=%r", len(text), text[:240])
            except Exception:
                pass
            # [Fix C2] Preferir el `text` preformado por el tool si el LLM cayo en
            # "no tengo info"/"puedo buscar" a pesar del resultado. Si pasa 2 veces,
            # caemos al texto del tool que SIEMPRE dice "Detecté N ..." (estilo consistente).
            tool_text = (result.get("text") or "").strip() if isinstance(result, dict) else ""
            if text and _is_negative_assistant(text):
                logger.info("[EVA_DIAG] 2nd_llm_negative Detected, falling back to tool_text")
                text = tool_text or text
            if not text:
                text = tool_text or f"No pude procesarlo, {first}."
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
         "- Si pide AGREGAR o QUITAR frases/reglas de vigilancia (vigila que..., avisame si..., quita la regla de..., elimina/borra la frase de..., deja de vigilar..., no alertes por...), SIEMPRE usa update_vigilance_config (NO respondas conversacionalmente; el parser detecta y aplica el cambio).\n"
         "- Si pregunta por las frases/reglas actuales ('que vigilas', 'muestrame las frases', 'como quedo la vigilancia'), usa get_vigilance_config.\n"
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
        phrases = data.get("attention_phrases", []) or []
        zones_map = v.get("attention_phrases_zones", {}) or {}
        if not isinstance(zones_map, dict):
            zones_map = {}
        mode_txt = 'centinela (noche)' if data.get('mode') == 'sentinel' else 'estándar (dentro de horario)'
        text = (
            f"{first}, esto es lo que vigilo en {data.get('camera_id') or 'la cámara'}:\n"
            f"Modo ahora: {mode_txt}.\n"
        )
        if phrases:
            text += f"Frases de vigilancia ({len(phrases)}):\n"
            for p in phrases[:8]:
                zn = zones_map.get(p)
                text += f"  • {p}{('  (zona: ' + zn + ')') if zn else ''}\n"
            if len(phrases) > 8:
                text += f"  … y {len(phrases)-8} más.\n"
        else:
            text += "No tienes frases de vigilancia configuradas. Dime “vigila que…” para agregar una.\n"
        if data.get("owner_notes"):
            text += f"Notas del dueño ({len(data.get('owner_notes', []))}):\n"
            for n in data.get("owner_notes", [])[:3]:
                text += f"  • {n}\n"
        text += "\nPara cambiarlas: “vigila que nadie se lleve platos sin pasar por caja” o “quita la regla de …”."
        return {"text": text, "events": []}

    if tool_name == "update_vigilance_config":
        params = {**params}
        if not params.get("camera_id"):
            params["camera_id"] = _extract_camera_id_from_message(message) or await _pick_best_camera_id(user_id)
        inferred = _build_vigilance_update_from_message(user_id, params.get("camera_id", ""), message)
        if inferred.get("needs_clarification"):
            return {"text": inferred.get("text", "Necesito más datos."), "events": []}
        # IMPORTANTE: si el parser (inferred) detectó una ACCIÓN CONCRETA de
        # agregar/quitar frase (result_note presente), su vigilance tiene prioridad
        # sobre lo que alucinó Qwen en params (p.ej. mode=sentinel). Esto evita que
        # Qwen sobrescriba el cambio de frases con un cambio de modo irrelevante.
        inferred_has_phrase_action = bool(inferred.get("result_note")) and bool(
            (inferred.get("vigilance") or {}).get("attention_phrases") is not None
            or "owner_notes" in (inferred.get("vigilance") or {}))
        if inferred_has_phrase_action:
            vigilance = inferred.get("vigilance") or {}
            schedule = inferred.get("schedule")
            mode = None  # no cambiar modo si la acción es de frases
        else:
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
        if not inferred_has_phrase_action and "attention_phrases" in params:
            vigilance["attention_phrases"] = params["attention_phrases"]
        if not inferred_has_phrase_action and "owner_notes" in params:
            vigilance["owner_notes"] = params["owner_notes"]
        data = await _tool_call("update_vigilance_config", user_id, {
            "camera_id": params.get("camera_id", ""),
            "vigilance": vigilance or None,
            "schedule": schedule,
            "mode": mode,
        })
        if not data.get("success"):
            return {"text": f"No pude actualizar: {data.get('error', 'error')}", "events": []}
        result_note = inferred.get("result_note", "")
        # Si el parser generó una nota específica (agregué/quité frase), la usamos;
        # si no, armamos el texto genérico de modo/sensibilidad.
        if result_note:
            text = f"{first}, {result_note}"
        else:
            v = data.get("vigilance", {})
            normal = v.get("normal_mode", {}) if isinstance(v.get("normal_mode"), dict) else {}
            sentinel = v.get("sentinel_mode", {}) if isinstance(v.get("sentinel_mode"), dict) else {}
            mode_text = 'modo centinela' if data.get('mode') == 'sentinel' else 'modo estándar'
            text = f"{first}, protección actualizada: {mode_text}. Sensibilidad: {normal.get('sensitivity', '—')}. Centinela: {'activo' if sentinel.get('enabled', False) else 'inactivo'}."
        # Si es modo normal y no hay frases, sugerimos agregar
        phrases = data.get("attention_phrases", []) or []
        if data.get('mode') != 'sentinel' and not phrases:
            text += "\nNo tienes frases de vigilancia todavía. Dime “vigila que…” para agregar una."
        return {"text": text, "events": []}

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
            mode_label = "🛡️ centinela" if item.get("mode") == "centinela" else "📋 normal"
            parts.append(f"- {mode_label} · {item.get('datetime', '')} · {item.get('camera_name', '')}: {item.get('descripcion', '')}.")
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
            mode_label = "🛡️ centinela" if item.get("mode") == "centinela" else "📋 normal"
            parts.append(f"- {mode_label} · {item.get('datetime', '')} · {item.get('camera_name', '')}: {item.get('description', '')[:120]}")
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
        dia = "ayer" if params.get("date") == "yesterday" else "hoy"
        text = f"Detecté **{total} persona(s)** {dia} en {cameras}."
        if sessions > 1:
            text += f" Fueron {sessions} visitas distintas."
        if peak > 0 and peak_time:
            text += f" El pico fue de {peak} persona(s) a las {peak_time}."
        return {
            "text": text, "events": [], "dia": dia,
            "total_people": total, "sessions": sessions,
            "peak_count": peak, "peak_time": peak_time,
            "cameras": data.get("cameras", []),
        }

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

# Cache del formato OpenAI tools generado a partir de _OS_TOOL_DEFINITIONS.
# Qwen/Hermes-native responde con tool_calls nativos al estilo OpenAI sólo
# cuando se pasa `tools=` en el payload. Sin esto, el LLM no sabe qué tools
# existen y responde en lenguaje natural ("no tengo información...").
_OS_TOOLS_OPENAI_CACHE: list = []


def _os_tools_openai() -> list:
    """Convierte _OS_TOOL_DEFINITIONS al formato OpenAI tools=
    (lista de {type:'function', function:{name,description,parameters}}).
    Generado una sola vez y cacheado."""
    if _OS_TOOLS_OPENAI_CACHE:
        return _OS_TOOLS_OPENAI_CACHE
    out = []
    for name, spec in _OS_TOOL_DEFINITIONS.items():
        params = spec.get("parameters", {}) or {}
        properties = {}
        required = []
        for k, v in params.items():
            # v viene como string de tipo o dict; normalizar a OpenAI schema
            if isinstance(v, dict):
                properties[k] = v
                if v.get("required"):
                    required.append(k)
            else:
                t = (v or "string").lower()
                ot = "string"
                if t in ("integer", "int", "number"):
                    ot = "integer"
                elif t in ("object", "dict"):
                    ot = "object"
                elif t in ("array", "list"):
                    ot = "array"
                elif t in ("boolean", "bool"):
                    ot = "boolean"
                properties[k] = {"type": ot}
        schema = {"type": "object", "properties": properties}
        if required:
            schema["required"] = required
        out.append({
            "type": "function",
            "function": {
                "name": name,
                "description": spec.get("description", ""),
                "parameters": schema,
            },
        })
    _OS_TOOLS_OPENAI_CACHE.extend(out)
    return _OS_TOOLS_OPENAI_CACHE


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

def _mk_resp(session, text, img_b64="", ready_to_confirm=False, camera_saved=False, suggestions=None, force_image=False, events_found=None, heatmap=None, heatmap_meta=None):
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
    # Persistir el último event_id visto para que frases como "esa alerta fue
    # falsa alarma" puedan resolver "esa" en el siguiente turno.
    if isinstance(events_found, list) and events_found:
        for ev in events_found:
            if isinstance(ev, dict) and ev.get("event_id") and not ev["event_id"].startswith("vigilance_"):
                session["last_event_id"] = ev["event_id"]
                session["last_event_camera_id"] = ev.get("camera_id") or session.get("camera_id", "")
                break
    return {
        "success": True, "response": text, "image_url": img_url,
        "session_id": session["session_id"], "phase": session["phase"],
        "zone": session.get("zone",""), "has_image": bool(session.get("image_b64")),
        "ready_to_confirm": ready_to_confirm, "camera_saved": camera_saved,
        "suggestions": suggestions or [], "events_found": events_found or [],
        "heatmap": heatmap or None,
        "heatmap_meta": heatmap_meta or None,
    }
