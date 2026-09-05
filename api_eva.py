#!/usr/bin/env python3
import os, sys
_BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)
"""
OjoIA API Eva v7.1 — "Cámara Primero, Reglas Después"
Flujo optimizado: conectar cámara en turno 3-5, reglas basadas en imagen real.
Mejoras v7.1:
- Análisis inteligente de imagen al primer frame (640px, no 512px)
- Eva da opinión experta sobre ubicación de cámara (¿es correcta? ¿enfocada? ¿obstáculos?)
- Sugiere ajustes específicos basados en tipo de negocio + imagen real
- describe_image mejorado: 60 palabras, análisis de nitidez, ángulo, obstáculos
"""
 
import os
import json
import re
import time
import base64
import secrets
import asyncio
import logging
import threading
import hmac
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Optional, Dict, List, Any

from fastapi import Query
from fastapi import FastAPI, UploadFile, File, HTTPException, Form, Request, Header, Depends
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse, FileResponse
from starlette.status import HTTP_200_OK
from pydantic import BaseModel
import httpx
import firebase_admin
from firebase_admin import auth, credentials

# Importar modulos locales
from gateway_resize import resize_image, image_to_base64, add_frame_watermark
from orchestrator import orchestrator
import camera_zones

# Configuracion Global
STORAGE_ROOT = Path("/home/sam/storage")
DISKS_CONFIG_FILE = STORAGE_ROOT / "disks_config.json"
EVA_CONFIG_FILE = STORAGE_ROOT / "eva_config.json"
# A9: path canonico de firebase-key.json (antes /home/sam/Downloads/, que es un
# directorio de descargas no controlado). El archivo vive en ai_system/ con
# permisos 600. Las 3 referencias en este archivo apuntan aqui ahora.
FIREBASE_KEY_PATH = Path(os.environ.get("FIREBASE_KEY_PATH", "/opt/ojoia/config/firebase-key.json"))

# ─────────────────────────────────────────────────────────────────────────
# S4: Lock de user.json — protege escrituras concurrentes.
# 25 puntos del codigo escriben sobre user.json sin sincronizacion. Para
# evitar races (token FCM perdido, sesion de Eva corrupta) centralizamos
# aqui un lock por user_id. Los puntos criticos (push-token register/
# unregister, pruner daemon, save_eva_chat_message, chat_eva_message)
# usan este helper. El resto se migra en Fase 2.
# ─────────────────────────────────────────────────────────────────────────
_USER_JSON_LOCKS: Dict[str, threading.Lock] = {}
_USER_JSON_LOCKS_GUARD = threading.Lock()

def _get_user_lock(user_id: str) -> threading.Lock:
    """Devuelve (creando si hace falta) un lock por user_id."""
    with _USER_JSON_LOCKS_GUARD:
        lk = _USER_JSON_LOCKS.get(user_id)
        if lk is None:
            lk = threading.Lock()
            _USER_JSON_LOCKS[user_id] = lk
        return lk

def _atomic_write_user_json(uf: Path, ud: dict) -> None:
    """Escribe user.json de forma atomica: temp + rename. LLamar dentro del lock."""
    uf.parent.mkdir(parents=True, exist_ok=True)
    tmp = uf.with_suffix(uf.suffix + ".tmp")
    payload = json.dumps(ud, indent=2, ensure_ascii=False)
    tmp.write_text(payload)
    tmp.replace(uf)


def update_user_json(user_id: str, mutator):
    """C1 (2026-08-31) — Read-modify-write SEGURO de user.json.

    Patrón único para los ~18 sitios que antes hacían:
        read json → mutar → write (sin lock, sin atomicidad)
    Con concurrencia (last_frame cada segundo + FCM + billing), eso
    perdía escrituras y podía dejar el JSON corrupto.

    Uso:
        def _mut(ud):
            ud["fcm_tokens"] = [...]
        update_user_json(user_id, _mut)

    - Lock por usuario (threading) alrededor de TODO el read+write.
    - Escritura atómica (tmp + rename). Un crash nunca deja JSON a medias.
    - mutator recibe el dict y lo muta in-place; si lanza excepción,
      NO se escribe nada (estado previo intacto).
    Devuelve el dict resultante.
    """
    uf = find_user_json(user_id)
    if not uf:
        uf = Path(STORAGE_ROOT) / "users" / user_id / "user.json"
    lock = _get_user_lock(user_id)
    with lock:
        ud = {}
        if uf.exists():
            with open(uf) as f:
                ud = json.load(f)
        mutator(ud)
        # RD-7 (2026-09-02): DUAL-WRITE bajo el mismo lock. Con usuarios
        # migrados, uf puede ser el HDD mientras lectores legacy leen el
        # compat NVMe (STORAGE_ROOT/users/uid) — el registro de tokens FCM
        # acababa en un solo disco y según quién leyera, el token estaba o
        # no (bug real visto hoy). Ahora: ambos espejos siempre sincronos.
        _atomic_write_user_json(uf, ud)
        compat = Path(STORAGE_ROOT) / "users" / user_id / "user.json"
        if compat != uf:
            try:
                _atomic_write_user_json(compat, ud)
            except Exception as e:
                logger.warning(f"[RD-7] dual-write compat falló {user_id[:8]}…: {e}")
        return ud


# ─────────────────────────────────────────────────────────────────────────
# UN SOLO CHAT por usuario (EVA-UNIFY) — helpers de sesión unificada
#
# BUG: el frontend bifurcaba el historial en varias sesiones ("os_<uid>",
# "chat_<uid>_<ts>", "eva_<uid>_single") y el GET del history LAS JUNTABA
# TODAS, mezclando chats distintos y duplicando mensajes.
#
# FIX: a partir de ahora TODO el historial vive en UNA sesión por usuario,
# llamada "chat_<uid>". Tanto el GET como el POST y el endpoint de mensaje
# usan este session_id estable.
#
# `_eva_unified_sid(user_id)` devuelve ese id. La primera vez que se lee, se
# consolidan los mensajes legacy (de os_<uid>, eva_<uid>_single,
# chat_<uid>_<ts>, etc.) dentro de "chat_<uid>" y se borran las sesiones
# viejas, para evitar duplicados y mezcla futuras. Idempotente.
# ─────────────────────────────────────────────────────────────────────────

_LEGACY_SESSION_PREFIXES = ("os_", "eva_", "chat_")


def _eva_unified_sid(user_id: str) -> str:
    return f"chat_{user_id}"


def _consolidate_legacy_eva_sessions(ud: dict, user_id: str) -> None:
    """Mueve los mensajes de sesiones legacy (os_<uid>, eva_<uid>_single,
    chat_<uid>_<ts>, etc.) a la sesión unificada "chat_<uid>" y elimina las
    sesiones viejas. Idempotente: si ya se consolidó, no hace nada.
    Mutar ud["eva_sessions"] in-place. No escribe en disco.
    """
    sessions = ud.get("eva_sessions", {}) or {}
    if not isinstance(sessions, dict):
        sessions = {}
        ud["eva_sessions"] = sessions
    unified_sid = _eva_unified_sid(user_id)
    unified = sessions.get(unified_sid, {}) or {}
    if not isinstance(unified, dict):
        unified = {}
    unified_msgs = list(unified.get("messages", []) or [])
    unified_keys = set((m.get("role"), m.get("content"), int(m.get("timestamp", 0) or 0)) for m in unified_msgs)

    migrated = 0
    to_remove = []
    for sid, sdata in sessions.items():
        if sid == unified_sid:
            continue
        if not isinstance(sdata, dict):
            continue
        # Solo consolidar sesiones legacy de chat (no sesiones internas de reportes
        # u otros usos que pudieran existir). Filtramos por prefijo conocido.
        if not any(sid.startswith(p) for p in _LEGACY_SESSION_PREFIXES):
            continue
        msgs = sdata.get("messages", []) or []
        for m in msgs:
            if not isinstance(m, dict):
                continue
            key = (m.get("role", "user"), m.get("content"), int(m.get("timestamp", 0) or 0))
            if key in unified_keys:
                continue
            unified_msgs.append(m)
            unified_keys.add(key)
            migrated += 1
        to_remove.append(sid)

    if not migrated and not to_remove:
        # Nada que migrar: dejamos ud intacto.
        return

    # Ordenar por timestamp tras consolidar
    unified_msgs.sort(key=lambda m: int(m.get("timestamp", 0) or 0))
    # Limitar a 200
    unified_msgs = unified_msgs[-200:]
    new_last = max([int(m.get("timestamp", 0) or 0) for m in unified_msgs] or [0])
    # Preservar summary y created_at si existían
    sessions[unified_sid] = {
        "messages": unified_msgs,
        "summary": unified.get("summary", ""),
        "created_at": unified.get("created_at", int(time.time())),
        "last_message_at": max(new_last, int(unified.get("last_message_at", 0) or 0)),
    }
    # Eliminar sesiones legacy que se consolidaron
    for sid in to_remove:
        sessions.pop(sid, None)
    ud["eva_sessions"] = sessions


# ─────────────────────────────────────────────────────────────────────────
# S1/M1.1 - Bearer token auth para usuarios (soft rollout)
#
# Flujo soft rollout:
#   - Nuevo endpoint POST /api/auth/token genera token random y lo guarda
#     en user.json.api_tokens[] (con created_at).
#   - await _verify_user_token(header) valida Authorization: Bearer <token>.
#   - Endpoints protegidos llaman a await _verify_user_token(...) como dependencia
#     con user_id conocido; si la auth falta, loguea warning pero ACEPTA.
#   - Adelante (Fase 2): switch a enforce=True para rechazar 401.
#
# Rutas PUBLICAS (sin auth, ni ahora ni en rollout strict): /health, /api/auth/token,
# /admin/auth/login (admin usa su propio sistema), endpoings de ingest de camaras
# (las camaras no saben portar bearer), /vigilance-frame/*, archivos estaticos.
# ─────────────────────────────────────────────────────────────────────────
AUTH_ENFORCE = True  # A1: enforce estricto activado. Antes False (warn-only).
# Riesgo de negocio cerrado: el cliente activo es de prueba. En rollback duro
# cualquier peticion a /api/* sin Bearer valido para el user_id recibira 401.
AUTH_TOKEN_TTL_SEC = 60 * 60 * 24 * 90  # 90 dias por defecto


def _generate_user_token() -> str:
    """Token aleatorio URL-safe de 32 bytes (~256 bits de entropia)."""
    return secrets.token_urlsafe(32)


async def _verify_user_token(authorization: Optional[str], user_id: str) -> dict:
    """
    Valida Authorization: Bearer <token> contra user.json.api_tokens[] O contra
    Firebase ID Token (el SPA usa Firebase Auth). Async porque verificar el
    Firebase token requiere await asyncio.to_thread(auth.verify_id_token).

    Soft rollout (AUTH_ENFORCE=False): si la auth falta o es invalida,
    se loguea WARNING pero el request continúa (devuelve identity=None).
    Hard rollout (AUTH_ENFORCE=True): lanza 401 si falta o es invalida.
    """
    if not authorization:
        if AUTH_ENFORCE:
            raise HTTPException(status_code=401, detail="Authorization header requerido")
        logger.warning(f"[auth:soft] user={user_id} request SIN Authorization header")
        return {"authenticated": False, "user_id": user_id, "reason": "no_header"}

    # A2 fix (2026-08-09): Starlette entrega el header Authorization como objeto
    # Header (o como lista si llega repetido) cuando se declara con
    # `Header(None, alias="Authorization")` en el endpoint. Antes llamabamos
    #authorization.replace("Bearer ", "") directamente y revientaba con
    # "'Header' object has no attribute 'replace'" en TODOS los endpoints que
    # validan token explicitamente (chat de Eva, history POST, ...). El plan A1
    # (AUTH_ENFORCE=True) hizo que el header SIEMPRE llegue desde el SPA y
    # destapo bug latente. Normalizamos a str de forma robusta antes de .replace.
    if isinstance(authorization, (list, tuple)):
        authorization = authorization[0] if authorization else ""
    if not isinstance(authorization, str):
        authorization = str(authorization)

    token = authorization.replace("Bearer ", "").strip()
    if not token:
        if AUTH_ENFORCE:
            raise HTTPException(status_code=401, detail="Token invalido")
        logger.warning(f"[auth:soft] user={user_id} Authorization malformado")
        return {"authenticated": False, "user_id": user_id, "reason": "bad_header"}

    # A1 (complemento Firebase): primero validar contra api_tokens[] locales
    # (token emitido por /api/auth/token). Si no hay match, intentar validar
    # como Firebase ID Token (el SPA usa Firebase Auth y envia getIdToken()).
    # Esto cierra el flujo del SPA sin romper la seguridad: el uid del
    # Firebase token DEBE coincidir con el user_id de la query (anti-suplantacion).
    try:
        uf = find_user_json(user_id)
        if not uf or not uf.exists():
            if AUTH_ENFORCE:
                raise HTTPException(status_code=401, detail="Usuario no encontrado")
            return {"authenticated": False, "user_id": user_id, "reason": "no_user"}

        # 1) token local (api_tokens[])
        with _get_user_lock(user_id):
            with open(uf) as f:
                ud = json.load(f)
            tokens = ud.get("api_tokens", []) or []
            now = int(time.time())
            valid = [t for t in tokens if not t.get("revoked") and
                     now < t.get("expires_at", now + AUTH_TOKEN_TTL_SEC * 100)]
            match = None
            for t in valid:
                if hmac.compare_digest(str(t.get("token", "")), token):
                    match = t
                    break

        if match:
            # Refrescar last_used
            match["last_used"] = now
            with _get_user_lock(user_id):
                with open(uf) as f:
                    ud = json.load(f)
                ud["api_tokens"] = [t for t in ud.get("api_tokens", []) if not t.get("revoked") and
                                     now < t.get("expires_at", now + AUTH_TOKEN_TTL_SEC * 100)] + \
                                    [t for t in ud.get("api_tokens", []) if (t.get("revoked") or
                                     now >= t.get("expires_at", 0))][-20:]
                match_idx = next((i for i, t in enumerate(ud["api_tokens"]) if t.get("id") == match.get("id")), None)
                if match_idx is not None:
                    ud["api_tokens"][match_idx]["last_used"] = now
                _atomic_write_user_json(uf, ud)
            return {"authenticated": True, "user_id": user_id, "token_id": match.get("id")}

        # 2) Firebase ID Token (SPA usa Firebase Auth). Verificar uid == user_id.
        # B3: verify_id_token hace llamada de red a Google — fuera del event loop.
        try:
            decoded = await asyncio.to_thread(auth.verify_id_token, token)
            fb_uid = decoded.get("uid")
            if fb_uid and fb_uid == user_id:
                return {"authenticated": True, "user_id": user_id, "token_id": "firebase", "firebase": True}
            # uid no coincide -> rechazar (anti-suplantacion de user_id)
            if AUTH_ENFORCE:
                raise HTTPException(status_code=401, detail="Token Firebase no corresponde a este usuario")
        except HTTPException:
            raise
        except Exception as e:
            # no es un Firebase token valido -> token rechazado
            if AUTH_ENFORCE:
                raise HTTPException(status_code=401, detail="Token no valido o expirado")
            logger.warning(f"[auth:soft] user={user_id} token no valido (local ni firebase): {e}")
            return {"authenticated": False, "user_id": user_id, "reason": "bad_token"}
    except HTTPException:
        raise
    except Exception as e:
        if AUTH_ENFORCE:
            raise HTTPException(status_code=401, detail=f"Error validacion: {e}")
        logger.error(f"[auth:soft] error user={user_id}: {e}")
        return {"authenticated": False, "user_id": user_id, "reason": "error"}
    # Si llegamos aqui sin retornar (solo en soft rollout)
    return {"authenticated": False, "user_id": user_id, "reason": "bad_token"}


# A1: dependencia FastAPI para proteger endpoints /api/* que reciben user_id
# por query, form-body o path. Resuelve el bug de seguridad raiz: antes cualquier
# cliente podia adivinar un user_id y leer sus eventos/frames/stream. Ahora se
# exige un Bearer <token> valido PARA ESE user_id concreto (validado contra
# user.json.api_tokens[] con hmac.compare_digest).
#
# Uso en los endpoints:
#   @app.get("/api/user/events")
#   async def user_events(user_id: str = Query(...), auth: dict = Depends(verify_user)):
#       # auth["user_id"] es el user_id validado; auth["authenticated"] == True
#
# Endpoints PUBLICOS (no llevan Depends(verify_user)): /health, /api/auth/token
# (genera el token, no requiere tenerlo), /api/zone-types, /api/support-info,
# /auth/firebase/verify (Firebase hace su propia verificacion), /ingest/*
# (las camaras ESP32 no portan Bearer - documento en la linea 178), /admin/*
# (usan _verify_admin propio), /reports/{user_id}/{filename} (servidos por
# StaticFiles o path-traversal-sanitized por A5), /vigilance-frame/*
# (notificaciones push con link de imagen).
async def verify_user(
    request: Request,
    authorization: str = Header(None, alias="Authorization"),
) -> dict:
    """
    Dependencia de FastAPI. Lee user_id del request (query/form/path),
    valida Authorization: Bearer <token> contra ese user_id, y devuelve
    la identidad. Si AUTH_ENFORCE=True y no valida -> 401.
    """
    # 1) extraer user_id de la peticion
    user_id = None
    # query params
    try:
        user_id = request.query_params.get("user_id") or request.query_params.get("uid")
    except Exception:
        logger.debug("silent: {exc}", exc=Exception)

    # form params (POST sin JSON body)
    if not user_id:
        try:
            form = asyncio.ensure_future(request.form())
            # form() es awaitable; pero como Dependencia sincrona leemos solo
            # si ya esta cacheado. En gral los endpoints pasan user_id por JSON
            # body o query. Si no llega por query, el endpoint recibira user_id
            # por separado y debemos confiar en que el route body llama a
            # await _verify_user_token(authorization, user_id) explicitamente.
        except Exception:
            logger.debug("silent: {exc}", exc=Exception)

    # 2) path params {user_id} o {uid}
    if not user_id:
        path_params = getattr(request, "path_params", {}) or {}
        user_id = path_params.get("user_id") or path_params.get("uid")
    # 3) JSON body (lo leemos solo si no vino por query/path)
    if not user_id:
        try:
            # evitamos await aqui (dependencia sync); el route body validara.
            # Verificamos solo si el header Authorization viene; si no, 401.
            pass
        except Exception:
            logger.debug("silent: {exc}", exc=Exception)


    if not user_id:
        # si el endpoint no expone user_id de forma estandar, exigimos que traiga
        # Authorization; el route body hara la verificacion fina con _verify_user_token.
        if AUTH_ENFORCE and not authorization:
            raise HTTPException(status_code=401, detail="Authorization requerido")
        return {"authenticated": False, "user_id": None, "reason": "no_user_id"}

    return await _verify_user_token(authorization, user_id)


# A1 (cont.): helper para endpoints que reciben user_id en el JSON body
# (middleware solo ve query/path, no body async). Estos endpoints deben llamar
# a _auth_user_from_body(request) al inicio para validar el token contra el
# user_id extraido del body. Usar:
#
#   @app.post("/api/chat/eva/message")
#   async def eva_message(request: Request):
#       body = await request.json()
#       _auth_user_from_body(request, body.get("user_id", ""))
#
async def _auth_user_from_body(request: Request, user_id: str) -> None:
    """Valida Authorization: Bearer <token> contra user_id del JSON body."""
    if not user_id:
        raise HTTPException(status_code=400, detail="user_id requerido")
    await _verify_user_token(request.headers.get("authorization"), user_id)


async def _parse_json_body(request: Request) -> dict:
    """Lee el body JSON de forma segura (maneja body vacio / no-JSON)."""
    try:
        return await request.json()
    except Exception:
        return {}


# Inicializar Firebase Admin (solo una vez)
try:
    firebase_admin.get_app()
except ValueError:
    if FIREBASE_KEY_PATH.exists():
        cred = credentials.Certificate(str(FIREBASE_KEY_PATH))
        firebase_admin.initialize_app(cred)
    else:
        print(f"Firebase key no encontrado en {FIREBASE_KEY_PATH}")

# Configurar logging
# B1: RotatingFileHandler (antes FileHandler plano -> api_eva.log crecio a 344M
# sin tope y lleno el disco 99%). Ahora rota en 10MB x 5 backups = 50MB maximo.
from logging.handlers import RotatingFileHandler
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        RotatingFileHandler(
            STORAGE_ROOT / "api_eva.log",
            maxBytes=10 * 1024 * 1024,   # 10MB
            backupCount=5,                # 5 archivos rotados + el activo
            encoding="utf-8",
        ),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Estado de Sesiones Eva
eva_sessions: Dict[str, Dict[str, Any]] = {}

# Cola asíncrona para procesamiento de frames (desacoplado del endpoint)
FRAME_QUEUE: asyncio.Queue = asyncio.Queue(maxsize=1000)
WORKER_RUNNING = False
# B5: contador de frames descartados por queue llena. Metrica defensiva:
# antes await FRAME_QUEUE.put() bloqueaba al ESP32 hasta que el worker
# consumiera; ahora intentamos put_nowait y si QueueFull -> drop + metrica.
# El ESP32 recibe respuesta inmediata (no se ve afectado por backpressure).
FRAME_QUEUE_DROPS = 0
FRAME_QUEUE_DROP_TS = 0.0  # ultimo log de drops (rate-limit de log)

# C2 (2026-08-31) — Bus de frames con backend Redis Streams (default) o
# memoria (fallback). Si Redis está disponible, los frames sobreviven a
# reinicios y los workers pueden escalar fuera de este proceso.
from frame_bus import FrameBus
frame_bus = FrameBus()

# C1+C2 — Pool de workers + lock por cámara para evitar race condition.
# ANTES (single worker): 1 task consumia FRAME_QUEUE en serie. Mientras
# ese worker hacia await orchestrator.process_grid (Qwen GPU, 2-5s),
# todos los demas frames esperaban. Techo: ~10-14 camaras.
# AHORA (pool): WORKER_COUNT tasks (4 default, env OJOIA_WORKER_COUNT).
#
# RACE CONDITION (fix critico del colega): con multiples workers
# concurrentes, dos podrian llegar a la misma camara con el grid casi
# lleno. Worker A: add_frame -> full=True -> await process_grid() -> al
# entrar al await cede control; Worker B: add_frame -> overflow (!16=17)
# -> full=True tambien -> dispara process_grid() duplicado/anomalo.
#
# FIX: un asyncio.Lock por (user_id, camera_id). Dentro del lock:
#   - grid.add_frame(...)                             (sincrono, rapido)
#   - is_full -> grid.get_and_reset() para capturar y vaciar a 0
# Fuera del lock (no bloquea a otras camaras):
#   - await orchestrator.process_grid(...)            (GPU, lento)
# El lock es held brevemente (I/O CPU puro), liberado antes del await.
CAMERA_LOCKS: Dict[tuple, asyncio.Lock] = {}

def _get_camera_lock(user_id: str, camera_id: str) -> asyncio.Lock:
    key = (user_id, camera_id)
    lk = CAMERA_LOCKS.get(key)
    if lk is None:
        lk = asyncio.Lock()
        CAMERA_LOCKS[key] = lk
    return lk

# C1: numero de workers en el pool (default 4, ajustable por env).
# No mas de 4 concurrentes porque Qwen en GPU0 tiene Semaphore(12)
# en el orchestrator; superarlo solo generaria await-serialization.
WORKER_COUNT = int(os.getenv("OJOIA_WORKER_COUNT", "4"))


# ── Plan & Billing Helpers ──────────────────────────────────────────────
def _get_plan_field(plan_name: str, field: str, default=None):
    """Read a field from the plan definition in disks_config.json."""
    cfg = get_disk_config()
    plans = cfg.get("plans", {})
    plan_def = plans.get(plan_name, plans.get("free", {}))
    return plan_def.get(field, default)


def _get_plan_features(plan_name: str) -> dict:
    """Get feature flags for a plan."""
    return _get_plan_field(plan_name, "features", {})


def _plan_check(user_data: dict) -> dict:
    """
    Check a user's plan status.
    Returns: {allowed: bool, reason: str, days_left: int, status: str}
    status: 'active' | 'trial' | 'expired' | 'grace' | 'suspended'
    """
    now = time.time()
    plan = user_data.get("plan", "free")
    status = user_data.get("status", "active")
    plan_end = user_data.get("plan_end", 0) or 0
    trial_end = user_data.get("trial_end", 0) or 0
    grace_days = user_data.get("grace_period_days", 3)
    days_left = max(0, int((plan_end - now) / 86400)) if plan_end else 0

    # Trial period check
    if trial_end and now < trial_end:
        trial_days_left = int((trial_end - now) / 86400)
        return {"allowed": True, "reason": None, "days_left": trial_days_left,
                "status": "trial", "plan": plan, "trial_days_left": trial_days_left}

    # Plan expired
    if plan_end and now > plan_end:
        # Check grace period
        grace_end = plan_end + (grace_days * 86400)
        if now < grace_end:
            grace_days_left = int((grace_end - now) / 86400) + 1
            return {"allowed": True, "reason": f"Plan vencido — {grace_days_left} días de gracia restantes",
                    "days_left": 0, "status": "grace", "plan": plan,
                    "grace_days_left": grace_days_left}
        else:
            if status != "suspended":
                user_data["status"] = "suspended"
            return {"allowed": False, "reason": "Plan vencido y período de gracia agotado",
                    "days_left": 0, "status": "suspended", "plan": plan}

    # Active
    if days_left <= 7 and days_left > 0:
        return {"allowed": True, "reason": None, "days_left": days_left,
                "status": "active", "plan": plan, "expiring_soon": True}

    return {"allowed": True, "reason": None, "days_left": days_left,
            "status": "active", "plan": plan}


def _enforce_plan_on_create_camera(user_data: dict) -> dict:
    """Check if user can add a camera. Returns {allowed, reason}."""
    check = _plan_check(user_data)
    if not check["allowed"]:
        return check

    plan = user_data.get("plan", "free")
    max_cams = _get_plan_field(plan, "max_cameras", 1)
    current_cams = len(user_data.get("cameras", []))

    if current_cams >= max_cams:
        return {"allowed": False,
                "reason": f"Límite de {max_cams} cámaras para el plan {plan}. "
                          f"Tienes {current_cams}. Considera actualizar tu plan."}
    return {"allowed": True}


_UD_INGEST_CACHE: Dict[str, tuple] = {}  # uid -> (ts, data) — 60s TTL


def _load_user_data_for_ingest(user_id: str) -> Optional[dict]:
    """Carga user.json para enforcement (cacheada 60s: el ingest es N fps
    y no puede leer disco en cada frame)."""
    now = time.time()
    hit = _UD_INGEST_CACHE.get(user_id)
    if hit and now - hit[0] < 60:
        return hit[1]
    uf = find_user_json(user_id)
    if not uf or not uf.exists():
        return None
    try:
        data = json.loads(uf.read_text())
    except Exception:
        return None
    _UD_INGEST_CACHE[user_id] = (now, data)
    return data


def _invalidate_ud_ingest_cache(user_id: str = None):
    if user_id is None:
        _UD_INGEST_CACHE.clear()
    else:
        _UD_INGEST_CACHE.pop(user_id, None)


def _enforce_plan_on_ingest(user_data: dict) -> dict:
    """Check if user can ingest frames (soft check — never blocks ESP32)."""
    check = _plan_check(user_data)
    if not check["allowed"]:
        return {"allowed": False, "soft_block": True, "reason": check["reason"]}
    return {"allowed": True}


def _compute_access_status(user_data: dict) -> str:
    """Compute overall access status for admin display: active | warning | expired | suspended."""
    check = _plan_check(user_data)
    status = check["status"]
    if status == "suspended":
        return "expired"
    if status == "grace":
        return "warning"
    if check.get("expiring_soon"):
        return "warning"
    return "active"

# App FastAPI
app = FastAPI(title="OjoIA Eva API", version="7.0")


# ═══════════════════════════════════════════════════════════════════════════
# Push tokens — registro, baja y podado automático
# ═══════════════════════════════════════════════════════════════════════════
FIREBASE_PROJECT_ID = "ojoia-67216"
PUSH_TOKEN_MAX_PER_USER = 5
PUSH_TOKEN_PRUNE_EVERY_SEC = 6 * 60 * 60


async def _prune_stale_push_tokens():
    """Cada 6h prueba los tokens recientes con dry_run y elimina stale."""
    from google.oauth2 import service_account
    import google.auth.transport.requests
    import requests as _req
    while True:
        try:
            creds = service_account.Credentials.from_service_account_file(
                os.environ.get("FIREBASE_KEY_PATH", "/opt/ojoia/config/firebase-key.json"),
                scopes=["https://www.googleapis.com/auth/firebase.messaging"]
            )
            creds.refresh(google.auth.transport.requests.Request())
            acc = creds.token
            probe_url = f"https://fcm.googleapis.com/v1/projects/{FIREBASE_PROJECT_ID}/messages:send"
            headers = {"Authorization": f"Bearer {acc}", "Content-Type": "application/json"}
            users_dir = STORAGE_ROOT / "users"
            if not users_dir.is_dir():
                await asyncio.sleep(PUSH_TOKEN_PRUNE_EVERY_SEC); continue
            for u in users_dir.iterdir():
                uf = u / "user.json"
                if not uf.exists(): continue
                try:
                    ud = json.loads(uf.read_text())
                except Exception:
                    continue
                tokens = ud.get("fcm_tokens", []) or []
                if not tokens: continue
                probed = tokens[-2:]
                kept = []
                removed = 0
                for t in probed:
                    try:
                        # B3: requests.post es sincrono; daemon pruner corre en el
                        # event loop principal. Offload al thread pool para no bloquear
                        # otros handlers mientras prueba tokens (8s timeout cada uno).
                        resp = await asyncio.to_thread(
                            lambda: _req.post(
                                probe_url, params={"dry_run": "true"}, headers=headers,
                                json={"message": {"token": t, "notification": {"title": "_", "body": "_"}}},
                                timeout=8
                            )
                        )
                        if resp.status_code == 200:
                            kept.append(t)
                        elif resp.status_code in (400, 404):
                            removed += 1
                            logger.info(f"[push-prune] drop {u.name}: {t[:18]}...")
                        else:
                            kept.append(t)
                    except Exception:
                        kept.append(t)
                if removed:
                    ud["fcm_tokens"] = ([t for t in tokens if t not in probed] + kept)[-PUSH_TOKEN_MAX_PER_USER:]
                    try:
                        # S4: lock para evitar corrupcion con requests concurrentes.
                        with _get_user_lock(u.name):
                            _atomic_write_user_json(uf, ud)
                    except Exception as e_p632:
                        # P0 (Sección #8): loggeo en lugar de pass silencioso
                        logger.warning(f"[push-prune] atomic_write_user_json failed for {u.name}: {e_p632}")
        except Exception as e:
            logger.error(f"[push-prune] cycle error: {e}")
        await asyncio.sleep(PUSH_TOKEN_PRUNE_EVERY_SEC)


@app.on_event("startup")
async def _start_push_token_pruner():
    try:
        asyncio.create_task(_prune_stale_push_tokens())
        logger.info("[push-prune] pruner cada 6h")
    except Exception as e:
        logger.error(f"[push-prune] start fail: {e}")


@app.post("/api/users/push-token")
async def register_push_token(request: Request):
    """Registra token FCM del dispositivo actual."""
    try:
        body = await request.json()
        token = (body.get("token") or "").strip()
        user_id = body.get("user_id")
        await _verify_user_token(request.headers.get("authorization"), user_id)  # A1: anti-suplantacion de user_id en body
        device = body.get("device")
        if not token or not user_id:
            return {"success": False, "error": "user_id y token requeridos"}
        # Buscar user.json con el helper existente para soportar paths custom
        uf = None
        for cand in [user_root(user_id) / "user.json"]:
            if cand.exists():
                uf = cand
        if not uf:
            return {"success": False, "error": f"usuario {user_id} no encontrado"}
        # S4: lock durante read+mutate+write para evitar race con pruner/chat
        with _get_user_lock(user_id):
            with open(uf) as f:
                ud = json.load(f)
            tokens = [t for t in (ud.get("fcm_tokens") or []) if (t or "").strip() != token]
            tokens.append(token)
            ud["fcm_tokens"] = tokens[-PUSH_TOKEN_MAX_PER_USER:]
            if device:
                devs = ud.get("fcm_devices") or {}
                devs[token] = device
                ud["fcm_devices"] = devs
            ud["last_token_refresh"] = int(time.time())
            _atomic_write_user_json(uf, ud)
        logger.info(f"[push-token] {user_id}: {token[:18]}... (count={len(ud['fcm_tokens'])})")
        return {"success": True, "count": len(ud["fcm_tokens"])}
    except Exception as e:
        logger.error(f"[push-token] error: {e}")
        return {"success": False, "error": str(e)}


# ─────────────────────────────────────────────────────────────────────────
# S1: Auth API de usuario (token propio random, soft rollout)
# ─────────────────────────────────────────────────────────────────────────
@app.post("/api/cameras/rtsp/probe")
async def rtsp_probe(request: dict, authorization: str = Header(None, alias="Authorization")):
    """D2 — Probar una URL RTSP antes de registrar la cámara.
    Devuelve un frame JPEG en base64 para que el usuario CONFIRME qué ve
    la cámara antes de guardarla. Auth: bearer del usuario (user_id en body)."""
    user_id = (request.get("user_id") or "").strip()
    url = (request.get("rtsp_url") or "").strip()
    await _verify_user_token(authorization, user_id)
    from rtsp_puller import validate_rtsp_url, grab_one_frame, RtspUrlError
    try:
        url = validate_rtsp_url(url)
    except RtspUrlError as e:
        raise HTTPException(status_code=400, detail=f"URL rechazada: {e}")
    frame = await asyncio.to_thread(grab_one_frame, url, 15)
    if frame is None:
        raise HTTPException(status_code=502, detail="No se pudo conectar o no llegó video (revisa URL, usuario/clave y que el puerto esté abierto)")
    return {"success": True, "frame_b64": base64.b64encode(frame).decode(),
            "bytes": len(frame)}


@app.post("/api/cameras/rtsp/register")
async def rtsp_register(request: dict, authorization: str = Header(None, alias="Authorization")):
    """D2 — Registrar una cámara RTSP remota en la cuenta del usuario.
    Crea la entrada en user.json.cameras + camera.json con rtsp_url.
    El rtsp_puller (daemon) la descubre en el próximo scan (≤30s)."""
    user_id = (request.get("user_id") or "").strip()
    url = (request.get("rtsp_url") or "").strip()
    name = (request.get("name") or "Cámara IP").strip()[:60]
    zone = (request.get("zone") or "").strip()[:40]
    await _verify_user_token(authorization, user_id)
    # P0-billing: enforcement de plan al crear cámara
    uf = find_user_json(user_id)
    if uf and uf.exists():
        ud_plan = json.loads(uf.read_text())
        chk = _enforce_plan_on_create_camera(ud_plan)
        if not chk.get("allowed"):
            raise HTTPException(status_code=403, detail=chk.get("reason", "Plan no permite más cámaras"))
    from rtsp_puller import validate_rtsp_url, RtspUrlError
    try:
        url = validate_rtsp_url(url)
    except RtspUrlError as e:
        raise HTTPException(status_code=400, detail=f"URL rechazada: {e}")

    camera_id = f"IPCAM-{secrets.token_hex(4).upper()}"
    ingest_key = secrets.token_urlsafe(32)

    # Entrada en user.json (seguro: lock + atómico — C1)
    def _mut(ud):
        cams = ud.setdefault("cameras", [])
        cams.append({
            "camera_id": camera_id, "name": name, "zone": zone,
            "type": "rtsp", "created_at": int(time.time()),
        })
    update_user_json(user_id, _mut)

    # camera.json con la config del puller
    cam_dir = Path(STORAGE_ROOT) / "users" / user_id / "cameras" / camera_id
    cam_dir.mkdir(parents=True, exist_ok=True)
    cam_cfg = {
        "camera_id": camera_id, "type": "rtsp", "rtsp_url": url,
        "ingest_key": ingest_key, "zone": zone, "name": name,
        "fps": 1, "enabled": True, "rtsp_enabled": True,
        "created_at": time.time(),
    }
    cj = cam_dir / "camera.json"
    cj.write_text(json.dumps(cam_cfg, indent=2, ensure_ascii=False))
    os.chmod(cj, 0o600)  # la URL trae credenciales: solo owner
    _invalidate_cam_cfg_cache(user_id, camera_id)  # F3
    logger.info(f"[D2] cámara RTSP registrada: {user_id[:6]}…/{camera_id} ({name}, zone={zone})")
    return {"success": True, "camera_id": camera_id, "ingest_key": ingest_key,
            "note": "El puller la descubre en ≤30s. Guarda ingest_key: sirve para auditar la fuente."}


@app.get("/api/cameras/{camera_id}/ingest-ips")
async def get_ingest_ips(camera_id: str, user_id: Optional[str] = None):
    """F1-bis: ver las IPs pineadas de una cámara + estado de la política."""
    _validate_safe_path(camera_id, "camera_id")
    cam_cfg = get_camera_config_static(user_id or "", camera_id) or {}
    return {
        "success": True, "camera_id": camera_id,
        "ingest_key_active": bool(cam_cfg.get("ingest_key")),
        "allowed_ips": cam_cfg.get("ingest_allowed_ips", []),
        "locked": bool(cam_cfg.get("ingest_ip_locked")),
    }


@app.post("/api/cameras/{camera_id}/ingest-ips")
async def manage_ingest_ips(camera_id: str, request: dict,
                             authorization: str = Header(None, alias="Authorization")):
    """F1-bis: gestionar IPs pineadas sin re-flashear firmware.
    Acciones: {action: "add"|"remove"|"reset"|"lock"|"unlock", ip: "..."}
    - add: aprueba una IP nueva (cuando el ISP del negocio cambió la IP)
    - remove: quita una IP de la lista
    - reset: borra la lista → la próxima IP que llegue queda pineada (TOFU)
    - lock/unlock: congela o abre la ventana TOFU
    Auth: bearer del usuario dueño (middleware valida user_id del body)."""
    user_id = (request.get("user_id") or "").strip()
    await _verify_user_token(authorization, user_id)
    _validate_safe_path(camera_id, "camera_id")
    cam_path = Path(STORAGE_ROOT) / "users" / user_id / "cameras" / camera_id / "camera.json"
    if not cam_path.exists():
        raise HTTPException(status_code=404, detail="camera.json no encontrado")
    action = (request.get("action") or "").strip()
    ip = (request.get("ip") or "").strip()

    def _mut(cfg):
        ips = cfg.get("ingest_allowed_ips") or []
        if action == "add":
            if not ip:
                raise HTTPException(status_code=400, detail="ip requerida")
            if ip not in ips:
                ips.append(ip)
            cfg["ingest_allowed_ips"] = ips[-3:]
        elif action == "remove":
            cfg["ingest_allowed_ips"] = [x for x in ips if x != ip]
        elif action == "reset":
            cfg["ingest_allowed_ips"] = []  # TOFU: próxima IP que llegue se pinea
        elif action == "lock":
            cfg["ingest_ip_locked"] = True
        elif action == "unlock":
            cfg["ingest_ip_locked"] = False
        else:
            raise HTTPException(status_code=400, detail="action inválido")

    # write atómico bajo lock de cámara (usa helper F1-bis)
    try:
        with open(cam_path) as f:
            cfg = json.load(f)
        _mut(cfg)
        tmp = cam_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(cfg, indent=2, ensure_ascii=False))
        tmp.replace(cam_path)
        _invalidate_cam_cfg_cache(user_id, camera_id)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"error: {e}")
    return {"success": True, "action": action, "allowed_ips": cfg.get("ingest_allowed_ips", [])}


@app.post("/devices/announce")
async def devices_announce(request: dict):
    """F1 — Registro del ESP32 en la red local del usuario.
    El firmware v9.2.2 lo llama al arrancar: guarda la IP LAN real
    (local_config_url), firmware, RSSI y uptime. Lo usan: el proxy de
    comandos (:81), el wizard de Eva y el escáner de red (v9.3.2).
    Auth implícita: camera_id debe existir; la IP pública queda sujeta al
    pinning F1-bis en el ingest (aqui solo registramos la IP LAN)."""
    camera_id = (request.get("camera_id") or request.get("firmware_id") or "").strip()
    if not camera_id:
        raise HTTPException(status_code=400, detail="camera_id requerido")
    _validate_safe_path(camera_id, "camera_id")
    # localizar dueño
    user_id = ""
    users_dir = STORAGE_ROOT / "users"
    if users_dir.is_dir():
        for user_dir in users_dir.iterdir():
            if (user_dir / "cameras" / camera_id / "camera.json").exists():
                user_id = user_dir.name
                break
    if not user_id:
        # Bug #2 (2026-09-04): antes el announce de una cámara desconocida se
        # ignoraba sin dejar rastro — el wizard de Eva solo podía decir
        # "esperando imagen..." sin saber si la cámara siquiera encendió.
        # Ahora se registra en la cola de cámaras nuevas (en memoria + .runtime,
        # NO en user.json/camera.json: la cámara aún no está reclamada) para
        # que el wizard muestre telemetría (rssi/IP) y resuelva el camera_id
        # real en vez de inventar un cam_<ts> sintético (Bug #1).
        from eva.pending_cameras import register_announce
        entry = register_announce(
            camera_id,
            rssi=request.get("rssi"),
            ip_lan=(request.get("ip") or "")[:45],
            firmware=(request.get("firmware") or "").strip(),
        )
        logger.info(f"[announce] cámara desconocida {camera_id} en cola de nuevas "
                    f"(rssi={entry.get('rssi')} lan_ip={entry.get('ip_lan')} "
                    f"fw={entry.get('firmware')})")
        return {"ok": True, "registered": False, "pending": True}

    # Bug #1 (2026-09-04): la cámara ya tiene dueño — si estaba en la cola de
    # nuevas (se registró por otro camino), sacarla.
    try:
        from eva.pending_cameras import claim as _claim_pending_cam
        if _claim_pending_cam(camera_id):
            logger.info(f"[announce] {camera_id} reclamada: sale de la cola de nuevas")
    except Exception as _e:
        logger.debug(f"[announce] pending_cameras claim skip: {_e}")

    def _mut(cfg):
        cfg["local_ip"] = (request.get("ip") or "")[:45]
        cfg["local_config_url"] = (request.get("local_config_url") or "")[:100]
        cfg["last_announce"] = time.time()
        cfg["rssi"] = request.get("rssi")
        cfg["uptime_s"] = request.get("uptime")
        fw = (request.get("firmware") or "").strip()
        if fw:
            cfg["firmware_version"] = fw
    try:
        cam_path = user_root(user_id) / "cameras" / camera_id / "camera.json"
        with open(cam_path) as f:
            cfg = json.load(f)
        _mut(cfg)
        tmp = cam_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(cfg, indent=2, ensure_ascii=False))
        tmp.replace(cam_path)
        _invalidate_cam_cfg_cache(user_id, camera_id)
        # fix (2026-09-04): el announce YA NO escribe last_frame — solo
        # last_announce. Pisar last_frame hacía que una cámara en ciclo
        # announce-only (OTA crash-loop, frames muertos por horas) figurara
        # "En vivo" en la app del usuario. El estado online real ahora
        # depende exclusivamente del ingest de frames.
        _update_camera_last_announce(user_id, camera_id, request.get("ip", ""))
    except Exception as e:
        logger.warning(f"[announce] persist error: {e}")
        return {"ok": True, "registered": False}
    logger.info(f"[announce] {camera_id} fw={request.get('firmware')} "
                f"lan_ip={request.get('ip')} rssi={request.get('rssi')}")
    return {"ok": True, "registered": True}


# ── F1: OTA — el firmware llama /ota/check/{id} cada ~30 min ─────────────────
# Contrato (v9.2.2 checkOTA): 200 con {"update": true, "size": N} dispara la
# descarga de /ota/firmware.bin; cualquier otra cosa = no update.
_OTA_STATE_FILE = STORAGE_ROOT / ".runtime" / "ota_state.json"


def _load_ota_state() -> dict:
    try:
        if _OTA_STATE_FILE.exists():
            return json.loads(_OTA_STATE_FILE.read_text())
    except Exception:
        pass
    return {"stable_version": "", "bin_path": "", "rollout": {}, "notes": ""}


def _save_ota_state(st: dict):
    try:
        _OTA_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _OTA_STATE_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(st, indent=2))
        tmp.replace(_OTA_STATE_FILE)
    except Exception as e:
        logger.warning(f"[ota] state save: {e}")


@app.get("/ota/check/{camera_id}")
async def ota_check(camera_id: str, request: Request = None):
    """F1 — Rollout gradual de firmware. Solo ofrece si la versión que corre
    la cámara (header X-Firmware) DIFIERE de la estable publicada. Sin esa
    comparación, la cámara en rollout se actualiza en bucle infinito
    (detectado en vivo con la lab camera 2026-09-01)."""
    _validate_safe_path(camera_id, "camera_id")
    st = _load_ota_state()
    version = st.get("stable_version") or ""
    rollout = st.get("rollout") or {}
    if not version or not rollout.get(camera_id):
        return {"update": False, "version": version or "none"}
    current = ""
    # Fuente de versión: 1) header del check; 2) telemetría guardada de frames
    # (más confiable — el header del checkOTA del v9.2.2 manda "v9.0" hardcodeado).
    # Si ambas difieren de la estable → ofrecer.
    if request is not None:
        current = (request.headers.get("x-firmware") or "").strip()
    if not current or current == "v9.0":
        # fallback: lo que reportó el último frame real (X-Firmware del ingest)
        try:
            users_dir = STORAGE_ROOT / "users"
            if users_dir.is_dir():
                for user_dir in users_dir.iterdir():
                    cj = user_dir / "cameras" / camera_id / "camera.json"
                    if cj.exists():
                        current = (json.loads(cj.read_text()).get("firmware_version") or "").strip()
                        break
        except Exception:
            pass
    if current == version:
        return {"update": False, "version": version, "current": current}
    bin_path = Path(st.get("bin_path") or "")
    if not bin_path.exists():
        logger.warning(f"[ota] bin estable no encontrado: {bin_path}")
        return {"update": False, "version": version, "error": "bin missing"}
    logger.info(f"[ota] update OFFERED a {camera_id}: {current or '?'} → {version} "
                f"({bin_path.stat().st_size} bytes)")
    return {"update": True, "version": version, "size": bin_path.stat().st_size,
            "url": "/ota/firmware.bin"}


@app.get("/ota/firmware.bin")
async def ota_firmware():
    """F1 — Sirve el bin estable. Abierto a la red del túnel como el resto
    del pipeline; las cámaras lo descargan tras ota/check (que ya validó
    pertenencia al rollout). El bin es público por diseño (no contiene
    secretos: ni WiFi ni claves van embebidas — todo se configura por NVS)."""
    st = _load_ota_state()
    bin_path = Path(st.get("bin_path") or "")
    if not st.get("stable_version") or not bin_path.exists():
        raise HTTPException(status_code=404, detail="sin bin estable publicado")
    return FileResponse(bin_path, media_type="application/octet-stream",
                        filename=f"firmware_{st['stable_version']}.bin")


# ── F-admin (2026-09-01): endpoints del tab Monitoreo del panel ───────────────
# El tab llamaba /admin/monitoring/* que NO existían (404 silencioso → tab roto).

def _scan_all_cameras_admin() -> list:
    """Inventario completo: camera.json + estado de user.json por cámara."""
    out = []
    users_dir = STORAGE_ROOT / "users"
    if not users_dir.is_dir():
        return out
    now = time.time()
    for user_dir in users_dir.is_dir() and users_dir.iterdir() or []:
        if not user_dir.is_dir():
            continue
        ud = {}
        try:
            uf = user_dir / "user.json"
            if uf.exists():
                ud = json.loads(uf.read_text())
        except Exception:
            ud = {}
        cams_meta = {c.get("camera_id"): c for c in ud.get("cameras", [])}
        cams_dir = user_dir / "cameras"
        if cams_dir.is_dir():
            for c_dir in cams_dir.iterdir():
                cj = c_dir / "camera.json"
                if not cj.exists():
                    continue
                try:
                    cfg = json.loads(cj.read_text())
                except Exception:
                    continue
                cam_id = cfg.get("camera_id", c_dir.name)
                meta = cams_meta.get(cam_id, {})
                # fix (2026-09-04) 'En vivo' falso: last_frame SOLO lo escribe
                # el ingest real de frames. El announce escribe last_announce
                # (separado). Antes el announce pisaba last_frame y una cámara
                # en ciclo announce-only (OTA crash-loop) se veía "online"
                # aunque no enviara frames desde hacía horas.
                last_frame = cfg.get("last_frame") or meta.get("last_frame") or 0
                last_announce = cfg.get("last_announce") or meta.get("last_announce") or 0
                online = (now - last_frame) < 180 if last_frame else False
                announce_ok = (now - last_announce) < 120 if last_announce else False
                out.append({
                    "camera_id": cam_id,
                    "user_id": user_dir.name,
                    "user_name": ud.get("name", ""),
                    "business_name": ud.get("business_name", ""),
                    "name": cfg.get("name", "") or cam_id,
                    "zone": cfg.get("zone", ""),
                    "type": cfg.get("type", "esp32"),
                    "online": online,
                    "announce_ok": announce_ok,  # WiFi vivo pero sin frames
                    "active": online,  # compat con el render actual
                    "last_frame": last_frame,
                    "frame_age_s": int(now - last_frame) if last_frame else None,
                    "latest_frame_url": f"/frames/{user_dir.name}/{cam_id}/latest.jpg",
                    "firmware_version": cfg.get("firmware_version", ""),
                    "local_ip": cfg.get("local_ip", ""),
                    "ingest_key": bool(cfg.get("ingest_key")),
                })
    return out


@app.get("/admin/monitoring/overview")
async def admin_monitoring_overview(authorization: str = Header(None, alias="Authorization")):
    """Tab Monitoreo: contadores globales + alertas de hoy."""
    _verify_admin(authorization)
    cams = _scan_all_cameras_admin()
    online = sum(1 for c in cams if c["online"])
    day_ago = time.time() - 86400
    alerts_today = 0
    for c in cams:
        edir = STORAGE_ROOT / "users" / c["user_id"] / "cameras" / c["camera_id"] / "events"
        if not edir.is_dir():
            continue
        for ef in edir.glob("evt_*.json"):
            try:
                if ef.stat().st_mtime < day_ago:
                    continue
                if json.loads(ef.read_text()).get("event_type") in ("violation", "attention", "vigilance"):
                    alerts_today += 1
            except Exception:
                continue
    return {"cameras_total": len(cams), "cameras_online": online,
            "cameras_offline": len(cams) - online, "alerts_today": alerts_today}


@app.get("/admin/monitoring/cameras")
async def admin_monitoring_cameras(limit: int = 100, authorization: str = Header(None, alias="Authorization")):
    """Tab Monitoreo: tarjetas de cámaras con miniatura, estado y métricas."""
    _verify_admin(authorization)
    cams = _scan_all_cameras_admin()
    # métricas por cámara desde el layout real users/{u}/cameras/{cam}/events
    day_ago = time.time() - 86400
    for c in cams[:limit]:
        m = {"today_alerts": 0, "total_events": 0}
        cdir = STORAGE_ROOT / "users" / c["user_id"] / "cameras" / c["camera_id"] / "events"
        if cdir.is_dir():
            try:
                files = list(cdir.glob("evt_*.json"))
                m["total_events"] = len(files)
                m["today_alerts"] = sum(1 for f in files if f.stat().st_mtime >= day_ago)
            except Exception:
                pass
        c["metrics"] = m
    return {"cameras": cams[:limit]}


@app.get("/admin/monitoring/cameras/{camera_id}")
async def admin_monitoring_camera_detail(camera_id: str, user_id: str = None,
                                        recent_frames: int = 12,
                                        recent_alerts: int = 12,
                                        authorization: str = Header(None, alias="Authorization")):
    """F-panel: detalle de cámara del tab Monitoreo (el modal estaba en 404
    desde siempre → 'undefined' en todos los campos). Devuelve estado vivo
    (usa latest_raw.jpg como latido real), datos, frames y alertas recientes."""
    _verify_admin(authorization)
    _validate_safe_path(camera_id, "camera_id")
    # localizar dueño si no viene
    if not user_id:
        for udir in (STORAGE_ROOT / "users").iterdir() if (STORAGE_ROOT / "users").is_dir() else []:
            if (udir / "cameras" / camera_id / "camera.json").exists():
                user_id = udir.name
                break
    if not user_id:
        raise HTTPException(status_code=404, detail="cámara no encontrada")
    base = user_root(user_id)
    cam_dir = base / "cameras" / camera_id
    if not cam_dir.exists():
        # buscar en discos alternos por si el usuario migró
        cfgd = get_disk_config()
        for disk in cfgd.get("disks", []):
            alt = Path(disk["mount"]) / disk.get("user_folder", "users") / user_id / "cameras" / camera_id
            if alt.exists():
                cam_dir = alt
                break
    cam_cfg = {}
    cj = cam_dir / "camera.json"
    if cj.exists():
        try:
            cam_cfg = json.loads(cj.read_text())
        except Exception:
            pass
    ud = {}
    uf = _compat_user_json_path(user_id)
    if uf:
        try:
            ud = json.loads(uf.read_text())
        except Exception:
            pass
    now = time.time()
    raw = cam_dir / "frames" / "latest_raw.jpg"
    frame_age = None
    last_frame = None
    if raw.exists():
        frame_age = int(now - raw.stat().st_mtime)
        last_frame = datetime.fromtimestamp(raw.stat().st_mtime).isoformat()
    last_announce = cam_cfg.get("last_announce") or 0
    offline_seconds = (now - max(raw.stat().st_mtime if raw.exists() else 0,
                                  last_announce)) if (raw.exists() or last_announce) else None
    # comandos pendientes (scan/reboot one-shot no consumidos)
    pending = any(c.get("scan_request") or c.get("reboot_request")
                  for c in ud.get("cameras", []) if c.get("camera_id") == camera_id)
    # frames recientes: los últimos evt_* con imagen
    recent = []
    edir = cam_dir / "events"
    if edir.is_dir():
        files = sorted(edir.glob("evt_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        for f in files[:recent_frames]:
            try:
                ev = json.loads(f.read_text())
                img = f.with_suffix(".jpg")
                recent.append({
                    "event_id": ev.get("event_id", f.stem),
                    "timestamp": ev.get("timestamp", int(f.stat().st_mtime)),
                    "event_type": ev.get("event_type", ""),
                    "image_url": (f"/frames/{user_id}/{camera_id}/events/{img.name}"
                                  if img.exists() else None),
                    "description": (ev.get("description") or "")[:120],
                })
            except Exception:
                continue
    # alertas recientes (solo attention/violation)
    alerts = [r for r in recent if r["event_type"] in ("attention", "violation")][:recent_alerts]
    return {
        "camera_id": camera_id,
        "name": cam_cfg.get("name") or camera_id,
        "user_id": user_id,
        "user_name": ud.get("name", ""),
        "business_name": ud.get("business_name", ""),
        "disk_mount": str(cam_dir.parents[1]),
        "type": cam_cfg.get("type", "esp32"),
        "firmware_version": cam_cfg.get("firmware_version", ""),
        "local_ip": cam_cfg.get("local_ip", ""),
        "active": frame_age is not None and frame_age < 180,
        "status": "online" if (frame_age is not None and frame_age < 180) else "offline",
        "last_frame": last_frame,
        "frame_age_s": frame_age,
        "announce_age_s": int(now - last_announce) if last_announce else None,
        "offline_seconds": int(offline_seconds) if offline_seconds else None,
        "pending_commands": pending,
        "latest_frame_url": f"/frames/{user_id}/{camera_id}/latest.jpg",
        "recent_frames": recent,
        "recent_alerts": alerts,
        "config": {
            "cooldown_min": cam_cfg.get("cooldown_min", 5),
            "rules": cam_cfg.get("vigilance", {}).get("attention_phrases", []),
            "rules_es": cam_cfg.get("vigilance", {}).get("attention_phrases", []),
            "vigilance_prompt": cam_cfg.get("system_prompt", ""),
            "yolo_triggers": cam_cfg.get("vigilance", {}).get("yolo_triggers", []),
        },
        "audit": {"events_total": len(list(edir.glob("evt_*.json"))) if edir.is_dir() else 0},
    }


@app.get("/admin/monitoring/alerts")
async def admin_monitoring_alerts(limit: int = 12, hours: int = 24, authorization: str = Header(None, alias="Authorization")):
    """Tab Monitoreo: últimas alertas con imagen y descripción."""
    _verify_admin(authorization)
    out = []
    cutoff = time.time() - hours * 3600
    for c in _scan_all_cameras_admin():
        edir = STORAGE_ROOT / "users" / c["user_id"] / "cameras" / c["camera_id"] / "events"
        if not edir.is_dir():
            continue
        for ef in edir.glob("evt_*.json"):
            try:
                if ef.stat().st_mtime < cutoff:
                    continue
                ev = json.loads(ef.read_text())
                if ev.get("event_type") not in ("violation", "attention", "vigilance"):
                    continue
                ev["_user_id"] = c["user_id"]
                ev["camera_name"] = c.get("name", "") or ev.get("camera_id", "")
                # imagen: el jpg hermano del evento
                img = ef.with_suffix(".jpg")
                if img.exists():
                    ev["image_url"] = ("/frames/" + c["user_id"] + "/" + c["camera_id"]
                                       + "/events/" + img.name)
                out.append(ev)
            except Exception:
                continue
    out.sort(key=lambda e: e.get("timestamp", 0) or e.get("ts", 0), reverse=True)
    return {"alerts": out[:limit]}


# ── F-admin: Backups del panel (Sistema → 📦 Backups) ─────────────────────────
_BACKUP_DIR = Path(os.environ.get("OJOIA_BACKUP_DIR", "/home/sam/storage/backups"))


@app.get("/admin/backups")
async def admin_backups_list(authorization: str = Header(None, alias="Authorization")):
    """Lista los backups .tar.gz existentes (nombre, tamaño, fecha)."""
    _verify_admin(authorization)
    _BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    out = []
    for f in sorted(_BACKUP_DIR.glob("*.tar.gz"), key=lambda p: p.stat().st_mtime, reverse=True):
        st = f.stat()
        out.append({"name": f.name, "size_mb": round(st.st_size / 1048576, 1),
                    "created_at": st.st_mtime})
    return {"backups": out, "dir": str(_BACKUP_DIR)}


@app.post("/admin/backups/create")
async def admin_backup_create(authorization: str = Header(None, alias="Authorization")):
    """Crea un backup tar.gz de user.json/camera.json/configs (sin frames/eventos
    — eso es lo que cambia; el resto es reproducible). Corre en thread pool."""
    _verify_admin(authorization)
    import tarfile
    ts = int(time.time())
    _BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    out = _BACKUP_DIR / f"ojoia_config_{ts}.tar.gz"

    def _make():
        users_dir = STORAGE_ROOT / "users"
        with tarfile.open(out, "w:gz") as tar:
            for uf in users_dir.glob("*/user.json"):
                tar.add(uf, arcname=str(uf.relative_to(STORAGE_ROOT)))
            for cj in users_dir.glob("*/cameras/*/camera.json"):
                tar.add(cj, arcname=str(cj.relative_to(STORAGE_ROOT)))
            admin_cfg = Path("/home/sam/storage/admin_config.json")
            if admin_cfg.exists():
                tar.add(admin_cfg, arcname="admin_config.json")
        return out.stat().st_size
    size = await asyncio.to_thread(_make)
    logger.info(f"[admin] backup creado {out.name} ({size} bytes)")
    return {"success": True, "backup": out.name, "size_bytes": size}


@app.get("/admin/system/analysis-interval")
async def admin_get_analysis_interval(authorization: str = Header(None, alias="Authorization")):
    """Slider del tab Sistema. Mapeado a la realidad v7: el 'intervalo de
    análisis' real es el grid (OJOIA_GRID_SIZE frames por análisis Qwen)."""
    _verify_admin(authorization)
    grid_size = int(os.getenv("OJOIA_GRID_SIZE", "16"))
    fps = 1  # fps estándar del ESP32
    return {"analysis_interval_s": grid_size * fps, "grid_size": grid_size,
            "note": "Intervalo real = tamaño de grid × fps de cámara"}


@app.post("/admin/system/analysis-interval")
async def admin_set_analysis_interval(request: dict, authorization: str = Header(None, alias="Authorization")):
    """Ajusta OJOIA_GRID_SIZE derivándolo del intervalo pedido (segundos @1fps).
    Requiere reinicio de la API para aplicarse (los workers leen el env al inicio)."""
    _verify_admin(authorization)
    val = float(request.get("analysis_interval_s") or 0)
    if not (4 <= val <= 120):
        raise HTTPException(status_code=400, detail="intervalo debe estar entre 4 y 120 s")
    grid_size = max(4, min(64, int(round(val))))
    # persistir en ojoia.env para que sobreviva reinicios
    env_path = Path("/opt/ojoia/config/ojoia.env")
    try:
        lines = env_path.read_text().splitlines()
        lines = [l for l in lines if not l.startswith("OJOIA_GRID_SIZE=")]
        lines.append(f"OJOIA_GRID_SIZE={grid_size}")
        env_path.write_text("\n".join(lines) + "\n")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"no pude escribir ojoia.env: {e}")
    return {"success": True, "grid_size": grid_size,
            "analysis_interval_s": grid_size,
            "note": "Aplicado en ojoia.env — surte efecto al reiniciar la API (o reiníciala desde el panel)"}


# ── F-admin: procesos + reinicio de servicios (tab Sistema) ───────────────────
@app.get("/admin/system/processes")
async def admin_system_processes(authorization: str = Header(None, alias="Authorization")):
    """Lista los procesos OjoIA (api, workers, modelos, puller) con estado."""
    _verify_admin(authorization)
    def _ps():
        import subprocess
        try:
            r = subprocess.run(
                ["ps", "-eo", "pid,etime,rss,args", "--sort", "-rss"],
                capture_output=True, text=True, timeout=5)
            procs = []
            for line in r.stdout.splitlines()[1:]:
                parts = line.split(None, 3)
                if len(parts) < 4:
                    continue
                args = parts[3]
                if any(k in args for k in ("api_eva.py", "ingest_server.py", "rtsp_puller.py",
                                            "yolo_server", "whisper", "vllm", "sglang",
                                            "llama-server", "megapanel", "portal.py")) \
                   and "grep" not in args:
                    procs.append({"pid": int(parts[0]), "uptime": parts[1],
                                  "rss_mb": round(int(parts[2]) / 1024, 0),
                                  "cmd": args[:90]})
            return procs
        except Exception as e:
            return [{"pid": 0, "uptime": "?", "rss_mb": 0, "cmd": f"error: {e}"}]
    procs = await asyncio.to_thread(_ps)
    return {"processes": procs}


def _restart_service(unit: str):
    """Reinicia un servicio systemd vía systemctl (requiere polkit/sudoers;
    si no hay permisos, cae a kill -TERM del proceso para Restart=always)."""
    import subprocess, signal
    try:
        r = subprocess.run(["systemctl", "restart", unit],
                           capture_output=True, text=True, timeout=30)
        if r.returncode == 0:
            return {"success": True, "method": "systemctl", "unit": unit}
    except Exception:
        pass
    # fallback: kill del proceso principal (systemd lo levanta de nuevo)
    try:
        import subprocess
        r = subprocess.run(["pgrep", "-f", unit.replace(".service", "")],
                           capture_output=True, text=True, timeout=5)
        pids = [int(x) for x in r.stdout.split()]
        for pid in pids[:1]:  # solo el principal
            os.kill(pid, signal.SIGTERM)
        return {"success": True, "method": "sigterm+systemd-restart", "unit": unit,
                "note": "señal enviada; systemd lo levanta (Restart=always)"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"no pude reiniciar {unit}: {e}")


@app.post("/admin/system/restart/backend")
async def admin_restart_backend(authorization: str = Header(None, alias="Authorization")):
    """Reinicia la API (api-eva). Nota: la respuesta puede no llegar si el
    restart es rápido — el panel la interpreta como éxito tras reconnect."""
    _verify_admin(authorization)
    logger.info("[admin] restart backend solicitado desde el panel")
    asyncio.get_event_loop().call_later(1.0, _restart_service, "api-eva")
    return {"success": True, "unit": "api-eva", "note": "reiniciando en 1s"}


@app.post("/admin/system/restart/tunnel")
async def admin_restart_tunnel(authorization: str = Header(None, alias="Authorization")):
    """Reinicia el túnel cloudflared."""
    _verify_admin(authorization)
    result = await asyncio.to_thread(_restart_service, "cloudflared")
    logger.info(f"[admin] restart tunnel: {result}")
    return result


@app.get("/admin/ota/status")
async def ota_status(authorization: str = Header(None, alias="Authorization")):
    """Panel admin: estado del bin publicado + versión de cada cámara."""
    _verify_admin(authorization)
    st = _load_ota_state()
    bin_path = Path(st.get("bin_path") or "")
    # inventario de cámaras con su firmware reportado (telemetría X-Firmware)
    cams = []
    users_dir = STORAGE_ROOT / "users"
    if users_dir.is_dir():
        for user_dir in users_dir.iterdir():
            cams_dir = user_dir / "cameras"
            if not cams_dir.is_dir():
                continue
            for c_dir in cams_dir.iterdir():
                cj = c_dir / "camera.json"
                if not cj.exists():
                    continue
                try:
                    cfg = json.loads(cj.read_text())
                except Exception:
                    continue
                cams.append({
                    "camera_id": cfg.get("camera_id", c_dir.name),
                    "user_id": user_dir.name,
                    "type": cfg.get("type", "esp32"),
                    "name": cfg.get("name", ""),
                    "firmware_version": cfg.get("firmware_version", ""),
                    "local_ip": cfg.get("local_ip", ""),
                    "rssi": cfg.get("rssi"),
                    "uptime_s": cfg.get("uptime_s"),
                    "last_frame": cfg.get("last_frame", 0),
                    "last_announce": cfg.get("last_announce", 0),
                    "stream_down": cfg.get("stream_down", False),
                    "ingest_key": bool(cfg.get("ingest_key")),
                    "in_rollout": bool((st.get("rollout") or {}).get(cfg.get("camera_id", c_dir.name))),
                })
    return {
        "published": {
            "version": st.get("stable_version", ""),
            "bin_path": st.get("bin_path", ""),
            "bin_size": bin_path.stat().st_size if bin_path.exists() else 0,
            "notes": st.get("notes", ""),
            "published_at": st.get("published_at", 0),
            "rollout": list((st.get("rollout") or {}).keys()),
        },
        "cameras": cams,
    }


@app.post("/admin/cameras/{camera_id}/reboot")
async def admin_camera_reboot(camera_id: str, authorization: str = Header(None, alias="Authorization")):
    """Panel admin: reiniciar una cámara ESP32 remotamente.
    Usa el config server local (:81) si la cámara está en la LAN conocida,
    o el mecanismo de polling: reboot_request=true → el firmware lo aplica
    en su próximo poll (≤30s) — implementado en firmware v9.3.2+."""
    _verify_admin(authorization)
    _validate_safe_path(camera_id, "camera_id")
    user_id = _resolve_user_id_from_camera(camera_id)
    if not user_id:
        raise HTTPException(status_code=404, detail="cámara no encontrada")
    def _mut(ud):
        for c in ud.get("cameras", []):
            if c.get("camera_id") == camera_id:
                c["reboot_request"] = True
    update_user_json(user_id, _mut)
    logger.info(f"[admin] reboot solicitado para {camera_id}")
    return {"success": True, "camera_id": camera_id,
            "note": "La cámara se reinicia en su próximo polling (≤30s)"}


@app.post("/admin/ota/publish")
async def ota_publish(request: dict, authorization: str = Header(None, alias="Authorization")):
    """F1 — Publicar bin estable + definir rollout (admin).
    Body: {bin_path: "...", version: "v9.3.2", rollout: ["OJO-D1C560"], notes: ""}
    Solo cámaras listadas en rollout reciben la actualización."""
    _verify_admin(authorization)
    bin_path = Path(request.get("bin_path") or "")
    version = (request.get("version") or "").strip()
    rollout = request.get("rollout") or []
    if not bin_path.is_absolute():
        bin_path = Path("/home/sam") / bin_path
    if not bin_path.exists() or bin_path.suffix != ".bin":
        raise HTTPException(status_code=400, detail="bin_path inválido")
    if not version.startswith("v"):
        raise HTTPException(status_code=400, detail="version debe empezar con v")
    if not isinstance(rollout, list):
        raise HTTPException(status_code=400, detail="rollout debe ser lista")
    st = {"stable_version": version, "bin_path": str(bin_path),
          "rollout": {c: True for c in rollout}, "notes": request.get("notes", ""),
          "published_at": time.time()}
    _save_ota_state(st)
    logger.info(f"[ota] publicado {version} → rollout {len(rollout)} cámara(s): {rollout}")
    return {"success": True, "version": version, "rollout": rollout,
            "size": bin_path.stat().st_size}


@app.post("/devices/scan-results")
async def devices_scan_results(request: dict):
    """F2 — Recibe los resultados del escáner SSDP del ESP32 (v9.3.2).
    El dispositivo escaneó SU red local (con consentimiento del usuario,
    ver F3/F4). Guarda la lista 48h y avisa al wizard de Eva si hay sesión
    esperando. Privacidad: solo IP/puerto/vendor/modelo de cámaras."""
    camera_id = (request.get("camera_id") or "").strip()
    devices = request.get("devices") or []
    if not camera_id:
        raise HTTPException(status_code=400, detail="camera_id requerido")
    _validate_safe_path(camera_id, "camera_id")
    # localizar dueño
    user_id = ""
    users_dir = STORAGE_ROOT / "users"
    if users_dir.is_dir():
        for user_dir in users_dir.iterdir():
            if (user_dir / "cameras" / camera_id / "camera.json").exists():
                user_id = user_dir.name
                break
    if not user_id:
        return {"ok": True, "saved": False, "reason": "cámara sin registrar"}
    # saneo
    clean = []
    for d in devices[:5]:
        if not isinstance(d, dict):
            continue
        clean.append({
            "ip": str(d.get("ip", ""))[:45],
            "port": int(d.get("port") or 80),
            "vendor": str(d.get("vendor", "other"))[:20],
            "model": str(d.get("model", ""))[:60],
        })
    result = {
        "camera_id": camera_id, "user_id": user_id,
        "found": len(clean), "devices": clean,
        "scanned_at": time.time(),
        "expires_at": time.time() + 48 * 3600,  # purge 48h
    }
    # guardar junto al escáner (la cámara que escaneó)
    try:
        cam_dir = user_root(user_id) / "cameras" / camera_id
        _write_json_atomic(cam_dir / "last_scan.json", result)
    except Exception as e:
        logger.warning(f"[scan] persist error: {e}")
        return {"ok": True, "saved": False}
    logger.info(f"[scan] {camera_id} (user {user_id[:6]}…) encontró "
                f"{len(clean)} cámara(s): {[d.get('vendor') for d in clean]}")
    return {"ok": True, "saved": True, "found": len(clean)}


def trigger_scan_for_camera(user_id: str, camera_id: str) -> bool:
    """F3 — llamada DIRECTA (sin HTTP) para Eva v2. Misma lógica que el
    endpoint REST pero para uso interno del proceso."""
    try:
        def _mut(ud):
            found = False
            for c in ud.get("cameras", []):
                if c.get("camera_id") == camera_id:
                    c["scan_request"] = True
                    found = True
            if not found:
                ud.setdefault("cameras", []).append({
                    "camera_id": camera_id, "name": camera_id, "type": "esp32",
                    "scan_request": True})
        update_user_json(user_id, _mut)
        logger.info(f"[F3] scan trigger interno: user {user_id[:6]}… cam {camera_id}")
        return True
    except Exception as e:
        logger.warning(f"[F3] trigger interno: {e}")
        return False


@app.post("/api/cameras/{camera_id}/scan/trigger")
async def api_scan_trigger(camera_id: str, request: dict,
                           authorization: str = Header(None, alias="Authorization")):
    """F3 — Eva pide a una cámara ESP32 que escanee su red (auth de usuario).
    Misma mecánica que /admin/scan/trigger pero para el dueño de la cámara."""
    user_id = (request.get("user_id") or "").strip()
    await _verify_user_token(authorization, user_id)
    _validate_safe_path(camera_id, "camera_id")

    def _mut(ud):
        found = False
        for c in ud.get("cameras", []):
            if c.get("camera_id") == camera_id:
                c["scan_request"] = True
                found = True
        if not found:
            # la cámara puede no estar en user.json aún — añadirla mínima
            ud.setdefault("cameras", []).append({
                "camera_id": camera_id, "name": camera_id, "type": "esp32",
                "scan_request": True})
    update_user_json(user_id, _mut)
    logger.info(f"[F3] scan trigger por Eva: user {user_id[:6]}… cam {camera_id}")
    return {"success": True, "camera_id": camera_id}


@app.post("/admin/scan/trigger")
async def admin_scan_trigger(request: dict, authorization: str = Header(None, alias="Authorization")):
    """F2 — Pedir a una cámara ESP32 que escanee su red local.
    Marca scan_request:true en su config; el firmware lo ve en su próximo
    polling (≤10s) y reporta a /devices/scan-results. Uso: wizard de Eva
    (F3) o admin."""
    _verify_admin(authorization)
    camera_id = (request.get("camera_id") or "").strip()
    if not camera_id:
        raise HTTPException(status_code=400, detail="camera_id requerido")
    _validate_safe_path(camera_id, "camera_id")
    # localizar dueño y marcar el flag en user.json (es lo que el firmware
    # lee en GET /camera/config/{id})
    user_id = ""
    users_dir = STORAGE_ROOT / "users"
    if users_dir.is_dir():
        for user_dir in users_dir.iterdir():
            uf = user_dir / "user.json"
            if uf.exists():
                try:
                    ud = json.loads(uf.read_text())
                    for c in ud.get("cameras", []):
                        if c.get("camera_id") == camera_id:
                            user_id = user_dir.name
                            break
                except Exception:
                    pass
                if user_id:
                    break
    if not user_id:
        raise HTTPException(status_code=404, detail="cámara no encontrada")

    def _mut(ud):
        for c in ud.get("cameras", []):
            if c.get("camera_id") == camera_id:
                c["scan_request"] = True
    update_user_json(user_id, _mut)  # C1: lock + atómico
    logger.info(f"[scan] trigger enviado a {camera_id}")
    return {"success": True, "camera_id": camera_id,
            "note": "El escaneo se ejecuta en ≤10s (próximo polling). "
                    "Resultados en camera.json/last_scan.json"}


# ── RD-4: resúmenes diarios para la app (aterrizaje del push) ────────────────
@app.get("/api/user/summaries")
async def get_user_summaries(user_id: str, limit: int = 14,
                             authorization: str = Header(None)):
    """Historial de resúmenes diarios (para la vista de la app y el
    aterrizaje del push del reporte matutino)."""
    await _verify_user_token(authorization, user_id)
    base = user_root(user_id)
    out = []
    for sdir in (base / "summaries", STORAGE_ROOT / "users" / user_id / "summaries"):
        if not sdir.is_dir():
            continue
        for f in sorted(sdir.glob("daily_*.json"), reverse=True)[:limit]:
            try:
                out.append(json.loads(f.read_text()))
            except Exception:
                continue
        break
    out.sort(key=lambda s: s.get("date", ""), reverse=True)
    return {"summaries": out[:limit]}


@app.get("/api/user/summaries/{date}")
async def get_user_summary_date(date: str, user_id: str,
                                authorization: str = Header(None)):
    """Un resumen concreto por fecha (deep-link del push: data.date)."""
    await _verify_user_token(authorization, user_id)
    _validate_safe_path(date, "date")
    base = user_root(user_id)
    for sdir in (base / "summaries", STORAGE_ROOT / "users" / user_id / "summaries"):
        f = sdir / f"daily_{date}.json"
        if f.exists():
            return json.loads(f.read_text())
    raise HTTPException(status_code=404, detail="Sin resumen para esa fecha")


@app.post("/api/auth/token")
async def issue_user_token(request: dict):
    """
    Emite un Bearer OjoIA para el usuario identificado por user_id.

    P0 (Bug #5): AHORA REQUIERE firebase_token (o un Authorization: Bearer
    con el Firebase ID Token) que al ser verificado matchee el user_id del
    body. Antes se emitía a cualquier user_id con solo nombrarlo — privilege
    escalation completo. Ver PLAN_CONSOLIDADO_P0.

    Body:
        user_id: str (requerido) — Firebase UID del usuario
        firebase_token: str (requerido) — Firebase ID Token a verificar
        device: str (opcional) — info de tracking
    Devuelve:
        token: str
        expires_at: int (epoch)
    """
    user_id = (request.get("user_id") or "").strip()
    device = request.get("device")
    firebase_token = (request.get("firebase_token") or "").strip()
    if not user_id:
        raise HTTPException(status_code=400, detail="user_id requerido")
    if not firebase_token:
        raise HTTPException(status_code=401, detail="firebase_token requerido para emitir API token")
    # Verificar Firebase ID Token en thread aparte (I/O bloqueante a Google)
    try:
        decoded = await asyncio.to_thread(auth.verify_id_token, firebase_token)
    except Exception as e:
        logger.warning(f"[auth] firebase_token inválido al emitir token: {e}")
        raise HTTPException(status_code=401, detail=f"Firebase token inválido: {e}")
    firebase_uid = decoded.get("uid", "")
    if firebase_uid != user_id:
        logger.warning(f"[auth] user_id mismatch: pidió {user_id} pero token es de {firebase_uid}")
        raise HTTPException(status_code=403, detail="user_id no corresponde al Firebase token")
    uf = find_user_json(user_id)
    if not uf or not uf.exists():
        raise HTTPException(status_code=404, detail="Usuario no encontrado")
    token = _generate_user_token()
    token_id = secrets.token_hex(8)
    now = int(time.time())
    expires_at = now + AUTH_TOKEN_TTL_SEC
    with _get_user_lock(user_id):
        with open(uf) as f:
            ud = json.load(f)
        tokens = ud.get("api_tokens", []) or []
        # Max 5 tokens activos por usuario; revocar el mas viejo si se excede
        active = [t for t in tokens if not t.get("revoked") and now < t.get("expires_at", 0)]
        if len(active) >= 5:
            active.sort(key=lambda t: t.get("created_at", 0))
            oldest = active[0]
            oldest["revoked"] = True
        tokens.append({
            "id": token_id,
            "token": token,
            "device": device,
            "created_at": now,
            "expires_at": expires_at,
            "last_used": None,
            "revoked": False
        })
        # Limpiar tokens revocados/expirados viejos (mantener ultimos 20)
        kept = [t for t in tokens if (not t.get("revoked") and
                now < t.get("expires_at", 0))] + \
               [t for t in tokens if (t.get("revoked") or
                now >= t.get("expires_at", 0))][-20:]
        ud["api_tokens"] = kept
        _atomic_write_user_json(uf, ud)
    logger.info(f"[auth] token issued for user={user_id} id={token_id}")
    return {"success": True, "token": token, "token_id": token_id, "expires_at": expires_at}


@app.delete("/api/auth/token")
async def revoke_user_token(user_id: str, token_to_revoke: str):
    """Revoca un Bearer token (logout de dispositivo)."""
    user_id = (user_id or "").strip()
    if not user_id or not token_to_revoke:
        raise HTTPException(status_code=400, detail="user_id y token requeridos")
    uf = find_user_json(user_id)
    if not uf or not uf.exists():
        raise HTTPException(status_code=404, detail="Usuario no encontrado")
    with _get_user_lock(user_id):
        with open(uf) as f:
            ud = json.load(f)
        tokens = ud.get("api_tokens", []) or []
        revoked = 0
        for t in tokens:
            if t.get("token") == token_to_revoke and not t.get("revoked"):
                t["revoked"] = True
                t["revoked_at"] = int(time.time())
                revoked += 1
        if revoked:
            ud["api_tokens"] = tokens
            _atomic_write_user_json(uf, ud)
    logger.info(f"[auth] token revoked for user={user_id} count={revoked}")
    return {"success": True, "revoked": revoked}


@app.get("/api/auth/tokens")
async def list_user_tokens(user_id: str):
    """Lista tokens activos del usuario (sin valor del token, solo metadatos)."""
    user_id = (user_id or "").strip()
    if not user_id:
        raise HTTPException(status_code=400, detail="user_id requerido")
    uf = find_user_json(user_id)
    if not uf or not uf.exists():
        raise HTTPException(status_code=404, detail="Usuario no encontrado")
    with _get_user_lock(user_id):
        with open(uf) as f:
            ud = json.load(f)
        tokens = ud.get("api_tokens", []) or []
        now = int(time.time())
        active = [t for t in tokens if not t.get("revoked") and now < t.get("expires_at", 0)]
    return {
        "success": True,
        "active_count": len(active),
        "tokens": [{
            "id": t.get("id"),
            "device": t.get("device"),
            "created_at": t.get("created_at"),
            "expires_at": t.get("expires_at"),
            "last_used": t.get("last_used"),
            "revoked": t.get("revoked", False)
        } for t in active]
    }


@app.delete("/api/users/push-token")
async def unregister_push_token(user_id: str, token: str):
    """Baja token (logout o NotRegistered)."""
    try:
        uf = user_root(user_id) / "user.json"
        if not uf.exists():
            return {"success": False, "error": "usuario no encontrado"}
        # S4: lock durante read+mutate+write
        with _get_user_lock(user_id):
            with open(uf) as f:
                ud = json.load(f)
            before = len(ud.get("fcm_tokens", []) or [])
            ud["fcm_tokens"] = [t for t in (ud.get("fcm_tokens") or []) if (t or "").strip() != token.strip()]
            devs = ud.get("fcm_devices") or {}
            if token in devs:
                del devs[token]
                ud["fcm_devices"] = devs
            _atomic_write_user_json(uf, ud)
        logger.info(f"[push-token] unregister {user_id}: {before}->{len(ud['fcm_tokens'])}")
        return {"success": True, "remaining": len(ud["fcm_tokens"])}
    except Exception as e:
        return {"success": False, "error": str(e)}


# ═══════════════════════════════════════════════════════════════════════════
# REPORTES — envío consolidado, página HTML y PDF
# ═══════════════════════════════════════════════════════════════════════════

@app.post("/api/reportes/send-v2")
async def send_report_v2(user_id: str, request: Request = None):
    """Envío completo: HTML + PDF + chat + push. URLs reales."""
    try:
        if not user_id:
            return {"success": False, "error": "user_id required"}
        body = {}
        try:
            if request and request.headers.get("content-type", "").startswith("application/json"):
                body = await request.json()
        except Exception:
            body = {}
        camera_id = body.get("camera_id")
        date = body.get("date", "yesterday")
        cutoff_hour = body.get("cutoff_hour")  # None = día completo (00:00–23:59)

        # 1) generar página HTML + PDF (URLs api.ojoia.com.do)
        from reportes.page_generator import generate_report_page
        page = await generate_report_page(user_id, date, camera_id, cutoff_hour=cutoff_hour)
        if not page.get("success"):
            return page
        # B4: defensivo — page generator podria retornar success=True sin
        # completar html_url/pdf_url en paths de error parciales.
        html_url = page.get("html_url", "")
        pdf_url = page.get("pdf_url", "")
        if not html_url or not pdf_url:
            return {"success": False, "error": "Reporte generado sin URLs", "page": page}
        report = page.get("report", {})
        summary = report.get("summary", {})
        date_str = page.get("date_str", date)
        biz = report.get("business_name", "Tu negocio")

        # 2) mensaje optimizado para el chat
        message = (
            f"🍽️ *Reporte Diario - {biz}*\n\n"
            f"📅 {date_str}\n"
            f"📊 {summary.get('total_events', 0)} análisis\n"
            f"👥 {summary.get('persons_total', 0)} personas únicas\n\n"
            f"[📊 Ver reporte web]({html_url})\n"
            f"[📥 Descargar PDF]({pdf_url})\n\n"
            f"_Generado automáticamente a las 7:30 AM_"
        )

        # 3) inyectar en la sesión unificada "chat_<uid>" (memoria + user.json)
        # UN SOLO CHAT: antes inyectaba en cualquier _session del usuario, que
        # podía no ser la sesión del chat del frontend. Ahora se inyecta en
        # "chat_<uid>" consistentemente en memoria Y persistido en user.json,
        # con merge anti-dup por (role, content, timestamp) para no duplicar el
        # reporte en sucesivos envíos del día.
        try:
            from eva_v2 import _sessions
            unified_sid = _eva_unified_sid(user_id)
            sess_mem = _sessions.get(unified_sid)
            if not sess_mem:
                sess_mem = {"user_id": user_id, "msgs": [], "messages": [], "last_activity": time.time()}
                _sessions[unified_sid] = sess_mem
            msg_event_ts = time.time()
            daily_msg = {"role": "assistant", "content": message, "timestamp": msg_event_ts,
                         "summary": True, "is_daily_report": True, "report_url": html_url}
            # Anti-dup en memoria: si ya hay un reporte con el mismo html_url, no añadir.
            already_mem = any(m.get("report_url") == html_url for m in sess_mem.get("messages", []))
            if not already_mem:
                sess_mem.setdefault("msgs", []).append(daily_msg)
                sess_mem.setdefault("messages", []).append({"role": "assistant", "content": message,
                                                            "timestamp": msg_event_ts})
        except Exception as e:
            logger.warning(f"[reportes] inject chat session: {e}")

        # 4) persistir en user.json["eva_sessions"]["chat_<uid>"] (sesión unificada)
        try:
            uf = find_user_json(user_id)
            if uf and uf.exists():
                with _get_user_lock(user_id):
                    with open(uf) as f:
                        ud = json.load(f)
                    try:
                        _consolidate_legacy_eva_sessions(ud, user_id)
                    except Exception as e_925:
                        # P0 (Sección #8): loggeo en lugar de pass silencioso
                        logger.warning(f"[legacy-eva-sessions] consolidate failed for {user_id}: {e_925}")
                    sessions = ud.get("eva_sessions", {}) or {}
                    usess = sessions.get(unified_sid, {}) or {}
                    umsgs = list(usess.get("messages", []) or [])
                    # Anti-dup por report_url (mismo reporte reenviado no se duplica)
                    if not any(m.get("report_url") == html_url for m in umsgs):
                        umsgs.append(daily_msg)
                        umsgs.sort(key=lambda m: int(m.get("timestamp", 0) or 0))
                        umsgs = umsgs[-200:]
                        sessions[unified_sid] = {
                            "messages": umsgs,
                            "summary": usess.get("summary", ""),
                            "created_at": usess.get("created_at", int(time.time())),
                            "last_message_at": max(int(m.get("timestamp", 0) or 0) for m in umsgs) if umsgs else int(time.time()),
                        }
                        ud["eva_sessions"] = sessions
                        _atomic_write_user_json(uf, ud)
        except Exception as e:
            logger.warning(f"[reportes] write user.json eva_sessions: {e}")

        # 4b) guardar también en eva_chat_history.json (legacy, sin uso por frontend)
        try:
            hf = user_root(user_id) / "eva_chat_history.json"
            hdata = {"history": [], "summary": ""} if not hf.exists() else json.loads(hf.read_text())
            if not any(m.get("report_url") == html_url for m in hdata.get("history", [])):
                hdata["history"].append({"role": "assistant", "content": message, "timestamp": time.time(), "summary": True, "is_daily_report": True, "report_url": html_url})
                hf.write_text(json.dumps(hdata, indent=2, ensure_ascii=False))
        except Exception as e:
            logger.warning(f"[reportes] write chat_history: {e}")

        # 5) push FCM apuntando a la página real
        push_sent = False
        push_ms = 0
        try:
            from google.oauth2 import service_account
            import google.auth.transport.requests
            import requests as _req
            uf = user_root(user_id) / "user.json"
            if uf.exists():
                ud = json.loads(uf.read_text())
                tokens = ud.get("fcm_tokens", []) or []
                if tokens:
                    creds = service_account.Credentials.from_service_account_file(
                        os.environ.get("FIREBASE_KEY_PATH", "/opt/ojoia/config/firebase-key.json"),
                        scopes=["https://www.googleapis.com/auth/firebase.messaging"]
                    )
                    creds.refresh(google.auth.transport.requests.Request())
                    t0 = time.time()
                    for tok in tokens:
                        try:
                            # B3: requests.post bloquea el event loop (I/O sincrono).
                            # Offload al thread pool con asyncio.to_thread + lambda
                            # para preservar kwargs (to_thread solo acepta *args).
                            await asyncio.to_thread(
                                lambda: _req.post(
                                    "https://fcm.googleapis.com/v1/projects/ojoia-67216/messages:send",
                                    headers={"Authorization": f"Bearer {creds.token}", "Content-Type": "application/json"},
                                    json={"message": {
                                        "token": tok,
                                        "notification": {"title": "📊 Reporte Diario", "body": f"Tu reporte de {biz} está listo", "click_action": html_url},
                                        "data": {"type": "daily_report", "url": html_url, "title": "📊 Reporte Diario", "body": f"Tu reporte de {biz} está listo", "tag": "daily_report"},
                                        "webpush": {"notification": {"title": "📊 Reporte Diario", "body": f"Tu reporte de {biz} está listo", "icon": "/img/icon-192.png", "tag": "daily_report"}, "fcm_options": {"link": html_url}},
                                        "android": {"priority": "high", "ttl": "15s", "notification": {"channel_id": "daily_reports", "click_action": html_url}}
                                    }},
                                    timeout=10
                                )
                            )
                        except Exception as e:
                            logger.warning(f"[reportes] fcm token err: {e}")
                    push_ms = int((time.time() - t0) * 1000)
                    push_sent = True
        except Exception as e:
            logger.warning(f"[reportes] push section: {e}")
        return {
            "success": True,
            "chat_injected": True,
            "push_sent": push_sent,
            "push_delivery_time_ms": push_ms,
            "html_url": html_url,
            "pdf_url": pdf_url,
            "message": message,
            "business_name": biz,
            "timing": {"total_ms": push_ms}
        }
    except Exception as e:
        logger.error(f"[reportes] send-v2: {e}")
        return {"success": False, "error": str(e)}


# Servir páginas estáticas: /reportes/{user}/{filename}
@app.get("/reportes/{user_id}/{filename}")
async def serve_report_file(user_id: str, filename: str):
    from fastapi.responses import FileResponse, HTMLResponse, Response
    base = STORAGE_ROOT / "report_pages" / user_id
    fp = (base / filename).resolve()
    # A5: path traversal — antes fp = base / filename sin validar; un request
    # con filename="../../etc/passwd" escapaba del base. Ahora exigimos que el
    # path resuelto SIGA dentro de base. ademas rechazamos ".." y slashes.
    if not fp.is_relative_to(base.resolve()):
        logger.warning(f"[traversal] rechazo reportes: user_id={user_id} filename={filename!r}")
        return HTMLResponse(content="<h1>403 Forbidden</h1>", status_code=403)
    if not fp.exists():
        return HTMLResponse(content=f"<h1>404</h1><p>{filename} no encontrado. Genera con /api/reportes/send-v2</p>", status_code=404)
    if filename.lower().endswith(".pdf"):
        return FileResponse(str(fp), media_type="application/pdf", filename=filename)
    return FileResponse(str(fp), media_type="text/html")


# Servir imágenes de alerta vigilante: /vigilance-frame/{user_id}/{event_id}.jpg
@app.get("/vigilance-frame/{user_id}/{event_id}")
async def serve_vigilance_frame(user_id: str, event_id: str):
    """Sirve el frame JPG de una alerta de vigilancia/centinela (para push con imagen)."""
    from fastapi.responses import FileResponse, HTMLResponse, Response
    # A5: path traversal — user_id y event_id vienen del path y se interpolan
    # en rutas fs. Sin validar, event_id="../../etc/passwd" escapaba. Validar.
    _validate_safe_path(user_id, "user_id")
    _validate_safe_path(event_id, "event_id")
    cam_dir = user_root(user_id) / "cameras"
    # Buscar el event_id.jpg en cualquier carpeta de cámara
    for cam_sub in cam_dir.iterdir() if cam_dir.exists() else []:
        cand = (cam_sub / "events" / f"{event_id}.jpg").resolve()
        # A5: confirmar que el candidato resuelto sigue dentro de cam_sub/events
        if not cand.is_relative_to((cam_sub / "events").resolve()):
            continue
        if cand.exists():
            return FileResponse(str(cand), media_type="image/jpeg",
                                headers={"Cache-Control": "private, max-age=600"})
    return HTMLResponse(content="404 frame no encontrado", status_code=404)


# CORS middleware — A3: lista de origenes explicita (no wildcard+credentials,
# que es invalido por spec y los browsers rechazan). Antes:
#   allow_origins=["*"] + allow_credentials=True  -> rechazado por spec.
# Origenes legitimos: el SPA publico (ojoia.com.do), el panel admin
# (admin.ojoia.com.do) y localhost para desarrollo. El API mismo recibe
# peticiones via api.ojoia.com.do (algunas con Origin = propio dominio).
# Los bearer tokens van en header Authorization (no en cookies), asi que
# allow_credentials=False es seguro y permitido por la spec.
ALLOWED_ORIGINS_ENV = os.environ.get("OJOIA_ALLOWED_ORIGINS", "")
ALLOWED_ORIGINS = [
    "https://ojoia.com.do", "https://www.ojoia.com.do",
    "https://admin.ojoia.com.do", "https://api.ojoia.com.do",
    # localhost para dev (cualquier puerto) -> los listamos explicitos
    "http://localhost", "http://127.0.0.1",
]
# si el operador configura orígenes extra por env (CSV), anadirlos
for _o in ALLOWED_ORIGINS_ENV.split(","):
    _o = _o.strip()
    if _o and _o not in ALLOWED_ORIGINS:
        ALLOWED_ORIGINS.append(_o)


app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=False,  # bearer en header, no cookies
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    # [Fix CORS] El SPA admin envia SIEMPRE el header 'ngrok-skip-browser-warning'
    # (desde HDRS) en cada request. El navegador lo incluye en
    # Access-Control-Request-Headers del preflight; al no estar en esta lista el
    # backend responde 400 y el navegador bloquea TODAS las llamadas (dashboard
    # "Offline"). Se agrega para que el preflight sea 200.
    allow_headers=["Authorization", "Content-Type", "X-Requested-With", "Accept", "Origin", "User-Agent", "DNT", "Cache-Control", "Keep-Alive", "X-Api-Key", "Pragma", "ngrok-skip-browser-warning", "Accept-Language"],
    expose_headers=["*"],
    max_age=86400,
)

# Static files for event images
app.mount("/events", StaticFiles(directory=str(STORAGE_ROOT / "users")), name="events-static")

# A5: helper anti-path-traversal. Rechaza user_id/date/path segments que
# contengan "..", "/", "\" o null bytes. Pensado para path params que se
# interpolan en rutas del filesystem. No reemplaza is_relative_to() (que se
# usa donde se construye fp completo), sino que filtra inputs temprano.
def _validate_safe_path(value: str, name: str = "param") -> str:
    if not value or not isinstance(value, str):
        raise HTTPException(status_code=400, detail=f"{name} requerido")
    # rechazar componentes peligrosos
    if "\\" in value or "/" in value or ".." in value or "\x00" in value:
        raise HTTPException(status_code=400, detail=f"{name} invalido")
    return value


# A1 — Middleware de auth de usuario (cierre de seguridad central).
# Aplica a TODAS las rutas /api/* que tengan user_id (query/form/path/body).
# Cierra el bug raiz: antes cualquier cliente podia adivinar un user_id y leer
# sus eventos/frames/stream. Ahora se exige Authorization: Bearer <token>
# valido PARA ESE user_id (validado contra user.json.api_tokens[] con
# hmac.compare_digest). Si AUTH_ENFORCE=True (ahora si) -> 401.
#
# Rutas PUBLICAS (no pasan por este check - lista explicita):
PUBLIC_USER_PATHS = {
    "/health", "/api/support-info", "/api/zone-types",
    "/api/push-config",  # RD-5: vapidKey es pública por diseño (como apiKey del frontend)
    # token generation/verify: el primero crea el token, el segundo usa Firebase
    "/api/auth/token",  # P0 (Bug #5): ahora valida firebase_token internamente vs user_id
    "/auth/firebase/verify",  # Firebase hace su propia verificacion
    "/api/support-info",
    # /ingest/* y /frames/ingest: las camaras ESP32 no portan Bearer (doc linea 178)
    "/ingest/frame", "/ingest/photo", "/ingest/snapshot", "/ingest/raw",
    "/frames/ingest",
    # /cameras/{id}/cmd: proxy al ESP32, no porta user_id confiable
    # (se filtra por camera_id que si conocido)
    # reportes servidos por path-traversal-sanitized (A5): publicos para que el
    # link push llegue al usuario sin login (token en URL seria loggable)
    # /api/reportes/view, /api/reportes/download, /api/reportes/url -> user_id en path
    "/api/reportes/view", "/api/reportes/download", "/api/reportes/url",
    # /api/reportes/send-v2: lo invoca el cron interno (dispatch_morning_reports.sh)
    # sin header Authorization. El cron corre en el host (no expuesto). Cuando
    # se necesita auth real, el cron deberá portar un token de servicio.
    "/api/reportes/send-v2",
    # /api/reports/send-daily: igual, cron interno
    "/api/reports/send-daily",
    # vigilance-frame: link de imagen en notificacion push (publico, sin login)
    # /vigilance-frame/{user_id}/{event_id} -> se filtra por path traversal en A5
    # /admin/* usa _verify_admin propio (no entra aqui porque no empieza con /api/)
}


def _extract_user_id_from_request(request: Request) -> Optional[str]:
    """Extrae user_id de query, form o path params. Devuelve None si no llega."""
    uid = request.query_params.get("user_id") or request.query_params.get("uid")
    if uid:
        return uid
    path_params = getattr(request, "path_params", {}) or {}
    uid = path_params.get("user_id") or path_params.get("uid")
    if uid:
        return uid
    return None  # body no se lee aqui (async); el route ya tiene su propio user_id


@app.middleware("http")
async def enforce_user_auth(request: Request, call_next):
    """
    A1 — Valida Authorization: Bearer <token> para /api/* que lleven user_id.
    Rutas en PUBLIC_USER_PATHS o sin user_id pasan sin check.
    OPTIONS (preflight) pasa siempre.

    CORS FIX: cuando este middleware corta con 401/403, agrega
    Access-Control-Allow-Origin a la respuesta. Razon: el navegador hace
    fetch() con header Origin y, si el backend responde 401 SIN ACAO, el
    navegador bloquea la lectura del response (CORS Missing Allow Origin) y
    el SPA queda "Offline" aunque el server este vivo. El 401 sigue siendo
    401 (no abre ningun hueco); solo permitimos que el navegador LO LEA.
    Usamos el Origin de la request si viene, sino "*" (allow_credentials=False
    asi "*" es valido por la spec).
    """
    path = request.url.path
    # solo /api/* (excepto las publicas explicitas o sus prefijos con path params).
    # /admin/* usa _verify_admin propio.
    # PUBLIC_USER_PATHS admite entradas con barra final como PREFIJO (startswith).
    # A2 fix (2026-08-09): /api/event-thumb/ y /api/thumb/ como PREFIJOS publicos.
    # Los thumbs se cargan como <img src="..."> desde el SPA y NO pueden portar
    # header Authorization. Antes el middleware A1 (AUTH_ENFORCE=True) los cortaba
    # con 401 JSON -> Firefox dispara OpaqueResponseBlocking (ORB) al ver una
    # respuesta no-imagen en un tag <img> -> thumbs no cargaban y el feed de
    # eventos se veia "vacio". Mismo criterio que /vigilance-frame/* (publicas
    # porque solo exponen miniaturas de eventos de seguridad, sin PII).
    public_prefixes = ("/api/reportes/view/", "/api/reportes/download/",
                       "/api/reportes/url/", "/reportes/",
                       "/api/event-thumb/", "/api/thumb/")
    if (not path.startswith("/api/")
            or path in PUBLIC_USER_PATHS
            or path.startswith(public_prefixes)
             or (path.startswith("/api/events/") and "/frame/" in path)):
        return await call_next(request)
    if request.method == "OPTIONS":
        return await call_next(request)
    user_id = _extract_user_id_from_request(request)
    if not user_id:
        # el endpoint no expone user_id por query/path; confiamos en que el
        # route body hara await _verify_user_token(authorization, user_id) explicito,
        # o el user_id viene en JSON body (el route la extrae y valida).
        # Mientras tanto, si no hay Authorization y enforce=True -> 401 para
        # forzar a que los endpoints con user_id en body validen explicito.
        if not request.headers.get("authorization"):
            resp = JSONResponse({"detail": "Authorization requerido"}, status_code=401)
            _add_cors_to_response(request, resp)
            return resp
        return await call_next(request)
    try:
        await _verify_user_token(request.headers.get("authorization"), user_id)
    except HTTPException as he:
        resp = JSONResponse({"detail": he.detail}, status_code=he.status_code)
        _add_cors_to_response(request, resp)
        return resp
    return await call_next(request)


def _add_cors_to_response(request: Request, response):
    """Agrega Access-Control-Allow-Origin a una respuesta generada por un
    middleware (no pasa por CORSMiddleware de FastAPI en ese path). Si la
    request trajo Origin, lo reflejamos; sino usamos '*' (valido porque
    allow_credentials=False). No altera el status ni el body."""
    origin = request.headers.get("origin")
    if origin:
        response.headers["Access-Control-Allow-Origin"] = origin
    else:
        response.headers["Access-Control-Allow-Origin"] = "*"
    # Permitir que el navegador lea el cuerpo del 401 (necesario para fetch).
    response.headers["Access-Control-Allow-Credentials"] = "false"


# Middleware para no-cache y CORS seguro
# v9.2: NO pisar los headers CORS que ya pone CORSMiddleware de FastAPI.
# Antes este middleware devolvía Access-Control-Allow-Headers: "*" en
# OPTIONS, lo cual Firefox/Chrome rechazan cuando la petición lleva
# Authorization (el navegador exige que Authorization esté listado
# explícitamente). Ahora dejamos que CORSMiddleware maneje TODO el
# flujo CORS (ya lo hace bien en línea 856-864 listando Authorization)
# y aquí solo agregamos headers de seguridad/caching complementarios.
@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    try:
        if request.method == "OPTIONS":
            response = await call_next(request)
        else:
            response = await call_next(request)
        # Headers complementarios (no tocan Allow-Origin/Headers/Methods
        # que ya maneja CORSMiddleware).
        response.headers["Cross-Origin-Resource-Policy"] = "cross-origin"
        if request.method != "OPTIONS":
            response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
        return response
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        logger.error(f"Middleware error: {type(e).__name__}: {e}")
        logger.error(f"Traceback: {tb}")
        return Response(
            status_code=500,
            content=f"Error: {type(e).__name__}: {e}",
            # CORS mínimo para que el navegador muestre el error legible
            headers={
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Headers": "Authorization, Content-Type",
            }
        )

# Helpers de Almacenamiento
# F3: caché de camera.json por (user, cam) con TTL 5s. Con N cámaras × N fps
# se estaba leyendo disco 2 veces por frame. La invalida _write_json_atomic
# y los writes directos de camera.json son poco frecuentes (config edit).
_CAM_CFG_CACHE: Dict[str, tuple] = {}  # key -> (ts, cfg)
_CAM_CFG_TTL = 5.0


def _invalidate_cam_cfg_cache(user_id: str = None, camera_id: str = None):
    if user_id is None:
        _CAM_CFG_CACHE.clear()
        return
    _CAM_CFG_CACHE.pop(f"{user_id}/{camera_id}", None)


def get_camera_config_static(user_id: str, camera_id: str) -> dict:
    """Lee camera.json de una camara (cacheado 5s — F3)."""
    key = f"{user_id}/{camera_id}"
    hit = _CAM_CFG_CACHE.get(key)
    now = time.time()
    if hit and now - hit[0] < _CAM_CFG_TTL:
        return hit[1]
    cam_file = user_root(user_id) / "cameras" / camera_id / "camera.json"
    if cam_file.exists():
        try:
            with open(cam_file) as f:
                cfg = json.load(f)
            _CAM_CFG_CACHE[key] = (now, cfg)
            return cfg
        except Exception as e_1274:
            # P0 (Sección #8): loggeo en lugar de pass silencioso
            logger.warning(f"[load-cam-config] {camera_id} corrupt: {e_1274}")
            return None
    return {}

def get_disk_config() -> dict:
    """Cargar configuracion de discos o crear defaults."""
    if DISKS_CONFIG_FILE.exists():
        with open(DISKS_CONFIG_FILE) as f:
            cfg = json.load(f)
        if "disks" in cfg:
            return cfg
    default = {
        "disks": [{"mount": str(STORAGE_ROOT), "label": "Primary", "total_gb": 1000,
                   "used_gb": 0, "free_gb": 1000, "user_folder": "users"}],
        "plans": {
            "founder": {"max_storage_gb": 500, "max_cameras": 5},
            "pro": {"max_storage_gb": 250, "max_cameras": 3},
            "free": {"max_storage_gb": 10, "max_cameras": 1}
        }
    }
    save_disk_config(default)
    return default

def save_disk_config(cfg: dict):
    """Guardar configuracion de discos atomicamente."""
    DISKS_CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = DISKS_CONFIG_FILE.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(cfg, f, indent=2)
    tmp.replace(DISKS_CONFIG_FILE)


def _dir_used_mb(path) -> float:
    """Tamaño de un directorio en MB usando du (rapido, C-level). Fallback 0."""
    try:
        import subprocess
        r = subprocess.run(["du", "-sm", str(path)], capture_output=True, text=True, timeout=10)
        if r.returncode == 0 and r.stdout.strip():
            return float(r.stdout.split()[0])
    except Exception as e_1313:
        # P0 (Sección #8): loggeo en lugar de pass silencioso
        logger.warning(f"[dir-used-mb] du failed for {path}: {e_1313}")
    return 0.0

_USER_DISK_CACHE: Dict[str, tuple] = {}  # uid -> (ts, path_str)


def invalidate_user_disk_cache(user_id: str = None):
    if user_id is None:
        _USER_DISK_CACHE.clear()
    else:
        _USER_DISK_CACHE.pop(user_id, None)


def get_user_storage_path(user_id: str, plan: str = "founder") -> Path:
    """Resolver ruta de almacenamiento del usuario.

    ST-2 (2026-09-01): ORDEN DE RESOLUCIÓN —
      1. user.json.disk_mount del propio usuario (elegido desde el panel o
         por migrate) — ES la fuente de verdad, respeta la decisión humana.
      2. priority_disk del plan (config de discos del admin).
      3. Disco con más espacio libre.
    Cacheado 5s por usuario (se llama en cada frame del ingest)."""
    import time as _t
    now = _t.time()
    hit = _USER_DISK_CACHE.get(user_id)
    if hit and now - hit[0] < 5.0:
        return Path(hit[1])
    cfg = get_disk_config()
    disks = cfg.get("disks", [])
    plans = cfg.get("plans", {})
    selected = None
    # 1) disk_mount explícito del usuario (persistido por el panel).
    # ST-2b SANITIZADO: solo se acepta si matchea un mount REAL de la config
    # de discos. El bug: disk_mount basura ('/storage/users/users') no
    # matcheaba nada, Path(dm).exists() era True y se le sumaba 'users/'
    # OTRA VEZ → rutas compuestas ('/users/users/users/...') acumulándose
    # en cada rewrite. Ahora: mount inválido = ignorar (caer a plan/más
    # espacio) y nunca componer user_folder encima de un path que ya lo
    # trae.
    uf = _compat_user_json_path(user_id)
    if uf and uf.exists():
        try:
            dm = json.loads(uf.read_text()).get("disk_mount")
            if dm:
                dm = dm.rstrip("/")
                sel = next((d for d in disks if d.get("mount") == dm), None)
                if sel is not None:
                    selected = sel
                elif dm == str(STORAGE_ROOT):
                    selected = {"mount": dm, "user_folder": "users"}
        except Exception:
            pass
    # 2) priority_disk del plan
    if selected is None:
        target = plans.get(plan, plans.get("founder", {}))
        priority = target.get("priority_disk")
        if priority:
            selected = next((d for d in disks if d.get("mount") == priority), None)
    # 3) más espacio libre
    if selected is None:
        selected = max(disks, key=lambda d: d.get("free_gb", 0),
                       default=disks[0] if disks else None)
    if selected is None:
        selected = {"mount": str(STORAGE_ROOT), "user_folder": "users"}
    out = Path(selected["mount"]) / str(selected.get("user_folder", "users")).strip("/") / user_id
    _USER_DISK_CACHE[user_id] = (now, str(out))
    return out


def user_root(user_id: str) -> Path:
    """ST-3: RAÍZ del usuario según su disco (get_user_storage_path).
    TODO el pipeline de frames/eventos debe usar esto — no STORAGE_ROOT
    directo — para que la migración de disco (panel) tenga efecto real.
    Cacheado 5s por usuario (se llama en cada frame)."""
    return get_user_storage_path(user_id, "free")


def user_event_search_dirs(user_id: Optional[str]) -> List[Path]:
    """ST-3 fix (2026-09-02): carpetas 'cameras' del usuario en TODOS sus discos.

    El usuario puede tener disk_mount en el HDD (panel/migrate) y sus eventos
    se guardan allí (save_event_to_disk_v2 usa get_user_storage_path), pero
    los endpoints de artefactos (thumb/frame/clip) buscaban SOLO en
    STORAGE_ROOT fijo (SSD) → 404 en todos los eventos nuevos + NS_ERROR/
    ORB en el frontend.

    Devuelve: [disco_real_del_usuario, STORAGE_ROOT] deduplicados — el disco
    real primero (hit común), SSD como fallback legacy.
    """
    dirs = []
    if user_id and user_id != "default":
        try:
            real = user_root(user_id) / "cameras"
            if real.is_dir():
                dirs.append(real)
        except Exception:
            pass
    legacy = Path(STORAGE_ROOT) / "users" / (user_id or "") / "cameras"
    if legacy.is_dir() and legacy not in dirs:
        dirs.append(legacy)
    # sin user_id: buscar en todos los usuarios de ambos discos (raro)
    if not dirs:
        for base_root in (Path(STORAGE_ROOT) / "users",):
            if base_root.is_dir():
                for d in base_root.iterdir():
                    if d.is_dir() and (d / "cameras").is_dir():
                        dirs.append(d / "cameras")
    return dirs


def _compat_user_json_path(user_id: str) -> Optional[Path]:
    """user.json de compat (STORAGE_ROOT/users/uid LITERAL, sin resolución) —
    es el que se mantiene sincronizado en todos los writes (patrón dual).
    OJO: NO usar user_root() aquí — crearía recursión infinita con
    get_user_storage_path (bug ST-3, corregido al detectarlo)."""
    p = STORAGE_ROOT / "users" / user_id / "user.json"
    return p if p.exists() else None

def find_user_json(user_id: str) -> Optional[Path]:
    """Buscar user.json de un usuario en todos los discos."""
    for plan in ("founder", "pro", "free"):
        path = get_user_storage_path(user_id, plan) / "user.json"
        if path.exists():
            return path
    compat = user_root(user_id) / "user.json"
    return compat if compat.exists() else None

def resolve_user_events_dirs(user_id: str) -> List[tuple]:
    """Devolver carpetas del usuario (nueva estructura + compat).

    HOT-COLD F2 (2026-09-02): busca en TODOS los discos montados — primero
    los activos (prioridad de lectura fresca) y después los ARCHIVADOS
    (histórico legible tras un cambio de disco, sin mover nada). Esto es lo
    que hace que archivar un disco lleno no 'esconda' los eventos viejos.
    Deduplica por (cámara, ruta)."""
    seen = set()
    dirs = []
    try:
        cfg = get_disk_config()
        disks = cfg.get("disks", [])
        activos = [d for d in disks if not d.get("archived")]
        archivados = [d for d in disks if d.get("archived")]
        bases = []
        # disco actual del usuario (user_root) primero
        bases.append(("actual", user_root(user_id)))
        # resto de discos activos (por si el usuario estuvo ahí antes)
        for d in activos:
            bases.append(("activo", Path(d.get("mount", "")) / d.get("user_folder", "users") / user_id))
        # discos archivados (histórico)
        for d in archivados:
            bases.append(("archivado", Path(d.get("mount", "")) / d.get("user_folder", "users") / user_id))
    except Exception:
        bases = [("actual", user_root(user_id))]
    for _, base in bases:
        try:
            cameras_dir = base / "cameras"
            if cameras_dir.exists():
                for cam_id in cameras_dir.iterdir():
                    events = cam_id / "events"
                    key = (cam_id.name, str(events))
                    if events.is_dir() and key not in seen:
                        seen.add(key)
                        dirs.append((cam_id.name, events))
            legacy = base / "events"
            if legacy.is_dir() and ("_global", str(legacy)) not in seen:
                seen.add(("_global", str(legacy)))
                dirs.append(("_global", legacy))
        except Exception:
            continue
    return dirs

def resolve_user_id(camera_id: str, provided_user_id: str, client_ip: str = "unknown") -> str:
    """Resolver user_id desde camera_id o IP si provided es default."""
    if provided_user_id and provided_user_id != "default":
        return provided_user_id
    # Mapeo de IPs conocidas a usuarios (camaras sin user_id)
    IP_USER_MAP = {
        "10.0.0.161": "moXcjYsfYogCFfvHq0TmadF8ytt2",
    }
    if client_ip in IP_USER_MAP:
        return IP_USER_MAP[client_ip]
    users_dir = STORAGE_ROOT / "users"
    if users_dir.is_dir():
        founder_match = None
        fcm_match = None
        any_match = None
        for user_folder in users_dir.iterdir():
            user_file = user_folder / "user.json"
            if user_file.is_file():
                try:
                    with open(user_file) as f:
                        ud = json.load(f)
                    for cam in ud.get("cameras", []):
                        if cam.get("camera_id") == camera_id:
                            uid = ud.get("user_id", user_folder.name)
                            plan = ud.get("plan", "")
                            has_fcm = bool(ud.get("fcm_tokens"))
                            if plan == "founder" and has_fcm:
                                founder_match = uid
                            elif has_fcm:
                                fcm_match = uid
                            else:
                                any_match = uid
                except Exception as e_1394:
                    # P0 (Sección #8): loggeo en lugar de pass silencioso
                    logger.warning(f"[find-user-by-camera] {user_file.name} read failed: {e_1394}")
        return founder_match or fcm_match or any_match or provided_user_id
    return provided_user_id

# Modelos Pydantic
class AnalysisRequest(BaseModel):
    prompt: str = "Describe this image briefly."
    priority: int = 10
    max_tokens: int = 100

class ChatRequest(BaseModel):
    message: str
    user_id: str
    session_id: str
    cam_id: Optional[str] = None
    include_frame: bool = False

# Endpoints Publicos
@app.get("/health")
async def health():
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(10.0, connect=5.0, read=5.0, write=5.0),
            headers={"Connection": "close"},
            http1=True,
            http2=False
        ) as client:
            resp = await client.get("http://localhost:8004/health")
            status = "ok" if resp.status_code == 200 else "degraded"
    except:
        status = "error"
    return {
        "status": status, "service": "eva-api", "version": "7.0",
        # B5: metricas de queue (para observabilidad)
        # C2: stats async del bus (incluye size real del stream + pending
        # del consumer group cuando el modo es redis).
        "frame_queue": await frame_bus.stats() | {
            "drops": FRAME_QUEUE_DROPS,
            "rate_limit_drops": sum(_INGEST_RATE_DROPS.values()),  # C4
            "workers": WORKER_COUNT,         # C1
            "camera_locks": len(CAMERA_LOCKS),
        },
    }

@app.get("/api/support-info")
async def support_info():
    """Info publica de contacto/soporte (editable desde admin). No requiere auth."""
    try:
        cfg = get_disk_config()
        sc = cfg.get("support_contact", {})
        return {
            "whatsapp": sc.get("whatsapp", ""),
            "email": sc.get("email", ""),
            "phone": sc.get("phone", ""),
            "bank_info": sc.get("bank_info", "")
        }
    except Exception:
        return {"whatsapp": "", "email": "", "phone": "", "bank_info": ""}


@app.get("/frames/{user_id}/{camera_id}/latest.jpg")
async def get_camera_latest_jpg(user_id: str, camera_id: str):
    """F-admin: miniatura del panel de monitoreo. Sirve latest_raw.jpg de
    la cámara (lo que está viendo ahora). Public path: la URL no expone nada
    sin conocer user_id+camera_id (GUIDs no secuenciales)."""
    _validate_safe_path(user_id, "user_id")
    _validate_safe_path(camera_id, "camera_id")
    raw = user_root(user_id) / "cameras" / camera_id / "frames" / "latest_raw.jpg"
    if not raw.exists():
        raise HTTPException(status_code=404, detail="sin frames")
    return Response(content=raw.read_bytes(), media_type="image/jpeg")


@app.get("/frames/{user_id}/{camera_id}/events/{image_name}")
async def get_camera_event_image(user_id: str, camera_id: str, image_name: str):
    """F-admin: imagen de evidencia de un evento para el panel (jpg hermano
    del evt_*.json). Path validado contra traversal."""
    _validate_safe_path(user_id, "user_id")
    _validate_safe_path(camera_id, "camera_id")
    _validate_safe_path(image_name, "image_name")
    if not image_name.endswith(".jpg"):
        raise HTTPException(status_code=400, detail="solo jpg")
    img = user_root(user_id) / "cameras" / camera_id / "events" / image_name
    if not img.exists():
        # algunos eventos guardan carpeta evt_*/grid.jpg
        alt = img.with_suffix("") / "grid.jpg"
        if alt.exists():
            img = alt
        else:
            raise HTTPException(status_code=404, detail="imagen no encontrada")
    return Response(content=img.read_bytes(), media_type="image/jpeg")


@app.get("/frames/latest")
async def get_latest_frame(camera_id: Optional[str] = None, user_id: Optional[str] = None):
    grid = orchestrator._get_grid(user_id or "", camera_id or "")
    frame_bytes = grid.get_last_frame_bytes()
    last_cam = grid.get_last_camera_id()
    if camera_id and last_cam and last_cam != camera_id:
        frame_bytes = b""
    if not frame_bytes and camera_id and user_id:
        try:
            events_dir = user_root(user_id) / "cameras" / camera_id / "events"
            frames_dir = user_root(user_id) / "cameras" / camera_id / "frames"
            live_raw = frames_dir / "latest_raw.jpg"
            sentinel_jpg = events_dir / "latest_vigilance.jpg"

            # FIX (2026-09-02): el centinela NO debe tapar el live.
            # Antes: si la cámara de noche no era centinela (no detectaba nada)
            # o el host reinició y el stream live no había llegado aún,
            # se servía latest_vigilance.jpg (de la noche anterior, con bbox
            # dibujado) COMO SI FUERA EL FRAME ACTUAL → el usuario veía una
            # imagen oscura de 22:15 con overlay "persona 73%" encima de su
            # negocio clarísimo de 09:17am. Ahora: prioridad al raw live; la
            # imagen centinela solo se usa si NO hay raw o es MUY vieja (>5 min
            # = la cámara está apagada y hubo centinela hace poco).
            live_raw = None
            sentinel_stale_after_s = 300  # >5min sin live = quizás cámara fuera
            try:
                if live_raw.exists():
                    raw_mtime = live_raw.stat().st_mtime
                else:
                    raw_mtime = 0
            except Exception:
                raw_mtime = 0
            sentinel_mtime = 0
            try:
                if sentinel_jpg.exists():
                    sentinel_mtime = sentinel_jpg.stat().st_mtime
            except Exception:
                sentinel_mtime = 0

            # usar el más reciente, con la regla de que el centinela SOLO sea
            # fallback cuando el raw esté viejo (cámara desconectada)
            if live_raw.exists() and raw_mtime >= sentinel_mtime - sentinel_stale_after_s:
                frame_bytes = live_raw.read_bytes()
                last_cam = camera_id
            elif sentinel_jpg.exists():
                frame_bytes = sentinel_jpg.read_bytes()
                last_cam = camera_id
            elif live_raw.exists():
                frame_bytes = live_raw.read_bytes()
                last_cam = camera_id
        except Exception:
            logger.debug("silent except")

    image_b64 = base64.b64encode(frame_bytes).decode() if frame_bytes else ""
    yolo_count = grid.get_last_yolo_count()
    yolo_detections = grid.get_last_yolo_detections()
    # Leer latest_yolo.json si existe (más actualizado, funciona en modo centinela también).
    # FIX (2026-09-02): ventana estricta: solo confiar en latest_yolo.json si
    # está a menos de 4 segundos de antigüedad (1 tick completo del worker).
    # El overlay del live view usa exactamente el frame actual. Con la ventana
    # de 60s el overlay se quedaba pegado al último evento con persona,
    # dibujando bboxes sobre el frame actual como si todavía estuviera.
    try:
        # HOT-COLD (2026-09-02): el viewer lee del HOT primero (path fijo
        # NVMe — migraciones no lo tocan), luego disco del usuario, luego
        # layout viejo NVMe. Toma el MÁS FRESCO de los que existan.
        import os as _os
        _cands = [
            hot_dir(user_id or "default", camera_id or "") / "latest_yolo.json",
            user_root(user_id or "default") / "cameras" / (camera_id or "") / "frames" / "latest_yolo.json",
            STORAGE_ROOT / "users" / (user_id or "default") / "cameras" / (camera_id or "") / "frames" / "latest_yolo.json",
        ]
        _yolo_json_path = None
        _best_mtime = 0
        for _c in _cands:
            try:
                if _c.exists() and _c.stat().st_mtime > _best_mtime:
                    _yolo_json_path = _c
                    _best_mtime = _c.stat().st_mtime
            except OSError:
                continue
        if _yolo_json_path.exists():
            with open(_yolo_json_path) as _f:
                _yolo_data = json.load(_f)
            _yolo_ts = _yolo_data.get("timestamp", 0)
            _yolo_age = time.time() - _yolo_ts if isinstance(_yolo_ts, (int, float)) else 999
            if _yolo_age < 4.0:
                yolo_detections = _yolo_data.get("detections", [])
                yolo_count = _yolo_data.get("count", len(yolo_detections))
            else:
                yolo_detections = []
                yolo_count = 0
    except:
        yolo_detections = []
        yolo_count = 0

    return {
        "success": bool(frame_bytes),
        "image_b64": image_b64,
        "camera_id": last_cam,
        "yolo": {"count": yolo_count, "detections": yolo_detections},
        "metadata": {"timestamp": int(time.time())}
    }

@app.get("/frames/latest.jpg")
async def get_latest_frame_jpg(camera_id: Optional[str] = None, user_id: Optional[str] = None):
    grid = orchestrator._get_grid(user_id or "", camera_id or "")
    frame_bytes = grid.get_last_frame_bytes()
    last_cam = grid.get_last_camera_id()
    if camera_id and last_cam and last_cam != camera_id:
        frame_bytes = b""
    if not frame_bytes and camera_id and user_id:
        try:
            hot_raw = hot_dir(user_id, camera_id) / "latest_raw.jpg"
            if hot_raw.exists():
                frame_bytes = hot_raw.read_bytes()
            if not frame_bytes:
                events_dir = user_root(user_id) / "cameras" / camera_id / "events"
                latest_vig = events_dir / "latest_vigilance.jpg"
                if latest_vig.exists():
                    frame_bytes = latest_vig.read_bytes()
                else:
                    frames_dir = user_root(user_id) / "cameras" / camera_id / "frames"
                    latest_raw = frames_dir / "latest_raw.jpg"
                    if latest_raw.exists():
                        frame_bytes = latest_raw.read_bytes()
        except:
            logger.debug("silent except")

    if not frame_bytes:
        return Response(status_code=204)
    return Response(content=frame_bytes, media_type="image/jpeg", headers={"Cache-Control": "no-store, no-cache, must-revalidate"})

@app.get("/frames/latest-raw.jpg")
async def get_latest_raw_jpg(camera_id: Optional[str] = None, user_id: Optional[str] = None):
    """Frame en vivo más reciente (siempre se guarda, sin importar YOLO)."""
    if not camera_id or not user_id:
        return Response(status_code=204)
    try:
        # HOT-COLD: hot primero (path fijo NVMe), fallback disco del usuario
        latest_raw = hot_dir(user_id, camera_id) / "latest_raw.jpg"
        if not latest_raw.exists():
            latest_raw = user_root(user_id) / "cameras" / camera_id / "frames" / "latest_raw.jpg"
        if latest_raw.exists():
            frame_bytes = latest_raw.read_bytes()
            return Response(content=frame_bytes, media_type="image/jpeg", headers={
                "Cache-Control": "no-store, no-cache, must-revalidate",
                "Access-Control-Allow-Origin": "*",
                "Cross-Origin-Resource-Policy": "cross-origin",
            })
    except:
        logger.debug("silent except")

    return Response(status_code=204)


# ═══════════════════════════════════════════════════════════════════════════
# C2.1 — Eva Frame (imagen que Eva muestra en el chat de configuración)
# ═══════════════════════════════════════════════════════════════════════════
# Sirve el archivo eva_frame.jpg que Eva guardó al analizar la imagen.
# El frontend lo carga como <img src="/eva-frame/{user_id}/{camera_id}">.

@app.get("/eva-frame/{user_id}/{camera_id}")
async def get_eva_frame_endpoint(user_id: str, camera_id: str):
    """Sirve la imagen que Eva guardó para el chat de configuración (eva_frame.jpg)."""
    try:
        # 1. Buscar en la cámara configurada
        frame_path = user_root(user_id) / "cameras" / camera_id / "frames" / "eva_frame.jpg"
        if frame_path.exists():
            return Response(content=frame_path.read_bytes(), media_type="image/jpeg", headers={
                "Cache-Control": "no-store, no-cache, must-revalidate",
                "Access-Control-Allow-Origin": "*",
                "Cross-Origin-Resource-Policy": "cross-origin",
            })

        # 2. Fallback: latest_raw.jpg
        raw_path = user_root(user_id) / "cameras" / camera_id / "frames" / "latest_raw.jpg"
        if raw_path.exists():
            return Response(content=raw_path.read_bytes(), media_type="image/jpeg", headers={
                "Cache-Control": "no-store, no-cache, must-revalidate",
                "Access-Control-Allow-Origin": "*",
                "Cross-Origin-Resource-Policy": "cross-origin",
            })

        # 3. Fallback: latest_vigilance.jpg
        vig_path = user_root(user_id) / "cameras" / camera_id / "events" / "latest_vigilance.jpg"
        if vig_path.exists():
            return Response(content=vig_path.read_bytes(), media_type="image/jpeg", headers={
                "Cache-Control": "no-store, no-cache, must-revalidate",
                "Access-Control-Allow-Origin": "*",
                "Cross-Origin-Resource-Policy": "cross-origin",
            })
    except Exception as e:
        logger.error(f"Error sirviendo eva-frame: {e}")
    # P0 (2026-08-22): devolver PNG 1x1 transparente en vez de 204.
    # Chrome 117+ bloquea respuestas opacas 204 (OpaqueResponseBlocking / ORB)
    # cuando se cargan como <img src>, disparando NS_ERROR_DOM_NETWORK_ERR.
    # Fix (2026-08-26): bytes validados con PIL — el PNG anterior
    # decia "broken data stream" en Firefox ("Image corrupt or truncated").
    transparent_png = bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452"
        "000000010000000108060000001f15c489"
        "0000000d49444154789c63606060600000"
        "00050001a5f645400000000049454e44ae426082"
    )
    return Response(
        content=transparent_png,
        media_type="image/png",
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate",
            "Access-Control-Allow-Origin": "*",
            "Cross-Origin-Resource-Policy": "cross-origin",
        },
    )

@app.get("/grid/latest")
async def get_latest_grid(partial: int = 1, camera_id: Optional[str] = None, user_id: Optional[str] = None):
    # B4: envolver en try — si orchestrator._get_grid o get_grid_image/encode
    # tiran (entrada corrupta, grid vacio, encoding fallido), devolver 502
    # controlado en vez de 500 con traceback al cliente.
    try:
        grid = orchestrator._get_grid(user_id or "", camera_id or "")
        info = grid.get_grid_info()
        grid_b64 = ""
        if info.get("frame_count", 0) > 0:
            grid_img = grid.get_grid_image()
            if grid_img:
                grid_b64 = base64.b64encode(grid_img).decode()
        return {
            "frames_used": info.get("frame_count", 0),
            "grid_b64": grid_b64,
            "camera_ids": info.get("camera_ids", []),
            "partial": bool(partial)
        }
    except Exception as e:
        logger.error(f"[grid/latest] {type(e).__name__}: {e}")
        return JSONResponse(
            {"frames_used": 0, "grid_b64": "", "camera_ids": [],
             "partial": bool(partial), "error": str(e)},
            status_code=502,
        )


# ═══════════════════════════════════════════════════════════════════════════
# FASE 3 (F3.5): KPIs y salud de la cámara
# ═══════════════════════════════════════════════════════════════════════════

_FROZEN_CAM_THRESHOLD_S = 300  # sin frames > 5 min = cámara congelada/desconectada


@app.get("/api/cameras/{camera_id}/health")
async def camera_health_endpoint(camera_id: str, user_id: Optional[str] = None):
    """F3.5: KPIs de vigilancia por cámara.

    Devuelve: alertas/24h, eventos/24h, fp_rate, cámara congelada
    (sin eventos recientes), y actividad del grid en vivo.
    Playbook 2026: KPIs de confianza del operador (precision-first).
    """
    if not user_id or not camera_id:
        return {"success": False, "error": "user_id y camera_id requeridos"}
    try:
        cam_dir = user_root(user_id) / "cameras" / camera_id
        cam_cfg_path = cam_dir / "camera.json"
        cam = {}
        if cam_cfg_path.exists():
            cam = json.loads(cam_cfg_path.read_text())
        metrics = cam.get("metrics", {}) or {}
        now = time.time()

        # Cámara congelada: el último frame del grid en vivo + last_event_at
        grid = orchestrator._get_grid(user_id, camera_id)
        grid_info = grid.get_grid_info()
        last_frame_age = None
        try:
            latest_raw = cam_dir / "frames" / "latest_raw.jpg"
            if latest_raw.exists():
                last_frame_age = int(now - latest_raw.stat().st_mtime)
        except Exception:
            pass
        frozen = (last_frame_age is not None and last_frame_age > _FROZEN_CAM_THRESHOLD_S)

        return {
            "success": True,
            "camera_id": camera_id,
            "alerts_last_24h": metrics.get("alerts_last_24h", 0),
            "events_last_24h": metrics.get("events_last_24h", 0),
            "total_alerts": metrics.get("total_alerts", 0),
            "total_false_positives": metrics.get("total_false_positives", 0),
            "fp_rate": metrics.get("fp_rate", 0.0),
            "needs_review": metrics.get("needs_review", False),
            "last_event_at": metrics.get("last_event_at"),
            "last_frame_age_s": last_frame_age,
            "frozen": frozen,
            "grid_live": {
                "frame_count": grid_info.get("frame_count", 0),
                "grid_size": grid_info.get("grid_size", 16),
            },
            "zones_count": len(cam.get("zones", []) or []),
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


# ── MJPEG Stream (viewer en tiempo real) ──────────────────────────────────
# Cache en RAM del último frame por (user_id, camera_id) para evitar disco
_frame_cache: Dict[str, bytes] = {}
_frame_cache_ts: Dict[str, float] = {}
_FRAME_CACHE_TTL = 5.0  # segundos

def _get_cache_key(user_id: str, camera_id: str) -> str:
    return f"{user_id}:{camera_id}"

def _cache_frame(user_id: str, camera_id: str, frame_bytes: bytes):
    key = _get_cache_key(user_id, camera_id)
    _frame_cache[key] = frame_bytes
    _frame_cache_ts[key] = time.time()

def _get_cached_frame(user_id: str, camera_id: str) -> Optional[bytes]:
    key = _get_cache_key(user_id, camera_id)
    ts = _frame_cache_ts.get(key, 0)
    if time.time() - ts > _FRAME_CACHE_TTL:
        return None
    return _frame_cache.get(key)

def _read_latest_frame_bytes(user_id: str, camera_id: str) -> Optional[bytes]:
    """Leer el frame más reciente: intenta cache en RAM primero, luego disco."""
    cached = _get_cached_frame(user_id, camera_id)
    if cached:
        return cached
    try:
        frames_dir = user_root(user_id) / "cameras" / camera_id / "frames"
        latest_raw = frames_dir / "latest_raw.jpg"
        if latest_raw.exists():
            data = latest_raw.read_bytes()
            if data:
                _cache_frame(user_id, camera_id, data)
                return data
    except Exception as e_1647:
        # P0 (Sección #8): loggeo en lugar de pass silencioso
        logger.warning(f"[get-frame-fallback] {user_id}/{camera_id} read failed: {e_1647}")
    return None


@app.get("/cameras/{camera_id}/stream")
async def camera_mjpeg_stream(camera_id: str, user_id: str = None, fps: int = 2):
    """
    MJPEG stream en tiempo real para el viewer.
    - fps: frames por segundo (1-10, default 2)
    - No requiere suscripción activa (solo visualización)
    - Usa cache en RAM para minimizar I/O de disco
    - Envia el ultimo frame conocido en cada tick (aunque se repita)
      para mantener la conexion activa y el navegador fluido
    """
    if not user_id:
        raise HTTPException(status_code=400, detail="user_id required")

    fps = max(1, min(fps, 10))
    frame_interval = 1.0 / fps
    boundary = b"--frame"

    def _build_keepalive_jpeg(text: str) -> bytes:
        """Frame gris con timestamp para mantener el stream vivo y diferenciarlo de frames reales."""
        import io
        try:
            from PIL import Image, ImageDraw
            img = Image.new("RGB", (480, 320), (22, 22, 22))
            d = ImageDraw.Draw(img)
            d.rectangle([6, 6, 474, 36], fill=(8, 8, 8))
            d.text((14, 12), "OjoIA - sin senal reciente", fill=(255, 255, 255))
            d.text((14, 280), text[:60], fill=(180, 180, 180))
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=55)
            return buf.getvalue()
        except Exception:
            return b"\xff\xd8\xff\xe0"  # bytes  no validos pero >0

    async def generate():
        last_frame_bytes = None
        first_frame_sent = False
        repeats = 0
        no_frame_ticks = 0

        while True:
            try:
                frame_bytes = _read_latest_frame_bytes(user_id, camera_id)

                # ── Heartbeat ──
                # Si no hay frame del hardware por un tiempo, enviar un frame keepalive
                # con timestamp para: (a) mantener la conexion TCP activa y evitar idle
                # timeout de Cloudflare/proxies, (b) que el vea que el stream NO esta
                # colgado (el campeon dispara >=  por lo cual el watchdog del front no
                # dispara falsos), (c) senalar al operador que la camara esta offline.
                if not frame_bytes:
                    no_frame_ticks += 1
                    frame_bytes = _build_keepalive_jpeg(
                        f"{camera_id[:12]} | {datetime.now().strftime('%H:%M:%S')} | esperando camara..."
                    )
                    last_frame_bytes = None  # forzar always enviado
                    repeats = 0
                else:
                    no_frame_ticks = 0

                if frame_bytes:
                    # Reenviar SIEMPRE el último frame conocido en cada tick.
                    # El ESP32 puede enviar 1 frame cada ~10s (TLS handshake lento),
                    # pero el viewer necesita recibir frames continuamente para no
                    # disparar el watchdog a los 8s y para que el navegador sienta
                    # que el stream está "vivo". Comparar byte-a-byte y saltarse
                    # frames idénticos congelaba el viewer visualmente.
                    first_frame_sent = True
                    last_frame_bytes = frame_bytes
                    repeats = 0
                    yield b"Content-Type: image/jpeg\r\n"
                    yield f"Content-Length: {len(frame_bytes)}\r\n".encode()
                    yield b"Cache-Control: no-store\r\n"
                    yield b"\r\n"
                    yield frame_bytes
                    yield b"\r\n"
                    yield boundary + b"\r\n"

            except Exception as e:
                logger.warning(f"MJPEG stream error for {camera_id}: {e}")

            await asyncio.sleep(frame_interval)

    return StreamingResponse(
        generate(),
        media_type="multipart/x-mixed-replace; boundary=frame",
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate",
            "Connection": "close",
            "Pragma": "no-cache",
            "X-Accel-Buffering": "no",
        }
    )

# Auth Firebase

# ── Auto-reglas basadas en tipo de negocio y preocupaciones ──
def _generate_initial_rules(business_type: str, main_concerns: list) -> list:
    """Generar reglas iniciales (EN) basadas en business_type + main_concerns para Qwen."""
    biz = business_type.lower().strip()
    concerns_str = " ".join(main_concerns).lower() if main_concerns else ""
    
    rules = []
    if biz in ("retail", "colmado", "tienda", "supermercado", "abastecedora"):
        if "robo" in concerns_str or "dinero" in concerns_str or "caja" in concerns_str:
            rules = ["Is anyone taking items without going through checkout?", "Is the cash drawer visible and supervised?", "Is anyone putting items in pockets?"]
        elif "empleado" in concerns_str:
            rules = ["Is an employee alone at register off hours?", "Is every dispensing from the proper side?", "Is employee staying after close?"]
        else:
            rules = ["Is there anyone after business hours?", "Is there movement at the cash register?", "Is there suspicious activity?"]
    elif biz in ("pharmacy", "farmacia"):
        rules = ["Is every medication processed at register?", "Is anyone accessing controlled meds?", "Is anyone there after hours?"]
    elif biz in ("restaurant", "restaurante", "comedor", "cafetería", "cafeteria"):
        rules = ["Is service from designated area only?", "Are all dishes rung up?", "Is anyone in kitchen after close?"]
    elif biz in ("warehouse", "almacen", "bodega"):
        rules = ["Is anyone entering unauthorized?", "Is anyone in storage?", "Is there off-hours movement?"]
    elif biz in ("office", "oficina"):
        rules = ["Is anyone with equipment after hours?", "Is anyone in restricted areas?", "Are people there after hours?"]
    elif biz in ("home", "casa"):
        rules = ["Is anyone after rest hours?", "Is there door/window movement?", "Is there suspicious activity?"]
    elif biz in ("agriculture", "finca", "granja"):
        rules = ["Are there people in restricted zones?", "Is anyone accessing buildings?", "Is there unauthorized movement?"]
    else:
        rules = ["Is anyone after business hours?", "Is there suspicious movement?", "Is there unusual activity?"]
    return rules[:3]
def _build_initial_prompt(business_type: str, main_concerns: list, rules: list, schedule_open: str, schedule_close: str) -> str:
    """Construir system_prompt inicial basado en business_type + reglas."""
    biz = business_type.lower().strip()
    biz_labels = {
        "retail": "colmado/tienda", "pharmacy": "farmacia", "restaurant": "restaurante",
        "warehouse": "almacen", "office": "oficina", "home": "casa", "agriculture": "finca",
    }
    biz_label = biz_labels.get(biz, biz or "negocio")
    
    concerns = ", ".join(main_concerns) if main_concerns else "general security"
    rules_text = "; ".join(rules[:3]) if rules else "monitor suspicious activity"
    
    return (
        f"Security camera monitoring for a {biz_label} in Dominican Republic. "
        f"Main concerns: {concerns}. "
        f"Business hours: {schedule_open} to {schedule_close}. "
        f"Alert rules: {rules_text}. "
        f"Outside business hours: ANY person present = immediate critical alert."
    )

def _generate_rules_es(business_type: str, main_concerns: list) -> list:
    """Generar reglas en español para mostrar en la UI."""
    biz = business_type.lower().strip()
    concerns_str = " ".join(main_concerns).lower() if main_concerns else ""
    if biz in ("retail", "colmado", "tienda", "supermercado", "abastecedora"):
        if "robo" in concerns_str or "dinero" in concerns_str or "caja" in concerns_str:
            return ["¿Alguien toma productos sin pasar por la caja?", "¿El cajón de dinero está visible y supervisado?", "¿Alguien mete cosas en los bolsillos?"]
        return ["¿Hay alguien después del horario?", "¿Movimiento en la caja registradora?", "¿Actividad sospechosa?"]
    elif biz in ("pharmacy", "farmacia"):
        return ["¿Todo medicamento pasa por caja?", "¿Alguien accede a medicamentos controlados?", "¿Hay gente después del cierre?"]
    elif biz in ("restaurant", "restaurante", "comedor", "cafetería", "cafeteria"):
        return ["¿Servicio solo del área designada?", "¿Todos los platos se registran?", "¿Alguien en la cocina después del cierre?"]
    elif biz in ("warehouse", "almacen", "bodega"):
        return ["¿Alguien entra sin autorización?", "¿Alguien en el almacén?", "¿Movimiento fuera de horario?"]
    elif biz in ("office", "oficina"):
        return ["¿Alguien con equipos después del horario?", "¿Alguien en áreas restringidas?", "¿Gente después del horario?"]
    elif biz in ("home", "casa"):
        return ["¿Alguien después del horario de descanso?", "¿Movimiento en puertas/ventanas?", "¿Actividad sospechosa?"]
    return ["¿Alguien después del horario?", "¿Movimiento sospechoso?", "¿Actividad inusual?"]

@app.post("/auth/firebase/verify")
async def verify_firebase(request: Request):
    data = await request.json()
    id_token = data.get("id_token") or data.get("idToken")
    if not id_token:
        raise HTTPException(status_code=400, detail="Missing idToken")
    try:
        # B3: verify_id_token hace llamada de red a Google (I/O bloqueante).
        # Mover fuera del event loop con asyncio.to_thread para no bloquear
        # otros handlers durante la verificacion (puede tardar 200-1000ms).
        decoded = await asyncio.to_thread(auth.verify_id_token, id_token)
        # B4: defensivo — si Firebase no trae uid (token malformado/exotico),
        # devolver 400 en vez de KeyError 500.
        uid = decoded.get("uid")
        if not uid:
            raise HTTPException(status_code=400, detail="Token sin uid")
        email = decoded.get("email", "")
        name = data.get("name", "")
        business_name = data.get("business_name", "")
        business_type = data.get("business_type", "")
        phone = data.get("phone", "")
        main_concerns_raw = data.get("main_concerns", "")
        # Convertir main_concerns a lista si viene como string
        if isinstance(main_concerns_raw, str):
            main_concerns = [c.strip() for c in main_concerns_raw.split(",") if c.strip()]
        else:
            main_concerns = main_concerns_raw or []
        employee_count = data.get("employee_count", "1")
        camera_expected_count = data.get("camera_expected_count", "1")
        schedule_open = data.get("schedule_open", "08:00")
        schedule_close = data.get("schedule_close", "22:00")
        # F4 (2026-09-01): consentimiento paraguas del escaneo de red.
        # El registro envía consent_network_scan=true con el checkbox de
        # las políticas (v2). Se persiste versionado para auditoría.
        consent_network_scan = bool(data.get("consent_network_scan"))
        CONSENT_TERMS_VERSION = "v2"
        # Validate plan against config — only allow public plans
        cfg = get_disk_config()
        available_plans = cfg.get("plans", {})
        requested_plan = data.get("plan", "")
        if requested_plan and requested_plan in available_plans:
            plan_def = available_plans[requested_plan]
            if plan_def.get("public", True):
                plan = requested_plan
            else:
                plan = "free"
        else:
            plan = "free"
        storage_path = get_user_storage_path(uid, plan)
        existing = {}
        user_file = find_user_json(uid)
        if user_file and user_file.exists():
            with open(user_file) as f:
                existing = json.load(f)
            # Preserve existing billing fields on login
            existing_plan = existing.get("plan", plan)
            existing_status = existing.get("status", "active")
            existing_plan_end = existing.get("plan_end", 0)
            existing_trial_end = existing.get("trial_end", None)
            existing_access_token = existing.get("access_token", "")
            existing_payments = existing.get("payments", [])
            existing_last_payment = existing.get("last_payment", None)
            existing_next_due = existing.get("next_due", 0)
            # P1-billing (2026-09-01): preservar TODO el estado que el job de
            # vencimientos y el panel escriben — antes un re-login lo BORRABA
            # (user_data se reconstruía desde cero y estos campos se perdían:
            # avisos re-enviados, trial re-asignado, cuotas reseteadas).
            existing_state = {k: existing[k] for k in (
                "last_expiry_notice", "grace_notified", "suspended_at",
                "suspended_reason", "trial_used", "quota_gb", "disk_mount",
                "max_cameras", "fcm_tokens", "cameras", "retention",
                "migrated_at", "is_test",
            ) if k in existing}
        else:
            existing_plan = plan
            existing_status = "active"
            existing_plan_end = 0
            existing_trial_end = None
            existing_access_token = ""
            existing_payments = []
            existing_last_payment = None
            existing_next_due = 0
            existing_state = {}

        now_ts = int(time.time())
        plan_def = available_plans.get(plan, {})

        # Compute plan_end for new registrations
        if existing_plan_end and existing_plan_end > now_ts:
            plan_end = existing_plan_end
        else:
            duration = plan_def.get("duration_days", 30)
            plan_end = now_ts + (duration * 86400)

        # Trial handling — only set if plan offers trial and user never had one.
        # P1-billing fix: el flag trial_used persiste aunque trial_end se
        # pierda — antes un re-login re-regalaba 14 días (el usuario real
        # z6q lo sufrió: trial_end re-asignado el 31-ago tras re-login).
        trial_days = plan_def.get("trial_days", 0)
        trial_used = bool(existing_state.get("trial_used") or existing_trial_end)
        trial_end = None
        if trial_days > 0 and not existing_payments and not trial_used:
            trial_end = now_ts + (trial_days * 86400)
            existing_state["trial_used"] = True
        else:
            # preservar el trial_end histórico (aunque esté vencido) —
            # el job de vencimientos y el panel lo usan como referencia
            trial_end = existing_trial_end
            if trial_used:
                existing_state["trial_used"] = True  # sello persistente

        # Access token
        access_token = existing_access_token or ("oj_live_" + secrets.token_urlsafe(32))

        # Add access_token to api_tokens[] so it can be validated by _verify_user_token
        api_tokens = existing.get("api_tokens", []) or []
        token_exists = any(t.get("token") == access_token for t in api_tokens)
        if not token_exists:
            api_tokens.append({
                "id": "tok_" + secrets.token_urlsafe(8),
                "token": access_token,
                "created_at": now_ts,
                "expires_at": now_ts + 365 * 86400,  # 1 year
                "revoked": False
            })

        user_data = {
            "user_id": uid,
            # F4: consentimientos (auditable: versión + timestamp)
            "consent_network_scan": consent_network_scan or existing.get("consent_network_scan", False),
            "consent_terms_version": CONSENT_TERMS_VERSION if consent_network_scan else existing.get("consent_terms_version", ""),
            "consent_terms_at": now_ts if consent_network_scan else existing.get("consent_terms_at", 0),
            # P1-billing: estado del job de vencimientos/panel preservado
            **existing_state,
            "name": name or existing.get("name", ""),
            "email": email or existing.get("email", ""),
            "phone": phone or existing.get("phone", ""),
            "business_name": business_name or existing.get("business_name", ""),
            "business_type": business_type or existing.get("business_type", ""),
            "main_concerns": main_concerns or existing.get("main_concerns", []),
            "employee_count": employee_count or existing.get("employee_count", "1"),
            "camera_expected_count": camera_expected_count or existing.get("camera_expected_count", "1"),
            "plan": plan,
            "status": existing_status,
            "created_at": existing.get("created_at", str(now_ts)),
            "plan_start": existing.get("plan_start", now_ts),
            "plan_end": plan_end,
            "trial_end": trial_end,
            "billing_cycle": existing.get("billing_cycle", "monthly"),
            "grace_period_days": existing.get("grace_period_days", cfg.get("grace_period_days", 3)),
            "next_due": existing_next_due or plan_end,
            "payments": existing_payments,
            "last_payment": existing_last_payment,
            "access_token": access_token,
            "api_tokens": api_tokens,
            "schedule": existing.get("schedule", {"open": schedule_open, "close": schedule_close}),
            "cameras": existing.get("cameras", []),
            "fcm_tokens": existing.get("fcm_tokens", []),
            "storage_path": str(storage_path),
            "disk_mount": str(storage_path.parent)
        }
        # Generar reglas iniciales basadas en business_type + main_concerns
        initial_rules = _generate_initial_rules(business_type, main_concerns)
        if initial_rules:
            user_data["vigilance_rules"] = initial_rules
            user_data["vigilance_prompt"] = _build_initial_prompt(business_type, main_concerns, initial_rules, schedule_open, schedule_close)
            user_data["rules_es"] = _generate_rules_es(business_type, main_concerns)
        storage_path.mkdir(parents=True, exist_ok=True)
        storage_path.mkdir(parents=True, exist_ok=True)
        with _get_user_lock(uid):  # C1
            _atomic_write_user_json(storage_path / "user.json", user_data)
            compat_dir = user_root(uid)
            compat_dir.mkdir(parents=True, exist_ok=True)
            _atomic_write_user_json(compat_dir / "user.json", user_data)
        return {
            "success": True,
            "user_id": uid,
            "email": email,
            "name": user_data["name"],
            "business_name": business_name,
            "plan": plan,
            "plan_end": plan_end,
            "access_token": access_token
        }
    except Exception as e:
        logger.error(f"Firebase verify error: {e}")
        return {"success": False, "error": str(e)}


# ═══════════════════════════════════════════════════════════════════════════
# FCM Token Registration
# ═══════════════════════════════════════════════════════════════════════════

@app.get("/api/push-config")
async def get_push_config():
    """RD-5: configuración pública del push web (vapidKey de Firebase
    Console → ojoia.env PUSH_VAPID_KEY). Sin vapid, Firebase 10.x
    rechaza getToken silenciosamente → nunca se registra el teléfono."""
    vk = os.environ.get("PUSH_VAPID_KEY", "")
    return {"vapid_key": vk, "public": bool(vk)}


@app.post("/api/fcm/register")
async def register_fcm_token(request: dict, authorization: str = Header(None, alias="Authorization")):
    """Registra token FCM para push notifications."""
    try:
        user_id = request.get("user_id", "") if isinstance(request, dict) else ""
        await _verify_user_token(authorization, user_id)  # A1: anti-suplantacion de user_id en body
        fcm_token = request.get("fcm_token", "") if isinstance(request, dict) else ""
        if isinstance(request, str):
            try:
                request = json.loads(request)
                user_id = request.get("user_id", "")
                fcm_token = request.get("fcm_token", "")
            except Exception as e_1989:
                # P0 (Sección #8): loggeo en lugar de pass silencioso
                logger.warning(f"[fcm-register] body parse failed: {e_1989}")
        if not user_id or not fcm_token:
            raise HTTPException(status_code=400, detail="user_id and fcm_token required")
        # RD-7: update_user_json = lock + atómico + DUAL-WRITE (ambos discos)
        def _mut_tok(ud):
            toks = ud.setdefault("fcm_tokens", [])
            if fcm_token not in toks:
                toks.append(fcm_token)
        update_user_json(user_id, _mut_tok)
        logger.info(f"FCM token registered for user {user_id}")
        return {"success": True}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"FCM register error: {e}")
        return {"success": False, "error": str(e)}


@app.delete("/api/fcm/unregister")
async def unregister_fcm_token(request: dict):
    """Elimina token FCM del usuario (logout o cambio de dispositivo)."""
    try:
        user_id = request.get("user_id", "") if isinstance(request, dict) else ""
        fcm_token = request.get("fcm_token", "") if isinstance(request, dict) else ""
        if not user_id or not fcm_token:
            raise HTTPException(status_code=400, detail="user_id and fcm_token required")
        # RD-7: mismo patrón dual-write
        def _mut_rm(ud):
            toks = ud.get("fcm_tokens", [])
            if fcm_token in toks:
                toks.remove(fcm_token)
                ud["fcm_tokens"] = toks
        update_user_json(user_id, _mut_rm)
        return {"success": True}
    except HTTPException:
        raise
    except Exception as e:
        return {"success": False, "error": str(e)}


# ─────────────────────────────────────────────────────────────────────────
# GET del historial de chat con Eva — SIEMPRE devuelve la sesión unificada.
# Ver helpers de consolidación al inicio del archivo (_eva_unified_sid,
# _consolidate_legacy_eva_sessions).
# ─────────────────────────────────────────────────────────────────────────

@app.get("/api/eva/pending-cameras")
async def eva_pending_cameras(user_id: str, max_age_s: int = 300,
                              authorization: str = Header(None)):
    """Cámaras nuevas detectadas por announce pero aún sin reclamar.
    Lo usa la app y el wizard de Eva (telemetría de "instalar cámara").
    La entrada sale de la cola al hacer claim (registro real del camera_id)."""
    await _verify_user_token(authorization, user_id)
    from eva.pending_cameras import list_recent
    pending = list_recent(max_age_s)
    return {"success": True, "count": len(pending), "pending_cameras": pending}


@app.get("/api/chat/eva/history")
async def get_eva_chat_history(user_id: str, session_id: Optional[str] = None, limit: int = 50):
    """Historial de mensajes del chat con Eva.

    UN SOLO CHAT: siempre devuelve los mensajes de la sesión unificada
    "chat_<uid>", consolidando el historial legacy la primera vez que se
    lee. Antes, cuando se llamaba sin session_id, JUNTABA TODAS las sesiones
    (os_<uid>, chat_<uid>_<ts>, eva_<uid>_single, etc.) lo que mezclaba chats
    distintos y duplicaba mensajes.
    """
    try:
        if not user_id:
            raise HTTPException(status_code=400, detail="user_id required")
        unified_sid = _eva_unified_sid(user_id)

        uf = find_user_json(user_id)
        if uf and uf.exists():
            with _get_user_lock(user_id):
                with open(uf) as f:
                    ud = json.load(f)
                # Consolidar legacy la primera vez (idempotente).
                try:
                    _consolidate_legacy_eva_sessions(ud, user_id)
                except Exception as ce:
                    logger.warning(f"[history] consolidate legacy: {ce}")
                sessions = ud.get("eva_sessions", {}) or {}
                # A partir de ahora SIEMPRE la sesión unificada, sin importar
                # qué session_id pidió el frontend (lo normalizamos abajo).
                # Si el frontend pidió un sid legacy, lo redirigimos al unificado.
                effective_sid = unified_sid
                if session_id and session_id != unified_sid:
                    # Redirigir: si la sesión pedida existe aún y tiene mensajes
                    # que no están en la unificada (caso raro: no alcanzó a migrar),
                    # consolidamos de nuevo y seguimos.
                    logger.info(f"[history] redirigiendo sid legacy {session_id} -> {unified_sid}")
                sess = sessions.get(effective_sid, {}) or {}
                history = list(sess.get("messages", []) or [])[-limit:] if limit else list(sess.get("messages", []) or [])
                # Guardar cambios si la consolidación migró algo
                _atomic_write_user_json(uf, ud)

        else:
            return {"success": True, "history": [], "count": 0, "user_id": user_id,
                    "session_id": unified_sid, "ts": 0}

        # ts de la última actualización (server side)
        server_ts = 0
        try:
            if uf and uf.exists():
                with open(uf) as f:
                    _ud_ts = json.load(f)
                _sessions_ts = _ud_ts.get("eva_sessions", {}) or {}
                _lm = int(_sessions_ts.get(unified_sid, {}).get("last_message_at", 0) or 0)
                server_ts = _lm
            if not server_ts:
                chat_history_file = user_root(user_id) / "eva_chat_history.json"
                if chat_history_file.exists():
                    server_ts = int(chat_history_file.stat().st_mtime)
        except Exception:
            logger.debug("silent: {exc}", exc=Exception)


        return {
            "success": True,
            "history": history,
            "count": len(history),
            "user_id": user_id,
            "session_id": unified_sid,
            "ts": server_ts
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error get_eva_chat_history: {e}")
        return {"success": False, "error": str(e), "history": [], "ts": 0}


# Alias POST para /api/chat/eva/history (compatibilidad con frontend)
@app.post("/api/chat/eva/history")
async def get_eva_chat_history_post(request: dict, authorization: str = Header(None, alias="Authorization")):
    """POST = guardar historial completo (compatibilidad con eva-chat-v5.js).
    GET (definido arriba) = leer historial.

    F1+2 fix: ahora persiste los campos completos (events, summary por msg,
    image_url, image_b64, is_daily_report, report_url) para que el carrusel
    y los reportes sobrevivan la reload desde backend. Y hace MERGE (no
    replace): solo anade mensajes con ts > last_message_at del server.
    """
    try:
        user_id = request.get("user_id", "")
        await _verify_user_token(authorization, user_id)  # A1: validar token vs user_id del body
        if not user_id:
            return {"success": False, "error": "user_id required"}
        uf = find_user_json(user_id)
        if not uf or not uf.exists():
            return {"success": True, "history": [], "count": 0, "ts": 0}
        history = request.get("history", []) or []
        summary = request.get("summary", "")

        # ── UN SOLO CHAT: SIEMRE normalizar al session_id unificado "chat_<uid>" ──
        # Antes se forzaba SIEMPRE "os_<user_id>", lo que (a) ignoraba el sid del
        # frontend y (b) bifurcaba el historial. El frontend ahora manda SIEMPRE
        # "chat_<uid>" (ver eva-chat-v5.js). Igual lo normalizamos acá para no
        # depender del frontend: cualquier sid legacy se mapea al unificado.
        requested_sid = request.get("session_id", "") or ""
        unified_sid = _eva_unified_sid(user_id)
        if requested_sid != unified_sid:
            if requested_sid:
                logger.info(f"[history-POST] normalizando sid {requested_sid} -> {unified_sid}")
            session_id = unified_sid
        else:
            session_id = unified_sid

        with _get_user_lock(user_id):
            with open(uf) as f:
                ud = json.load(f)
            # Consolidar legacy la primera vez (idempotente) antes de tocar.
            try:
                _consolidate_legacy_eva_sessions(ud, user_id)
            except Exception as ce:
                logger.warning(f"[history-POST] consolidate legacy: {ce}")
            sessions = ud.get("eva_sessions", {}) or {}
            existing = sessions.get(session_id, {}) or {}
            existing_msgs = existing.get("messages", []) or []
            last_message_at = int(existing.get("last_message_at", 0) or 0)

            # MERGE: agregar solo mensajes nuevos (timestamp > last_message_at).
            # Para detectar nuevos, usamos el timestamp del msg si viene del
            # frontend; si no (no trae), usamos ts=ahora y lo anadimos.
            # Ademas preservamos todos los fields que el frontend envia (events,
            # summary, image_url, image_b64, is_daily_report, report_url).
            now_ts = int(time.time())
            existing_keys = set()
            existing_content_keys = set()  # (role, content) sin timestamp — anti-dup de greetings
            for m in existing_msgs:
                key = (m.get("role"), m.get("content"), m.get("timestamp", 0))
                existing_keys.add(key)
                # Guardar tambien (role, content) para detectar greetings duplicados
                # que llegan con timestamp distinto cada vez
                ck = (m.get("role"), m.get("content"))
                if m.get("summary") or (m.get("role") == "assistant" and m.get("content","").startswith(("Hola", "¡Hola", "Resumen del día"))):
                    if ck not in existing_content_keys:
                        # Solo marcar como "reciente" si el msg es de los ultimos 30 min
                        msg_age = now_ts - int(m.get("timestamp", 0) or 0)
                        if msg_age < 1800:
                            existing_content_keys.add(ck)

            merged = list(existing_msgs)
            for h in history:
                if not isinstance(h, dict):
                    continue
                content = h.get("content")
                if content is None:
                    continue
                msg_ts = h.get("timestamp") or now_ts
                try:
                    msg_ts = int(float(msg_ts))
                except Exception:
                    msg_ts = now_ts
                key = (h.get("role", "user"), content, msg_ts)
                if key in existing_keys:
                    continue
                # ANTI-SPAM: si es un greeting/summary con el MISMO contenido (role+content)
                # que ya existe en los ultimos 30 min, saltar — previene saludos duplicados
                ck = (h.get("role", "user"), content)
                if h.get("summary") and ck in existing_content_keys:
                    continue
                # Anadir solo si msg_ts > last_message_at o si no hay msgs aun
                if existing_msgs and msg_ts <= last_message_at:
                    continue
                saved_msg = {
                    "role": h.get("role", "user"),
                    "content": str(content),
                    "timestamp": msg_ts,
                }
                # Preservar fields opcionales (carrusel + reportes + imagenes)
                for opt_key in ("events", "summary", "is_daily_report", "report_url",
                                "image_url", "image_b64", "heatmap", "heatmap_meta"):
                    if h.get(opt_key) is not None:
                        saved_msg[opt_key] = h.get(opt_key)
                merged.append(saved_msg)
                existing_keys.add(key)

            # Limitar a ultimos 200
            merged = merged[-200:]
            new_last_message_at = max([int(m.get("timestamp", 0) or 0) for m in merged] or [now_ts])

            sessions[session_id] = {
                "messages": merged,
                "summary": summary or existing.get("summary", ""),
                "created_at": existing.get("created_at", now_ts),
                "last_message_at": new_last_message_at,
            }
            ud["eva_sessions"] = sessions
            _atomic_write_user_json(uf, ud)
        logger.info(f"[EVA] Historial guardado: {len(merged)} msgs para {user_id} sid={session_id} (last_ts={new_last_message_at})")
        return {
            "success": True,
            "history": merged[-50:],
            "count": len(merged),
            "saved": True,
            "user_id": user_id,
            "session_id": session_id,
            "ts": new_last_message_at
        }
    except Exception as e:
        logger.error(f"[EVA POST history] {e}")
        return {"success": False, "error": str(e), "history": [], "ts": 0}


@app.post("/api/chat/eva/save")
async def save_eva_chat_message(request: dict, authorization: str = Header(None, alias="Authorization")):
    """Guarda un mensaje del chat con Eva en user.json."""
    try:
        user_id = request.get("user_id", "")
        await _verify_user_token(authorization, user_id)  # A1: validar token vs user_id del body
        session_id = request.get("session_id", "")
        role = request.get("role", "user")
        content = request.get("content", "")
        timestamp = request.get("timestamp") or int(time.time())
        if not user_id or not session_id:
            raise HTTPException(status_code=400, detail="user_id and session_id required")
        uf = find_user_json(user_id)
        if not uf or not uf.exists():
            return {"success": False, "error": "user not found"}
        # S4: lock durante read+mutate+write (race vs thread daemon chat_eva_message)
        with _get_user_lock(user_id):
            with open(uf) as f:
                ud = json.load(f)
            sessions = ud.get("eva_sessions", {}) or {}
            if session_id not in sessions:
                sessions[session_id] = {
                    "messages": [],
                    "created_at": timestamp,
                    "last_message_at": timestamp
                }
            sessions[session_id]["messages"].append({
                "role": role,
                "content": content,
                "timestamp": timestamp
            })
            sessions[session_id]["last_message_at"] = timestamp
            # Mantener solo últimos 100 mensajes por sesión
            sessions[session_id]["messages"] = sessions[session_id]["messages"][-100:]
            ud["eva_sessions"] = sessions
            _atomic_write_user_json(uf, ud)
        return {"success": True, "saved": True}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error save_eva_chat_message: {e}")
        return {"success": False, "error": str(e)}


@app.post("/api/chat/eva/feedback")
async def eva_event_feedback(request: dict, authorization: str = Header(None, alias="Authorization")):
    """Feedback del usuario sobre un evento (falsa alarma / confirmación).

    Lo invoca el frontend desde el widget del evento y, opcionalmente, desde
    el chat de Eva. Delega en tool_learn_from_feedback, que:
      - persiste feedback en el JSON del evento
      - si is_real=False: añade nota como owner_note y registra false_alarm
    Body: {user_id, event_id, is_real, notes?, correction_note?}
    """
    try:
        user_id = request.get("user_id", "")
        await _verify_user_token(authorization, user_id)  # A1: validar token vs user_id del body
        event_id = request.get("event_id", "")
        is_real = bool(request.get("is_real", True))
        notes = request.get("notes") or ""
        correction_note = request.get("correction_note") or ""
        if not user_id or not event_id:
            return {"success": False, "error": "user_id and event_id required"}
        from eva.tools import tool_learn_from_feedback
        result = await tool_learn_from_feedback(
            event_id=event_id, is_real=is_real,
            notes=(notes.strip() or None), user_id=user_id,
            correction_note=(correction_note.strip() or None),
        )
        if not result.get("success"):
            logger.warning(f"[EVA feedback] {result.get('error', 'error')} (event={event_id})")
        else:
            logger.info(f"[EVA feedback] user={user_id[:8]} event={event_id[:18]} is_real={is_real} action={result.get('action')}")
        return result
    except Exception as e:
        logger.error(f"[EVA feedback] error: {e}")
        return {"success": False, "error": str(e)}


# ═══════════════════════════════════════════════════════════
# EVA CHAT - Endpoint principal de conversación
# ═══════════════════════════════════════════════════════════
@app.post("/api/chat/eva/message")
async def chat_eva_message(request: dict, authorization: str = Header(None, alias="Authorization")):
    """Endpoint principal del chat con Eva.
    
    Body:
        user_id: str (requerido)
        message: str (requerido) - mensaje del usuario
        session_id: str - ID de sesión (se crea si no existe)
        cam_id: str - cámara objetivo (opcional)
        include_frame: bool - incluir frame actual (default True)
    
    Returns:
        success: bool
        sessionId / session_id: str (compatibilidad)
        phase: str - estado actual de la sesión
        response: str - texto de Eva
        messages: array - historial completo
        image_url: str - URL de imagen si hay
        suggestions: array - opciones rápidas
        ready_to_confirm: bool
        camera_saved: bool
        events_found: array
    """
    try:
        user_id = (request.get("user_id") or "").strip()
        await _verify_user_token(authorization, user_id)  # A1: validar token vs user_id del body
        message = (request.get("message") or "").strip()
        session_id = request.get("session_id") or ""
        cam_id = request.get("cam_id") or ""
        include_frame = bool(request.get("include_frame", True))
        
        if not user_id:
            return {"success": False, "error": "user_id required"}
        if not message:
            return {"success": False, "error": "message required"}
        
        # Cachear sesión en user.json si existe
        uf = find_user_json(user_id)
        ud = {}
        if uf and uf.exists():
            try:
                with open(uf) as f:
                    ud = json.load(f)
            except Exception:
                ud = {}
        
        # ── UN SOLO CHAT: SIEMPE usar el session_id unificado "chat_<uid>" ──
        # Antes: si no venía session_id, se generaba "chat_<uid>_<ts>" nuevo en
        # cada mensaje, bifurcando el historial. Si venía uno legacy, también se
        # bifurcaba. Ahora se normaliza SIEMPRE a "chat_<uid>".
        unified_sid = _eva_unified_sid(user_id)
        if not session_id or session_id != unified_sid:
            if session_id and session_id != unified_sid:
                logger.info(f"[EVA] normalizando session_id {session_id} -> {unified_sid}")
            session_id = unified_sid
        
        logger.info(f"[EVA] user={user_id} session={session_id} msg={message[:80]}")
        
        # Llamar al motor Eva v2 primero (tiene las herramientas nuevas: count_people, is_open_hours, etc.)
        result = None
        try:
            from eva_v2 import handle_eva_v2
            result = await handle_eva_v2(
                user_id=user_id,
                message=message,
                session_id=session_id,
                cam_id=cam_id or None,
                include_frame=include_frame,
                storage_root=STORAGE_ROOT
            )
        except Exception as e1:
            import traceback
            logger.exception(f"[EVA] handle_eva_v2 falló: {e1}")
            logger.error(f"[EVA] TRACEBACK: {traceback.format_exc()}")
            from eva_v2 import _make_os_session, _load_session, _mk_resp as _eva_mk_resp
            # P0 (Bug #1): _load_session PUEDE retornar None si user.json está
            # corrupto/ausente o si la sesión en disco falla el parseo. Antes
            # pasábamos `session` (potencialmente None) a _eva_mk_resp, que
            # entonces crasheaba con TypeError: 'NoneType' object does not
            # support item assignment en eva_v2._mk_resp (session["image_url"]).
            # Ahora garantizamos que `session` SIEMPRE sea un dict válido.
            session = None
            try:
                session = _load_session(session_id)
            except Exception as e_ls:
                logger.error(f"[EVA] _load_session falló en fallback: {e_ls}")
                session = None
            if not isinstance(session, dict) or (session and session.get("user_id") != user_id):
                try:
                    session = _make_os_session(user_id, session_id)
                except Exception as e_mk:
                    logger.error(f"[EVA] _make_os_session falló en fallback: {e_mk}")
                    session = None
            if not isinstance(session, dict) or not session:
                # Última barrera: sesión sintética mínima. NO usar None.
                session = {
                    "session_id": session_id, "user_id": user_id, "phase": "os",
                    "owner_name": "amigo", "msgs": [],
                    "image_b64": "", "image_url": "", "image_sent": True,
                    "last_event_id": None, "last_event_camera_id": "",
                    "zone": "", "has_image": False, "camera_id": "",
                    "business_name": "", "business_type": "", "owner": "",
                }
            try:
                from eva_v2 import _sessions
                _sessions[session_id] = session
            except Exception as e_2440:
                # P0 (Sección #8): loggeo en lugar de pass silencioso
                logger.warning(f"[eva-session-create] {session_id} save failed: {e_2440}")
            result = _eva_mk_resp(
                session,
                f"Lo siento, tuve un problema técnico procesando eso. Intenta reformularlo, por favor.",
                suggestions=["Qué ha pasado hoy", "Cuántas personas han venido hoy", "Muéstrame el pico"]
            )
            result["sessionId"] = session_id
        
        # Normalizar respuesta: SIEMRE devolver el sessionId unificado.
        result["sessionId"] = unified_sid
        result["session_id"] = unified_sid
        result.setdefault("response", "")
        result.setdefault("image_url", "")
        result.setdefault("phase", "os")
        result.setdefault("suggestions", [])
        result.setdefault("events_found", [])
        result.setdefault("ready_to_confirm", False)
        result.setdefault("camera_saved", False)
        
        # Construir historial de mensajes para el frontend. UN SOLO CHAT:
        # siempre desde la sesión unificada "chat_<uid>".
        msgs_session_id = unified_sid
        messages = []
        # Cargar mensajes previos si existen
        if uf and uf.exists():
            with _get_user_lock(user_id):
                with open(uf) as f:
                    ud = json.load(f)
                # Consolidar legacy la primera vez.
                try:
                    _consolidate_legacy_eva_sessions(ud, user_id)
                except Exception as e_2473:
                    # P0 (Sección #8): loggeo en lugar de pass silencioso
                    logger.warning(f"[legacy-consolidate] {user_id} failed: {e_2473}")
                sessions = ud.get("eva_sessions", {}) or {}
                sess = sessions.get(msgs_session_id, {}) or {}
                prev = sess.get("messages", []) or []
                messages = prev[-50:]
                # Guardar de inmediato el estado consolidado (sin el msg nuevo aún).
                _atomic_write_user_json(uf, ud)
        # Agregar mensaje actual del usuario
        messages.append({"role": "user", "content": message})
        # Agregar respuesta de Eva
        messages.append({"role": "assistant", "content": result["response"]})
        result["messages"] = messages
        
        # Guardar en user.json bajo el sid unificado (best-effort, lock + atomic)
        if uf and uf.exists():
            try:
                msgs_session_id_local = msgs_session_id
                messages_local = list(messages)
                def _save_async():
                    try:
                        with _get_user_lock(user_id):
                            with open(uf) as f:
                                ud_fresh = json.load(f)
                            sessions = ud_fresh.get("eva_sessions", {}) or {}
                            if not isinstance(sessions, dict):
                                sessions = {}
                            if msgs_session_id_local not in sessions or not isinstance(sessions.get(msgs_session_id_local), dict):
                                sessions[msgs_session_id_local] = {
                                    "messages": [],
                                    "created_at": int(time.time()),
                                    "last_message_at": int(time.time())
                                }
                            sessions[msgs_session_id_local]["messages"] = messages_local[-100:]
                            sessions[msgs_session_id_local]["last_message_at"] = int(time.time())
                            ud_fresh["eva_sessions"] = sessions
                            _atomic_write_user_json(uf, ud_fresh)
                    except Exception:
                        logger.debug("silent: {exc}", exc=Exception)

                import threading
                threading.Thread(target=_save_async, daemon=True).start()
            except Exception as e:
                logger.warning(f"[EVA] No se pudo guardar en user.json: {e}")
        
        return result
        
    except Exception as e:
        logger.error(f"[EVA] Error en chat_eva_message: {e}")
        return {"success": False, "error": str(e), "response": "Error procesando mensaje"}


# Alias para compatibilidad con frontend actual (/config/chat)
# A2 fix (2026-08-09): el alias antes NO extraia el header Authorization y lo
# pasaba como None a chat_eva_message -> _verify_user_token(None, user_id) ->
# 401 "Authorization requerido" -> success:false "Error procesando mensaje".
# El navegador usa /config/chat (eva-chat-v6.js:450), NO /api/chat/eva/message,
# asi que el chat de Eva entero estaba roto por este alias. Ahora extraemos el
# header del Request y lo pasamos explicito.
@app.post("/config/chat")
async def chat_config_alias(request: dict, authorization: str = Header(None, alias="Authorization")):
    """Alias del endpoint de chat para compatibilidad con versiones anteriores."""
    return await chat_eva_message(request, authorization)


# --- UTIL FUNCTIONS NEW ---
def _resolve_user_events_dir(user_id: str) -> Path:
    """Resuelve directorio events para un user."""
    root_dir = Path("/home/sam/storage/users")
    if not user_id: return root_dir
    
    candidates = [
        (f"{root_dir}/{user_id}/cameras", "default_cams"),
        (f"{root_dir}/{user_id}/per_cams", "legacy"),
        (f"{root_dir}/{user_id}", "single")
    ]
    
    for (path, strategy) in candidates:
        if Path(path).exists():
            return Path(path)
    
    return root_dir


def _sum_people_in_events(events_dir: Path, camera_id: str = None, start_ts: float = None, end_ts: float = None) -> dict:
    """Suma personas en eventos JSON por directorio."""
    total_people = 0
    timestamps_present = []
    start_ts = start_ts or time.time() - 172800  # 48h
    end_ts = end_ts or time.time()
    
    lookup_dirs = [events_dir]
    if camera_id:
        lookup_dirs = [events_dir / cam_id / "events" for cam_id in [camera_id] if (events_dir / cam_id / "events").exists()]
    
    for events_subdir in lookup_dirs:
        if not events_subdir.exists():
            continue
            
        for evt_file in events_subdir.glob("evt_*.json"):
            try:
                data = json.load(open(evt_file))
                ts = data.get("timestamp_created", 0) or data.get("timestamp", 0)
                
                # Solo contar eventos en rango
                if start_ts <= ts <= end_ts:
                    # Buscar conteo: YOLO→Qwen→default
                    count = (
                        data.get("persons") or
                        data.get("yolo", {}).get("count", 0) or
                        data.get("qwen_json", {}).get("persons", 0) or
                        0
                    )
                    
                    if count and count > 0:
                        total_people += count
                        timestamps_present.append({
                            "timestamp": ts,
                            "datetime": datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S"),
                            "people": count,
                            "event_id": evt_file.stem
                        })
            except:
                continue
                
    return {
        "total_people": total_people,
        "timestamps_present": sorted(timestamps_present, key=lambda x: x["timestamp"])
    }


# ════════════════════════
# ENDPOINT: PERSPECTIVA
# ════════════════════════
@app.get("/api/cameras/{camera_id}/count_people")
async def count_people(
    user_id: str,
    camera_id: str,
    start: Optional[float] = Query(None, description="Timestamp inicio (default: ahora-48h)"),
    end: Optional[float] = Query(None, description="Timestamp fin (default: ahora)")
):
    """Endpoint: Cuántas personas aparecieron entre start-end en camera_id.
    Usa tracker temporal: sesiones separadas por > 5 minutos = visitas distintas."""
    if not user_id:
        return {"success": False, "error": "user_id required", "total_people": 0}
    
    from eva.tools import tool_count_people
    
    # Determinar date
    date_param = "today"
    if start or end:
        date_param = None
    
    result = await tool_count_people(
        user_id=user_id,
        camera_id=camera_id,
        date=date_param,
        start=start,
        end=end
    )
    
    return {
        "success": True,
        "user_id": user_id,
        "camera_id": camera_id,
        "total_people": result.get("total_people", 0),
        "sessions": result.get("sessions", 0),
        "events_count": result.get("events_count", 0),
        "peak_count": result.get("peak_count", 0),
        "peak_time": result.get("peak_time", ""),
        "cameras": result.get("cameras", []),
        "tracker_version": "v1_time_based",
        "request": {"start": start, "end": end}
    }


# ════════════════════════
# ENDPOINTS: ZONAS DE INTERÉS (ROI)
# ════════════════════════
@app.get("/api/cameras/{camera_id}/zones")
async def get_camera_zones_endpoint(camera_id: str, user_id: str):
    """Lee las zonas de interés dibujadas para una cámara."""
    if not user_id:
        return {"success": False, "error": "user_id required", "zones": []}
    try:
        zones = camera_zones.get_camera_zones(user_id, camera_id)
        zone_types = camera_zones.get_zone_types()
        return {
            "success": True,
            "user_id": user_id,
            "camera_id": camera_id,
            "zones": zones,
            "zone_types": zone_types,
        }
    except Exception as e:
        return {"success": False, "error": str(e), "zones": []}


@app.post("/api/cameras/{camera_id}/zones")
async def save_camera_zones_endpoint(camera_id: str, user_id: str, request: Request):
    """Guarda/actualiza las zonas de interés dibujadas para una cámara.
    Body: {"zones": [...]} o {"zone": {...}} (single add).
    """
    if not user_id:
        return {"success": False, "error": "user_id required"}
    try:
        body = await request.json()
        # Modo 1: recibe lista completa de zonas
        if isinstance(body, dict) and "zones" in body and isinstance(body["zones"], list):
            ok = camera_zones.save_camera_zones(user_id, camera_id, body["zones"])
            return {
                "success": ok,
                "user_id": user_id,
                "camera_id": camera_id,
                "zones": body["zones"] if ok else [],
            }
        # Modo 2: recibe una sola zona para agregar/actualizar
        if isinstance(body, dict) and "zone" in body and isinstance(body["zone"], dict):
            saved = camera_zones.add_or_update_zone(user_id, camera_id, body["zone"])
            return {
                "success": bool(saved),
                "user_id": user_id,
                "camera_id": camera_id,
                "zone": saved,
            }
        # Fallback: body es directamente la lista
        if isinstance(body, list):
            ok = camera_zones.save_camera_zones(user_id, camera_id, body)
            return {
                "success": ok,
                "user_id": user_id,
                "camera_id": camera_id,
                "zones": body if ok else [],
            }
        return {"success": False, "error": "Body debe tener 'zones' (list) o 'zone' (dict)"}
    except Exception as e:
        return {"success": False, "error": str(e)}


@app.delete("/api/cameras/{camera_id}/zones/{zone_id}")
async def delete_camera_zone_endpoint(camera_id: str, user_id: str, zone_id: str):
    """Elimina una zona por su ID."""
    if not user_id:
        return {"success": False, "error": "user_id required"}
    try:
        ok = camera_zones.delete_zone(user_id, camera_id, zone_id)
        return {
            "success": ok,
            "user_id": user_id,
            "camera_id": camera_id,
            "zone_id": zone_id,
            "remaining_zones": camera_zones.get_camera_zones(user_id, camera_id) if ok else [],
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


@app.get("/api/zone-types")
async def get_zone_types_endpoint():
    """Lista los 15 tipos de zona disponibles (entrance, cashier, kitchen, etc.)."""
    return {"success": True, "zone_types": camera_zones.get_zone_types()}


# ═══════════════════════════════════════════════════════════════════════════
# C1.3 — Sugerir zonas con IA (WOW #2)
# ═══════════════════════════════════════════════════════════════════════════
# Endpoint que toma el último frame de la cámara y pide a Qwen que sugiera
# zonas de interés (ROI) con coords relativas 0-1, listas para el drawer.

_QWEN_SUGGEST_ZONES_PROMPT = (
    "Eres un asistente de instalación de cámaras de seguridad. Analiza esta imagen "
    "y sugiere entre 3 y 6 zonas de interés (ROI) para vigilar. "
    "Para cada zona, identifica: tipo (entrance/cashier/kitchen/dining/inventory/"
    "counter/hall/parking/restricted/office/storage/hallway/production/other), "
    "un nombre descriptivo, y coordenadas relativas (0-1) como {x, y, w, h} donde "
    "(x,y) es la esquina superior izquierda y (w,h) el ancho y alto.\n"
    "Devuelve SOLO JSON:\n"
    '{"zones":[{"type":"cashier","name":"Mostrador principal","coords":{"x":0.35,"y":0.20,"w":0.40,"h":0.50}}, ...]}\n'
    "Las coordenadas deben ser entre 0 y 1, reflejando la posición real en la imagen. "
    "Enfoca zonas críticas: entradas, cajas, almacenes, áreas restringidas, zonas de tráfico."
)


async def _suggest_zones_with_qwen(image_b64: str, zone: str = "", biz_type: str = "") -> list:
    """Pide a Qwen que sugiera zonas de interés para el frame actual."""
    try:
        import base64 as _b64
        raw = _b64.b64decode(image_b64)
        # Redimensionar a 640px de ancho para ahorrar tokens
        from PIL import Image
        import io
        img = Image.open(io.BytesIO(raw))
        img.thumbnail((640, 640), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=80)
        small_b64 = _b64.b64encode(buf.getvalue()).decode()

        ctx = ""
        if zone: ctx += f"La cámara está en: {zone}. "
        if biz_type: ctx += f"Negocio: {biz_type}. "
        prompt = _QWEN_SUGGEST_ZONES_PROMPT + f" {ctx}"

        msgs = [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{small_b64}"}},
            {"type": "text", "text": prompt}]}]

        async with httpx.AsyncClient(timeout=60) as cl:
            r = await cl.post("http://localhost:8004/v1/chat/completions",
                              json={"model": "qwen", "messages": msgs, "max_tokens": 500, "temperature": 0.2})
            if r.status_code != 200:
                return []
            data = r.json()
            content = data.get("choices", [{}])[0].get("message", {}).get("content", "")

        # Parsear JSON de la respuesta
        import re as _re
        content = _re.sub(r'^```json\s*', '', content)
        content = _re.sub(r'\s*```$', '', content).strip()
        m = _re.search(r'\{.*\}', content, _re.DOTALL)
        if not m:
            return []
        parsed = json.loads(m.group())
        zones = parsed.get("zones", [])
        if not isinstance(zones, list):
            return []

        # Normalizar cada zona
        result = []
        for i, z in enumerate(zones):
            if not isinstance(z, dict):
                continue
            coords = z.get("coords", {})
            if not all(k in coords for k in ("x", "y", "w", "h")):
                continue
            # Clamp coords a 0-1
            coords = {k: max(0.0, min(1.0, float(v))) for k, v in coords.items()}
            result.append({
                "id": f"sug_{i}_{int(time.time())}",
                "name": str(z.get("name", f"Zona {i+1}"))[:60],
                "type": str(z.get("type", "other"))[:30],
                "coords": coords,
                "color": _zone_color_for_type(z.get("type", "other")),
                "icon": _zone_icon_for_type(z.get("type", "other")),
                "suggested_by": "qwen",
                "created_at": time.time(),
            })
        return result
    except Exception as e:
        logger.error(f"Error sugiriendo zonas con Qwen: {e}")
        return []


def _zone_color_for_type(zone_type: str) -> str:
    """Color hex para un tipo de zona (mismo mapping que el drawer)."""
    color_map = {
        "entrance": "#2196f3", "cashier": "#ff9800", "register": "#ff9800",
        "kitchen": "#f44336", "dining": "#4caf50", "inventory": "#9c27b0",
        "counter": "#e91e63", "hall": "#607d8b", "parking": "#8bc34a",
        "restricted": "#f44336", "office": "#3f51b5", "storage": "#795548",
        "hallway": "#607d8b", "production": "#607d8b", "other": "#9e9e9e",
    }
    return color_map.get(zone_type, "#9e9e9e")


def _zone_icon_for_type(zone_type: str) -> str:
    """Icono para un tipo de zona (mismo mapping que camera_zones.get_zone_types)."""
    icon_map = {
        "entrance": "🚪", "cashier": "💰", "register": "🧾",
        "kitchen": "🍳", "dining": "🍽️", "inventory": "📦",
        "counter": "🛍️", "hall": "🏠", "parking": "🚗",
        "restricted": "🚫", "office": "💼", "storage": "📦",
        "hallway": "🚶", "production": "🏭", "other": "📍",
    }
    return icon_map.get(zone_type, "📍")


async def _get_latest_frame_b64(user_id: str, camera_id: str) -> str:
    """Obtiene el último frame de la cámara en base64 (misma lógica que /frames/latest)."""
    if not user_id or not camera_id:
        return ""
    try:
        grid = orchestrator._get_grid(user_id, camera_id)
        frame_bytes = grid.get_last_frame_bytes()
        last_cam = grid.get_last_camera_id()
        if last_cam and last_cam != camera_id:
            frame_bytes = b""
        if not frame_bytes:
            events_dir = user_root(user_id) / "cameras" / camera_id / "events"
            latest_vig = events_dir / "latest_vigilance.jpg"
            if latest_vig.exists():
                frame_bytes = latest_vig.read_bytes()
            else:
                frames_dir = user_root(user_id) / "cameras" / camera_id / "frames"
                latest_raw = frames_dir / "latest_raw.jpg"
                if latest_raw.exists():
                    frame_bytes = latest_raw.read_bytes()
        if frame_bytes:
            return base64.b64encode(frame_bytes).decode()
    except Exception as e_2871:
        # P0 (Sección #8): loggeo en lugar de pass silencioso
        logger.warning(f"[frame-b64] {user_id}/{camera_id} encode failed: {e_2871}")
    return ""


@app.post("/api/cameras/{camera_id}/suggest-zones")
async def suggest_zones_endpoint(camera_id: str, request: Request):
    """
    C1.3 — Sugiere zonas de interés con IA (WOW #2).

    Body opcional: {"user_id": "...", "zone": "...", "business_type": "..."}
    Devuelve: {"success": true, "zones": [...], "image_b64": "..."}
    """
    try:
        body = {}
        try:
            body = await request.json()
        except Exception:
            logger.debug("silent: {exc}", exc=Exception)

        user_id = body.get("user_id", "")
        zone = body.get("zone", "")
        biz_type = body.get("business_type", "")

        if not user_id or not camera_id:
            return {"success": False, "error": "user_id y camera_id requeridos", "zones": []}

        # Obtener el último frame
        image_b64 = await _get_latest_frame_b64(user_id, camera_id)
        if not image_b64:
            return {"success": False, "error": "Sin frame disponible para esta cámara", "zones": [], "image_b64": ""}

        # Pedir sugerencias a Qwen
        zones = await _suggest_zones_with_qwen(image_b64, zone, biz_type)
        return {
            "success": True,
            "zones": zones,
            "image_b64": image_b64,
            "count": len(zones),
            "suggested_by": "qwen",
        }
    except Exception as e:
        logger.error(f"Error en suggest-zones: {e}")
        return {"success": False, "error": str(e), "zones": [], "image_b64": ""}


# ═══════════════════════════════════════════════════════════════════════════
# FASE 1 — Placement Check: verificación de encuadre de la cámara
# ═══════════════════════════════════════════════════════════════════════════
# El usuario posiciona la cámara; Eva evalúa N frames consecutivos y devuelve
# score 0-100 + checklist + consejos accionables antes de pasar a zonas.

_QWEN_PLACEMENT_PROMPT = (
    "Eres un instalador experto de cámaras de seguridad evaluando el encuadre "
    "de una cámara recién instalada en un negocio. Analiza ESTA imagen (frame en vivo).\n\n"
    "Evalúa estos aspectos:\n"
    "1. nitidez: ¿la imagen es nítida o borrosa/atajada?\n"
    "2. obstruccion: ¿hay objetos bloqueando la lente (dedo, cable, reflejo, suciedad)?\n"
    "3. iluminacion: ¿demasiado oscuro, quemado por luz, o correcto?\n"
    "4. altura_angulo: ¿se verían caras y torsos (ideal) o solo coronas de cabeza (muy alta)?\n"
    "5. cobertura: ¿el área importante del negocio (caja/mostrador/entrada) ocupa buena parte del encuadre?\n"
    "6. estabilidad: ¿la cámara apunta donde debe o está torcida/girada?\n\n"
    "Responde SOLO JSON válido:\n"
    '{"score": <0-100>, '
    '"items": [{"key": "nitidez", "ok": true|false, "detail": "1 frase corta en español"}, '
    '{"key": "obstruccion", ...}, {"key": "iluminacion", ...}, '
    '{"key": "altura_angulo", ...}, {"key": "cobertura", ...}, {"key": "estabilidad", ...}], '
    '"consejo": "1-2 frases con la acción más importante para mejorar el encuadre, en español", '
    '"veredicto": "perfecto" | "aceptable" | "mejorar"}\n'
    "score>=85 = perfecto, 60-84 = aceptable, <60 = mejorar. Sé estricto pero justo."
)


async def _placement_check_with_qwen(frames_b64: list, zone: str = "", biz_type: str = "") -> dict:
    """Evalúa el encuadre con Qwen usando 2-3 frames (el primero + intermedio + último)."""
    if not frames_b64:
        return {}
    # Muestrear hasta 3 frames representativos
    idxs = sorted({0, len(frames_b64) // 2, len(frames_b64) - 1})
    sample = [frames_b64[i] for i in idxs]

    ctx = ""
    if zone:
        ctx += f"La cámara debe cubrir: {zone}. "
    if biz_type:
        ctx += f"Tipo de negocio: {biz_type}. "

    content = []
    for fb in sample:
        content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{fb}"}})
    content.append({"type": "text", "text": _QWEN_PLACEMENT_PROMPT + " " + ctx})

    try:
        async with httpx.AsyncClient(timeout=60) as cl:
            r = await cl.post("http://localhost:8004/v1/chat/completions",
                              json={"model": "qwen", "messages": [{"role": "user", "content": content}],
                                    "max_tokens": 600, "temperature": 0.2})
            if r.status_code != 200:
                return {}
            raw = r.json()["choices"][0]["message"]["content"]
        import re as _re
        raw = _re.sub(r'^```json\s*', '', raw.strip())
        raw = _re.sub(r'\s*```$', '', raw).strip()
        m = _re.search(r'\{.*\}', raw, _re.DOTALL)
        if not m:
            return {}
        parsed = json.loads(m.group())
        # Normalizar
        score = parsed.get("score")
        try:
            score = int(max(0, min(100, int(score))))
        except (TypeError, ValueError):
            score = 0
        items = []
        if isinstance(parsed.get("items"), list):
            for it in parsed["items"]:
                if isinstance(it, dict) and it.get("key"):
                    items.append({
                        "key": str(it.get("key", ""))[:20],
                        "ok": bool(it.get("ok", False)),
                        "detail": str(it.get("detail", ""))[:120],
                    })
        veredicto = parsed.get("veredicto", "mejorar")
        if veredicto not in ("perfecto", "aceptable", "mejorar"):
            veredicto = "mejorar" if score < 60 else ("aceptable" if score < 85 else "perfecto")
        return {
            "score": score,
            "items": items,
            "consejo": str(parsed.get("consejo", ""))[:300],
            "veredicto": veredicto,
        }
    except Exception as e:
        logger.error(f"Placement check Qwen error: {e}")
        return {}


async def _collect_camera_frames(user_id: str, camera_id: str, n_frames: int = 3,
                                 interval_s: float = 2.5) -> list:
    """Recolecta N frames distintos de la cámara (por mtime de latest_vigilance/latest_raw).

    Lee el archivo de disco en intervalos: si la cámara transmite, el archivo
    cambia entre lecturas; si no cambia, se deduplica por hash.
    """
    import hashlib
    frames = []
    seen_hashes = set()
    for _ in range(max(1, n_frames) * 3):  # reintentos para conseguir n_frames distintos
        b64 = await _get_latest_frame_b64(user_id, camera_id)
        if b64:
            h = hashlib.md5(b64.encode()).hexdigest()[:12]
            if h not in seen_hashes:
                seen_hashes.add(h)
                frames.append(b64)
                if len(frames) >= max(1, n_frames):
                    break
        await asyncio.sleep(interval_s)
    # Si no logramos frames distintos, usar el único que hay
    if not frames:
        b64 = await _get_latest_frame_b64(user_id, camera_id)
        if b64:
            frames = [b64]
    return frames


@app.post("/api/cameras/{camera_id}/placement-check")
async def placement_check_endpoint(camera_id: str, request: Request):
    """
    FASE 1 — Verifica el encuadre de la cámara durante la instalación.

    Body: {"user_id": "...", "zone": "...", "business_type": "...", "frames": 3}
    Devuelve: {"success": true, "score": 0-100, "items": [...], "consejo": "...",
               "veredicto": "perfecto"|"aceptable"|"mejorar", "frames_used": N}
    """
    try:
        body = {}
        try:
            body = await request.json()
        except Exception:
            body = {}
        user_id = body.get("user_id", "")
        zone = body.get("zone", "")
        biz_type = body.get("business_type", "")
        n_frames = int(body.get("frames", 3) or 3)
        n_frames = max(1, min(6, n_frames))

        if not user_id or not camera_id:
            return {"success": False, "error": "user_id y camera_id requeridos"}

        frames_b64 = await _collect_camera_frames(user_id, camera_id, n_frames=n_frames)
        if not frames_b64:
            return {"success": False,
                    "error": "La cámara no está enviando imágenes todavía. Verifica que esté conectada y encendida."}

        result = await _placement_check_with_qwen(frames_b64, zone, biz_type)
        if not result:
            # Fallback determinista: al menos confirmar que hay frames
            return {
                "success": True,
                "score": 60,
                "items": [{"key": "senal", "ok": True,
                           "detail": f"La cámara envía imágenes ({len(frames_b64)} frames). No pude evaluar el encuadre con IA, revísalo visualmente."}],
                "consejo": "La cámara transmite. Revisa visualmente que el área importante quede centrada en la imagen.",
                "veredicto": "aceptable",
                "frames_used": len(frames_b64),
            }

        result["frames_used"] = len(frames_b64)
        return {"success": True, **result}
    except Exception as e:
        logger.error(f"Error en placement-check: {e}")
        return {"success": False, "error": str(e)}


# ═══════════════════════════════════════════════════════════════════════════
# WOW #3 — Prueba de reglas con notificación real
# ═══════════════════════════════════════════════════════════════════════════
# Endpoint que permite al usuario probar una regla y recibe una notificación
# FCM real si la regla se dispara. Efecto Zeigarnik: counter visible "X/3".

@app.post("/api/cameras/{camera_id}/test-rule")
async def test_rule_endpoint(camera_id: str, request: Request):
    """
    WOW #3 — Prueba una regla y envía notificación real si se dispara.

    Body: {"user_id": "...", "rule_index": 0, "test_action": "abrir cajón",
           "rule_text": "Alerta si hay movimiento en la zona de caja"}

    Devuelve: {"success": true, "triggered": true, "notification_sent": true}
    """
    try:
        body = await request.json()
    except Exception:
        body = {}
    user_id = body.get("user_id", "")
    rule_index = body.get("rule_index", 0)
    test_action = body.get("test_action", "")
    rule_text = body.get("rule_text", "")

    if not user_id or not camera_id:
        return {"success": False, "error": "user_id y camera_id requeridos",
                "triggered": False, "notification_sent": False}

    try:
        # Evaluar si la regla se dispara con la acción de prueba
        # Usamos Qwen para que evalúe si el test_action activa la regla
        triggered = await _evaluate_rule_trigger(rule_text, test_action, user_id, camera_id)

        notification_sent = False
        if triggered:
            # Enviar notificación FCM real
            try:
                from orchestrator import send_fcm_notification
                result = await send_fcm_notification(
                    title=f"🔍 Prueba de regla #{rule_index + 1}",
                    body=f"✅ Tu regla se activó correctamente: \"{rule_text[:80]}\"\n"
                         f"Acción de prueba: \"{test_action}\"",
                    user_id=user_id,
                    notif_type="vigilance_alert",
                    tag=f"rule_test_{camera_id}_{rule_index}",
                )
                notification_sent = bool(result)
            except Exception as e:
                logger.error(f"Error sending test notification: {e}")

        return {
            "success": True,
            "triggered": triggered,
            "notification_sent": notification_sent,
            "rule_index": rule_index,
            "test_action": test_action,
            "rule_text": rule_text,
        }
    except Exception as e:
        logger.error(f"Error en test-rule: {e}")
        return {"success": False, "error": str(e),
                "triggered": False, "notification_sent": False}


async def _evaluate_rule_trigger(rule_text: str, test_action: str, user_id: str, camera_id: str) -> bool:
    """Usa Qwen para evaluar si una acción de prueba dispara una regla."""
    try:
        prompt = (
            f"Estás evaluando una regla de seguridad para una cámara.\n\n"
            f"Regla: \"{rule_text}\"\n"
            f"Acción de prueba que el usuario está realizando: \"{test_action}\"\n\n"
            f"¿Esta acción de prueba activaría o no esta regla? "
            f"Responde SOLO 'SI' o 'NO'."
        )
        async with httpx.AsyncClient(timeout=30) as cl:
            r = await cl.post("http://localhost:8004/v1/chat/completions",
                              json={"model": "qwen",
                                    "messages": [{"role": "user", "content": prompt}],
                                    "max_tokens": 10, "temperature": 0.0})
            if r.status_code == 200:
                data = r.json()
                content = data.get("choices", [{}])[0].get("message", {}).get("content", "").strip().upper()
                return content.startswith("SI")
    except Exception as e:
        logger.error(f"Error evaluando regla: {e}")
    return False


@app.delete("/api/cameras/{camera_id}")
async def delete_camera_endpoint(camera_id: str, user_id: str = ""):
    """Elimina una cámara del usuario.

    Qué hace (eliminación segura, no destructiva en FS):
    1. Quita la entrada de user.json (cameras[]).
    2. Quita vigilance_context si apuntaba a esa cámara.
    3. Renombra el directorio de la cámara a .deleted_<ts> para conservar
       events/frames por si el usuario se equivoca. (No borra nada del FS.)
    4. Si purge=true, elimina físicamente el directorio tras renombrar.

    El frontend llama DELETE /api/cameras/{id}?user_id=UID y espera {success}.
    """
    if not user_id:
        return {"success": False, "error": "user_id required"}
    if not camera_id:
        return {"success": False, "error": "camera_id required"}
    try:
        uf = find_user_json(user_id)
        if not uf or not uf.exists():
            return {"success": False, "error": "usuario no encontrado"}
        with open(uf) as f:
            ud = json.load(f)
        cams = ud.get("cameras", [])
        if not isinstance(cams, list):
            cams = list(cams.values()) if isinstance(cams, dict) else []
        if not any(c.get("camera_id") == camera_id for c in cams):
            return {"success": False, "error": "cámara no encontrada para este usuario"}
        new_cams = [c for c in cams if c.get("camera_id") != camera_id]
        ud["cameras"] = new_cams
        # Limpiar vigilance_context si apuntaba a esa cámara
        vc = ud.get("vigilance_context")
        if isinstance(vc, dict) and (
            vc.get("camera_id") == camera_id or len(new_cams) == 0
        ):
            ud["vigilance_context"] = {k: v for k, v in vc.items() if k != "camera_id"}
        with open(uf, "w") as f:
            json.dump(ud, f, indent=2, ensure_ascii=False)
        # Renombrar el directorio de la cámara a .deleted_<ts> (no borrar).
        cam_root = user_root(user_id) / "cameras" / camera_id
        moved_to = None
        if cam_root.exists():
            ts = int(time.time())
            dest = cam_root.parent / f"{camera_id}.deleted_{ts}"
            try:
                import shutil
                shutil.move(str(cam_root), str(dest))
                moved_to = str(dest)
            except Exception as me:
                logger.warning(f"[delete_camera] no pude mover {cam_root}: {me}")
        logger.info(f"[delete_camera] {user_id}/{camera_id} removed from user.json; dir moved={bool(moved_to)}")
        return {
            "success": True,
            "user_id": user_id,
            "camera_id": camera_id,
            "remaining_cameras": [c.get("camera_id") for c in new_cams],
            "archived_dir": moved_to,
        }
    except Exception as e:
        logger.exception(f"[delete_camera] error: {e}")
        return {"success": False, "error": str(e)}


@app.get("/api/user/profile")
async def get_user_profile(user_id: str):
    user_file = find_user_json(user_id)
    if user_file and user_file.exists():
        with open(user_file) as f:
            data = json.load(f)
        return {
            "user_id": user_id,
            "name": data.get("name", ""),
            "email": data.get("email", ""),
            "business_name": data.get("business_name", ""),
            "business_type": data.get("business_type", ""),
            "schedule": data.get("schedule", {}),
            "what_to_monitor": data.get("what_to_monitor", ""),
            "plan": data.get("plan", "founder"),
            "status": data.get("status", "active"),
            "cameras": data.get("cameras", []),
            "vigilance_prompt": data.get("vigilance_prompt", ""),
            "vigilance_rules": data.get("vigilance_rules", []),
            "rules_es": data.get("rules_es", []),
            "scanner_question": data.get("scanner_question", ""),
            "employee_count": data.get("employee_count", "1"),
            "main_concerns": data.get("main_concerns", []),
            "plan_start": data.get("plan_start", 0),
            "plan_end": data.get("plan_end", 0),
            "trial_end": data.get("trial_end", None),
            "billing_cycle": data.get("billing_cycle", "monthly"),
            "next_due": data.get("next_due", 0),
            "access_token": data.get("access_token", ""),
            "camera_count": len(data.get("cameras", [])),
            "payment_count": len(data.get("payments", []))
        }
    return {"user_id": user_id, "plan": "free", "status": "active", "cameras": [],
            "plan_end": 0, "access_token": ""}

@app.post("/api/user/profile")
async def update_user_profile(request: Request):
    data = await request.json()
    user_id = data.get("user_id", "")
    await _verify_user_token(request.headers.get("authorization"), user_id)  # A1: validar token vs user_id del body
    if not user_id:
        raise HTTPException(status_code=400, detail="user_id required")
    user_file = find_user_json(user_id)
    if not user_file or not user_file.exists():
        raise HTTPException(status_code=404, detail="User not found")
    updatable_fields = ["vigilance_prompt", "vigilance_rules", "name", "business_name", "business_type", "schedule", "what_to_monitor", "schedule_open", "schedule_close", "employee_count", "main_concerns", "phone"]
    def _mut_profile(user_data):
        for field in updatable_fields:
            if field in data:
                if field in ["schedule_open", "schedule_close"]:
                    if "schedule" not in user_data:
                        user_data["schedule"] = {}
                    user_data["schedule"]["open" if field == "schedule_open" else "close"] = data[field]
                elif field == "main_concerns" and isinstance(data[field], str):
                    user_data["main_concerns"] = [c.strip() for c in data[field].split(",") if c.strip()]
                else:
                    user_data[field] = data[field]
    update_user_json(user_id, _mut_profile)  # C1: lock + atomic write
    return {"success": True}

@app.get("/api/user/events")
async def get_user_events(user_id: str, date: str = None, filter: str = None, limit: int = 50, camera_id: str = None, exclude_vigilance: bool = False):
    events = []
    now = int(time.time())
    start_of_today = int(datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    cam_names = {}
    user_file = find_user_json(user_id)
    if user_file and user_file.exists():
        try:
            # B4: user.json corrupto (parcial/vacio) no debe tirar 500 a un
            # cliente legitimo. Cargar de forma defensiva.
            with open(user_file) as f:
                ud = json.load(f)
            for cam in ud.get("cameras", []):
                cam_names[cam.get("camera_id", "")] = cam.get("name", "")
        except Exception as e:
            logger.warning(f"[user/events] user.json corrupto para {user_id}: {e}")
    # M4.4: si filtra por camera_id, no recorrer dirs de otras camaras
    target_cam_ids = {camera_id} if camera_id else None
    # W3 fix (2026-08-11): recolectar TODOS los .json de todas las camaras en
    # una sola lista con su mtime, ordenar globalmente por mtime DESC, y luego
    # iterar cortando por limit. ANTES el loop procesaba camara por camara
    # (os.listdir orden arbitrario: OJO-D1CC08 primero) y llenaba limit=50 con
    # los 50 .json mas recientes de la primera camara, sin visitar la siguiente.
    # Resultado: si OJO-D1CC08 tenia miles de eventos viejos, el backend devolvia
    # 50 eventos de vitrina y 0 de OJO-E17604 aunque esta tuviera 313 evt_*
    # de HOY. El front mostraba solo los vigilance_* del 8-ago que el usuario
    # veia como "no hay eventos nuevos".
    all_entries = []  # list of (mtime, entry, cam_id_for_name_fallback)
    for cam_id, events_dir in resolve_user_events_dirs(user_id):
        if not events_dir.exists():
            continue
        # M4.4: saltar dirs de camaras que no nos interesan
        if target_cam_ids and cam_id not in target_cam_ids and cam_id != "_global":
            continue
        try:
            for e in os.scandir(str(events_dir)):
                if e.name.endswith(".json") and e.is_file():
                    try:
                        all_entries.append((e.stat(follow_symlinks=False).st_mtime, e, cam_id))
                    except OSError:
                        continue
        except Exception:
            continue
    # Ordenar globalmente por mtime descendente — mix entre camaras.
    all_entries.sort(key=lambda t: t[0], reverse=True)
    scanned = 0
    for _mtime, entry, _cam_id in all_entries:
        scanned += 1
        if len(events) >= limit:
            break
        if scanned > limit * 20:
            break  # seguro: no recorrer todo el storage
        try:
            with open(entry.path) as f:
                ev = json.load(f)
        except Exception:
            continue  # json corrupto/vacio -> skip
        # M4.4: filtrar por camera_id del propio evento (fallback si events_dir agrupa _global)
        if target_cam_ids and ev.get("camera_id", "") not in target_cam_ids:
            continue
        if date == "hoy" or filter == "today":
            if int(ev.get("timestamp", 0) or 0) < start_of_today:
                continue
        # M4.6: vigilance_alert es centinela de 1 frame. Bandera + opcionalmente excluido.
        is_centinela = ev.get("event_type") in ("vigilance_alert", "night_alert")
        ev["is_centinela"] = is_centinela
        if exclude_vigilance and is_centinela:
            continue
        # M4.6: filtro "alerts" ahora incluye centinelas como alertas operacionales,
        # con prioridad violation > vigilance_alert/night_alert para el chat brief.
        if filter == "alerts" and not (ev.get("event_type") == "violation" or is_centinela):
            continue
        cid = ev.get("camera_id", "")
        ev["camera_name"] = cam_names.get(cid, _cam_id if _cam_id != "_global" else "Camara")
        if ev.get("event_type") == "violation":
            rule_violated = ev.get("metadata", {}).get("rule_violated", "")
            qa = rule_violated if rule_violated else ev.get("metadata", {}).get("qwen_analysis", "")[:60] if isinstance(ev.get("metadata", {}).get("qwen_analysis"), str) else ""
        else:
            _qa = ev.get("metadata", {}).get("qwen_analysis", "")
            qa = (_qa[:100] if isinstance(_qa, str) else "")
        is_violation = ev.get("event_type") in ("violation", "vigilance_alert", "night_alert")
        ev["qwen"] = {"violation": is_violation, "description": qa}
        ev_meta = ev.get("metadata", {}) if isinstance(ev.get("metadata"), dict) else {}
        yolo_classes = ev_meta.get("yolo_classes") if isinstance(ev_meta, dict) else None
        if isinstance(yolo_classes, str):
            yolo_classes = [c.strip() for c in yolo_classes.split(",") if c.strip()]
        yolo_classes = yolo_classes or []
        md_persons = ev_meta.get("person_tracking", {}) if isinstance(ev_meta, dict) else {}
        md_unique = int(md_persons.get("unique_persons") or 0) if isinstance(md_persons, dict) else 0
        md_total_yolo = int(ev_meta.get("total_yolo_objects") or 0) if isinstance(ev_meta, dict) else 0
        yolo_count = md_total_yolo or md_unique or len(yolo_classes) or 1
        ev["yolo"] = {"count": yolo_count, "classes": yolo_classes}
        ev["persons"] = md_unique or yolo_count
        if "metadata" in ev and isinstance(ev["metadata"], dict) and "grid_b64" in ev["metadata"]:
            del ev["metadata"]["grid_b64"]
        ev["thumb_url"] = f"https://api.ojoia.com.do/api/event-thumb/{ev.get('event_id', '')}?user_id={user_id}"
        events.append(ev)
    return {"events": events}


@app.get("/api/event-thumb/{event_id}")
@app.get("/api/thumb/{event_id}")
async def get_event_thumb(event_id: str, user_id: str = None):
    """Servir miniatura de un evento"""
    from PIL import Image as PILImage
    import io
    # ST-3 fix: buscar en TODOS los discos del usuario (HDD real + SSD legacy)
    search_dirs = user_event_search_dirs(user_id)
    for cam_base in search_dirs:
        if not cam_base.exists():
            continue
        for cam_dir in cam_base.iterdir():
            if not cam_dir.is_dir():
                continue
            img_file = cam_dir / "events" / f"{event_id}.jpg"
            if img_file.exists():
                try:
                    # B3: PIL open+thumbnail+save es CPU work que bloquea el
                    # event loop (decodificacion JPEG). Offload al thread pool.
                    def _gen_thumb(p):
                        from PIL import Image as _PIL
                        import io as _io
                        im = _PIL.open(p)
                        im.thumbnail((160, 120))
                        b = _io.BytesIO()
                        im.save(b, format="JPEG", quality=60)
                        return b.getvalue()
                    thumb_bytes = await asyncio.to_thread(_gen_thumb, img_file)
                    if thumb_bytes:
                        return Response(content=thumb_bytes, media_type="image/jpeg", headers={"Cache-Control": "max-age=86400"})
                except Exception as e_3266:
                    # P0 (Sección #8): loggeo en lugar de pass silencioso
                    logger.warning(f"[thumbnail-gen] {img_file.name} failed: {e_3266}")
    raise HTTPException(status_code=404, detail="Image not found")


@app.get("/api/event-frame/{event_id}")
async def get_event_frame_full(event_id: str, user_id: str = None):
    """D2 (Fase D): frame completo de un evento."""
    # ST-3 fix: multi-disco
    for cam_base in user_event_search_dirs(user_id):
        if not cam_base.exists():
            continue
        for cam_dir in cam_base.iterdir():
            if not cam_dir.is_dir():
                continue
            img_file = cam_dir / "events" / f"{event_id}.jpg"
            if img_file.exists():
                return Response(content=img_file.read_bytes(), media_type="image/jpeg",
                                headers={"Cache-Control": "max-age=86400"})
    raise HTTPException(status_code=404, detail="Image not found")


@app.get("/api/event-clip/{event_id}")
async def get_event_clip(event_id: str, user_id: str = None):
    """D2 (Fase D): paquete forense del evento para el chat de Eva.

    Devuelve:
      - frames: lista de URLs de los 16 frames del grid (frame_000.jpg...)
        guardados en disco por save_event_to_disk_v2 → carrusel tipo video.
      - grid_url: la imagen 4×4 completa del momento.
      - summary/description: narrativa del evento.
      - mp4_url (opcional): clip de video si ffmpeg está disponible.
    """
    # ST-3 fix: buscar en TODOS los discos del usuario
    search_dirs = user_event_search_dirs(user_id)
    for cam_base in search_dirs:
        if not cam_base.exists():
            continue
        for cam_dir in cam_base.iterdir():
            if not cam_dir.is_dir():
                continue
            ev_dir = cam_dir / "events" / event_id
            ev_json = cam_dir / "events" / f"{event_id}.json"
            if not ev_json.exists():
                continue
            try:
                ev = json.loads(ev_json.read_text())
            except Exception:
                ev = {}
            frames_dir = ev_dir / "frames"
            frames = []
            if frames_dir.exists():
                for fp in sorted(frames_dir.glob("frame_*.jpg")):
                    frames.append(f"/api/event-frame-file/{event_id}/{fp.name}?user_id={user_id or ''}")
            grid_file = ev_dir / "grid.jpg"
            q = ev.get("qwen_json", {}) if isinstance(ev.get("qwen_json"), dict) else {}
            vis = q.get("vision", {}) if isinstance(q.get("vision"), dict) else {}
            # D2: clip mp4 con ffmpeg (si existe) — 2fps = 8s de video
            mp4_url = ""
            mp4_file = ev_dir / f"{event_id}.mp4"
            if not mp4_file.exists() and frames_dir.exists():
                try:
                    import shutil as _sh
                    if _sh.which("ffmpeg"):
                        def _make_mp4(fd: Path, out: Path):
                            import subprocess as _sp
                            _sp.run(["ffmpeg", "-y", "-loglevel", "error",
                                     "-framerate", "2", "-pattern_type", "glob",
                                     "-i", str(fd / "frame_*.jpg"),
                                     "-c:v", "libx264", "-pix_fmt", "yuv420p",
                                     "-movflags", "+faststart", str(out)],
                                    timeout=30, check=True)
                        await asyncio.to_thread(_make_mp4, frames_dir, mp4_file)
                except Exception as _e_mp4:
                    logger.debug(f"[D2] ffmpeg clip falló: {_e_mp4}")
            if mp4_file.exists():
                mp4_url = f"/api/event-clip-video/{event_id}?user_id={user_id or ''}"
            return {
                "success": True,
                "event_id": event_id,
                "camera_id": ev.get("camera_id", cam_dir.name),
                "datetime": ev.get("datetime", ""),
                "event_type": ev.get("event_type", ""),
                "summary": ev.get("summary") or ev.get("description") or (vis.get("scene") or ""),
                "scene": vis.get("scene", ""),
                "persons": vis.get("persons", []),
                "frames": frames,
                "frames_count": len(frames),
                "grid_url": f"/api/event-frame/{event_id}?user_id={user_id or ''}" if grid_file.exists() or (cam_dir / "events" / f"{event_id}.jpg").exists() else "",
                "mp4_url": mp4_url,
            }
    raise HTTPException(status_code=404, detail="Evento no encontrado")


@app.get("/api/event-frame-file/{event_id}/{frame_name}")
async def get_event_frame_file(event_id: str, frame_name: str, user_id: str = None):
    """D2: un frame individual del paquete del evento (carrusel)."""
    # path traversal guard
    if "/" in frame_name or ".." in frame_name or not frame_name.startswith("frame_"):
        raise HTTPException(status_code=400, detail="frame inválido")
    # ST-3 fix: multi-disco
    for cam_base in user_event_search_dirs(user_id):
        if not cam_base.exists():
            continue
        for cam_dir in cam_base.iterdir():
            if not cam_dir.is_dir():
                continue
            fp = cam_dir / "events" / event_id / "frames" / frame_name
            if fp.exists():
                return Response(content=fp.read_bytes(), media_type="image/jpeg",
                                headers={"Cache-Control": "max-age=86400"})
    raise HTTPException(status_code=404, detail="Frame no encontrado")


@app.get("/api/event-clip-video/{event_id}")
async def get_event_clip_video(event_id: str, user_id: str = None):
    """D2: video mp4 del evento (2fps ≈ 8s)."""
    # ST-3 fix: multi-disco
    for cam_base in user_event_search_dirs(user_id):
        if not cam_base.exists():
            continue
        for cam_dir in cam_base.iterdir():
            if not cam_dir.is_dir():
                continue
            mp4 = cam_dir / "events" / event_id / f"{event_id}.mp4"
            if mp4.exists():
                return Response(content=mp4.read_bytes(), media_type="video/mp4",
                                headers={"Cache-Control": "max-age=86400"})
    raise HTTPException(status_code=404, detail="Video no encontrado")


@app.get("/api/events/siblings")
async def get_event_siblings(user_id: str, event_id: str = "", camera_id: Optional[str] = None):
    """Devuelve los eventos vecinos (anterior y siguiente) del actual.

    Usado para navegación entre eventos en el viewer.
    NOTA: Debe estar declarado ANTES de /api/events/{event_id} para que no sea
    interceptado por el catch-all.
    """
    try:
        if not user_id:
            raise HTTPException(status_code=400, detail="user_id required")
        events_list = []
        for cam_id, events_dir in resolve_user_events_dirs(user_id):
            if cam_id == "_global":
                continue
            if not events_dir.exists():
                continue
            for fname in sorted(events_dir.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
                if not fname.name.endswith(".json"):
                    continue
                ev_id = fname.name[:-5]
                if event_id and ev_id == event_id:
                    continue
                if camera_id and cam_id != camera_id:
                    continue
                try:
                    with open(fname) as f:
                        ev = json.load(f)
                    events_list.append({
                        "event_id": ev_id,
                        "camera_id": cam_id,
                        "timestamp": ev.get("timestamp", 0),
                        "event_type": ev.get("event_type", ""),
                        "description": ev.get("description", "")[:140]
                    })
                except:
                    continue
        events_list.sort(key=lambda x: x.get("timestamp", 0), reverse=True)
        return {
            "success": True,
            "current_event_id": event_id,
            "events": events_list[:50]
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error get_event_siblings: {e}")
        return {"success": False, "events": [], "error": str(e)}


@app.get("/api/events/{event_id}/frame/{index}")
async def get_event_frame(event_id: str, index: int, user_id: str):
    """Sirve un frame individual del grid de un evento."""
    try:
        if not user_id:
            raise HTTPException(status_code=400, detail="user_id required")
        if index < 0:
            raise HTTPException(status_code=400, detail="invalid index")
        frame_path = None
        for _cam_id, events_dir in resolve_user_events_dirs(user_id):
            frames_dir = events_dir / event_id / "frames"
            if frames_dir.exists():
                candidates = (
                    list(frames_dir.glob(f"frame_{int(index):02d}.jpg")) +
                    list(frames_dir.glob(f"frame_{int(index):03d}.jpg")) +
                    list(frames_dir.glob(f"frame_{int(index):04d}.jpg")) +
                    list(frames_dir.glob(f"frame_{int(index)}_*.jpg")) +
                    list(frames_dir.glob(f"frame_{index}.jpg"))
                )
                for c in candidates:
                    if c.exists():
                        frame_path = c
                        break
            if frame_path:
                break
        if not frame_path or not frame_path.exists():
            for _cam_id, events_dir in resolve_user_events_dirs(user_id):
                main_jpg = events_dir / f"{event_id}.jpg"
                if main_jpg.exists():
                    frame_path = main_jpg
                    break
        if not frame_path or not frame_path.exists():
            raise HTTPException(status_code=404, detail="Frame no encontrado")
        with open(frame_path, "rb") as f:
            data = f.read()
        return Response(
            content=data,
            media_type="image/jpeg",
            headers={
                "Cache-Control": "public, max-age=3600",
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Methods": "GET, OPTIONS",
                # v9.2: listar Authorization explícitamente en vez de "*".
                # Firefox/Chrome bloquean "*" cuando la petición lleva
                # Authorization (ver nota en add_security_headers).
                "Access-Control-Allow-Headers": "Authorization, Content-Type, Accept, Origin, User-Agent, Cache-Control, Pragma",
                "Content-Disposition": "inline",
                "X-Frame-Options": "SAMEORIGIN"
            }
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error get_event_frame: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/events/{event_id}")
async def get_event_detail(event_id: str, user_id: str):
    """Detalle de un evento con su grid + frames."""
    event_file = None
    img_file = None
    for _cam_id, events_dir in resolve_user_events_dirs(user_id):
        ef = events_dir / f"{event_id}.json"
        if ef.exists():
            event_file = ef
            img_file = events_dir / f"{event_id}.jpg"
            break
    if not event_file or not event_file.exists():
        raise HTTPException(status_code=404, detail="Evento no encontrado")
    with open(event_file) as f:
        event = json.load(f)
    if img_file and img_file.exists():
        with open(img_file, "rb") as f:
            event["frame_b64"] = base64.b64encode(f.read()).decode()
    event["qwen"] = {
        "violation": event.get("event_type") == "violation",
        "description": event.get("metadata", {}).get("qwen_analysis", "")
    }
    event["yolo"] = {"count": 1}
    event["grid_b64"] = event.get("metadata", {}).get("grid_b64", "")
    frames_dir = events_dir / event_id / "frames"
    if frames_dir.exists():
        frame_files = sorted(frames_dir.glob("frame_*.jpg"))
        event["frames"] = [{"index": i, "file": f.name} for i, f in enumerate(frame_files)]
        event["frameCount"] = len(frame_files)
    else:
        event["frames"] = []
        event["frameCount"] = 0
    return event


@app.get("/api/user/events/stats")

@app.get("/api/user/events/stats")
async def get_user_events_stats(user_id: str, date: str = None):
    now = int(time.time())
    start_of_today = int(datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    total = 0
    today_count = 0
    alerts_count = 0
    for cam_id, events_dir in resolve_user_events_dirs(user_id):
        if not events_dir.exists():
            continue
        for fname in sorted(events_dir.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
            if not fname.name.endswith(".json"):
                continue
            try:
                with open(fname) as f:
                    ev = json.load(f)
            except:
                continue
            total += 1
            ts = ev.get("timestamp", 0)
            if ts >= start_of_today:
                today_count += 1
            if ev.get("event_type") == "violation":
                alerts_count += 1
    return {"total": total, "today": today_count, "alerts": alerts_count}

# Camaras
@app.get("/api/cameras")
async def get_cameras(user_id: str):
    user_file = find_user_json(user_id)
    if user_file and user_file.exists():
        with open(user_file) as f:
            ud = json.load(f)
        cams = ud.get("cameras", []) or []
        # ── Auto-discovery: registrar cámaras del FS que no estén en user.json ──
        cams_dir = user_root(user_id) / "cameras"
        known_ids = {c.get("camera_id") for c in cams if c.get("camera_id")}
        if cams_dir.exists():
            for d in cams_dir.iterdir():
                if not d.is_dir() or d.name in known_ids or ".deleted_" in d.name or ".orphan_" in d.name:
                    continue
                cj = d / "camera.json"
                if not cj.exists():
                    continue
                try:
                    cc = json.loads(cj.read_text())
                    cid = cc.get("camera_id") or d.name
                except Exception:
                    cc, cid = {}, d.name
                last_frame = 0
                lf = d / "frames" / "latest_raw.jpg"
                if lf.exists():
                    last_frame = int(lf.stat().st_mtime)
                cams.append({
                    "camera_id": cid,
                    "name": cc.get("name", cid),
                    "zone": cc.get("zone", ""),
                    "business_type": cc.get("business_type", ""),
                    "business_name": cc.get("business_name", ""),
                    "active": bool(cc.get("active", True)),
                    "last_announce": 0,
                    "last_frame": last_frame,
                    "configured_at": cc.get("configured_at", 0),
                })
                logger.info(f"[cameras] Auto-discovered {cid} for user {user_id}")
        now = time.time()
        result = []
        for cam in cams:
            cam_copy = dict(cam)
            # Determine online status dynamically
            # Camera is online ONLY if it has announced OR sent a frame within last 2 min
            last_announce = cam.get("last_announce") or 0
            last_frame = cam.get("last_frame") or 0
            is_online = False
            if last_announce and (now - last_announce) < 120:
                is_online = True
            if last_frame and (now - last_frame) < 120:
                is_online = True
            cam_copy["active"] = is_online
            cam_copy["announce_age"] = int(now - last_announce) if last_announce else None
            cam_copy["frame_age"] = int(now - last_frame) if last_frame else None
            # Metrics para el frontend
            try:
                cam_id = cam.get("camera_id", "")
                ev_dir = user_root(user_id) / "cameras" / cam_id / "events"
                total_ev = 0
                total_al = 0
                today_ev = 0
                today_al = 0
                today_start = (int(now) // 86400) * 86400
                if ev_dir.exists():
                    for f in ev_dir.iterdir():
                        if f.name.endswith(".json"):
                            total_ev += 1
                            try:
                                with open(f) as fh:
                                    _ev = json.load(fh)
                                _ts = _ev.get("timestamp", 0)
                                _type = _ev.get("event_type", "")
                                if _type in ("vigilance_alert", "violation", "attention_alert"):
                                    total_al += 1
                                if _ts and _ts >= today_start:
                                    today_ev += 1
                                    if _type in ("vigilance_alert", "violation", "attention_alert"):
                                        today_al += 1
                            except:
                                logger.debug("silent except")

                cam_copy["metrics"] = {
                    "total_events": total_ev,
                    "total_alerts": total_al,
                    "today_events": today_ev,
                    "today_alerts": today_al,
                }
            except:
                cam_copy["metrics"] = {"total_events": 0, "total_alerts": 0, "today_events": 0, "today_alerts": 0}
            result.append(cam_copy)
        # Si se autodescubrieron cámaras, persistirlas en user.json para futuras requests rápidas
        discovered = [c for c in cams if c.get("camera_id") not in known_ids]
        if discovered:
            try:
                update_user_json(user_id, lambda ud2: ud2.__setitem__("cameras", cams))  # C1
            except Exception:
                logger.debug("silent: {exc}", exc=Exception)

        return {"cameras": result}
    return {"cameras": []}

# ── Proxy ESP32 (LED, calidad, rotación) ─────────────────────────────────────

from typing import Optional as _Optional

@app.post("/cameras/{camera_id}/cmd", include_in_schema=False)
async def cam_cmd(camera_id: str, request: dict = None):
    """Proxy de comandos al ESP32 local + guardar config para polling."""
    cors_headers = {"Access-Control-Allow-Origin": "*"}
    try:
        body = request or {}

        # Guardar configuracion en user.json para polling del ESP32
        _save_cam_config_to_user(camera_id, body)

        target_ip = None
        users_dir = STORAGE_ROOT / "users"
        if users_dir.is_dir():
            for user_dir in users_dir.iterdir():
                if not user_dir.is_dir():
                    continue
                uf = user_dir / "user.json"
                if not uf.exists():
                    continue
                try:
                    with open(uf) as f:
                        ud = json.load(f)
                    for c in ud.get("cameras", []):
                        if c.get("camera_id") == camera_id:
                            target_ip = c.get("last_announce_ip") or ""
                            break
                except Exception:
                    logger.debug("silent: {exc}", exc=Exception)

                if target_ip:
                    break
        # FIX (2026-09-03): NO intentar POST directo al :81 del ESP32.
        # target_ip es la IP PÚBLICA del negocio (last_announce_ip) — el
        # servidor no puede entrar al :81 interno por el router (siempre
        # fallaba con timeout de 10s por comando = UX lenta + falsa ilusión
        # de 'se aplicó ya'). El polling (≤30s) es el camino correcto y
        # suficiente: la config queda persistida arriba.
        return JSONResponse(content={"ok": True, "queued": True, "via": "polling",
                                     "applies_in_s": 30}, headers=cors_headers)
    except Exception as e:
        return JSONResponse(status_code=502, content={"ok": False, "error": str(e)}, headers=cors_headers)


def _save_cam_config_to_user(camera_id: str, body: dict):
    """Guardar configuracion de camara en user.json para polling del ESP32."""
    # [DIAG] log inicial
    try:
        logger.info(f"[DIAG_SAVE] entry camera_id={camera_id} body={body}")
    except Exception:
        logger.debug("silent: {exc}", exc=Exception)

    try:
        users_dir = STORAGE_ROOT / "users"
        if not users_dir.is_dir():
            logger.warning(f"[DIAG_SAVE] users_dir no existe")
            return
        for user_dir in users_dir.iterdir():
            if not user_dir.is_dir():
                continue
            uf = user_dir / "user.json"
            if not uf.exists():
                continue
            user_id = user_dir.name
            # [Fix B1] Usar _get_user_lock para evitar race con _update_camera_last_frame
            # que pisa user.json cada frame (cada ~1s). Sin este lock, el save del
            # LED quedaba escrito durante milisegundos antes de ser pisado por el
            # próximo frame que escribía un snapshot sin led_on.
            try:
                with _get_user_lock(user_id):
                    # Re-leer DENTRO del lock — el snapshot puede haber cambiado
                    with open(uf) as f:
                        ud = json.load(f)
                    cams = ud.get("cameras", [])
                    found = False
                    for c in cams:
                        if c.get("camera_id") == camera_id:
                            # Guardar campos que el ESP32 entiende
                            if "quality" in body:
                                c["quality"] = body["quality"]
                            if "interval_ms" in body:
                                c["interval_ms"] = body["interval_ms"]
                            if "framesize" in body:
                                c["framesize"] = body["framesize"]
                            if "led_auto" in body:
                                c["led_auto"] = body["led_auto"]
                            if "led_bright" in body:
                                c["led_bright"] = body["led_bright"]
                            if "h_mirror" in body:
                                c["h_mirror"] = body["h_mirror"]
                            if "v_flip" in body:
                                c["v_flip"] = body["v_flip"]
                            if "led_on" in body:
                                c["led_on"] = body["led_on"]
                            if "brightness" in body:
                                c["brightness"] = body["brightness"]
                            if "contrast" in body:
                                c["contrast"] = body["contrast"]
                            # Escritura atómica dentro del lock
                            tmp = uf.with_suffix(".tmp")
                            with open(tmp, "w") as f:
                                json.dump(ud, f, indent=2)
                            tmp.replace(uf)
                            found = True
                            logger.info(f"[DIAG_SAVE] OK camera_id={camera_id} keys_after={list(c.keys())}")
                            break
                    if not found:
                        logger.info(f"[DIAG_SAVE] camera_id={camera_id} NOT FOUND in user {user_id}")
            except Exception as e:
                logger.warning(f"[DIAG_SAVE] error user {user_id}: {e}")
                continue
        else:
            logger.warning(f"[DIAG_SAVE] camera_id={camera_id} NO ENCONTRADA en ningún user.json")
    except Exception as e:
        logger.warning(f"Error saving cam config: {e}")

@app.get("/api/cameras/{camera_id}")
async def get_camera(camera_id: str, user_id: str = None):
    """Devuelve los datos de una camara especifica incluyendo reglas y prompt."""
    if not user_id:
        raise HTTPException(status_code=400, detail="user_id required")
    uf = find_user_json(user_id)
    if not uf or not uf.exists():
        raise HTTPException(status_code=404, detail="Usuario no encontrado")
    with open(uf) as f:
        ud = json.load(f)
    for c in ud.get("cameras", []):
        if c.get("camera_id") == camera_id:
            # Cargar datos adicionales de camera.json si existe
            cam_cfg = get_camera_config_static(user_id, camera_id) or {}
            result = dict(c)
            result["system_prompt"] = cam_cfg.get("system_prompt", ud.get("vigilance_prompt", ""))
            result["rules"] = cam_cfg.get("rules", [])
            result["rules_es"] = cam_cfg.get("rules_es", [])
            result["yolo_triggers"] = cam_cfg.get("yolo_triggers", ["person"])
            # Override active status dynamically
            now = time.time()
            last_announce = result.get("last_announce", 0) or 0
            last_frame = result.get("last_frame", 0) or 0
            announce_age = now - last_announce if last_announce else None
            frame_age = now - last_frame if last_frame else None
            is_online = False
            if announce_age is not None and announce_age < 120:
                is_online = True
            if frame_age is not None and frame_age < 120:
                is_online = True
            result["active"] = is_online
            return result
    raise HTTPException(status_code=404, detail="Camara no encontrada")

@app.get("/api/cameras/{camera_id}/vigilance")
async def get_camera_vigilance(camera_id: str, user_id: Optional[str] = None):
    """Devuelve la configuracion de vigilancia de una camara."""
    if not user_id:
        raise HTTPException(status_code=400, detail="user_id required")
    uf = find_user_json(user_id)
    if not uf or not uf.exists():
        raise HTTPException(status_code=404, detail="Usuario no encontrado")
    with open(uf) as f:
        ud = json.load(f)
    cam = next((c for c in ud.get("cameras", []) if c.get("camera_id") == camera_id), None)
    if not cam:
        raise HTTPException(status_code=404, detail="Camara no encontrada")
    cam_cfg = get_camera_config_static(user_id, camera_id) or {}
    vigilance = cam_cfg.get("vigilance", ud.get("vigilance", {}))
    schedule = ud.get("schedule", {})
    mode = "vigilante" if _is_vigilante_mode(schedule, vigilance, datetime.now().strftime("%H:%M"), cam_cfg.get("night_mode", False)) else "normal"
    return {
        "vigilance": vigilance,
        "schedule": schedule,
        "mode": mode,
        "system_prompt": cam_cfg.get("system_prompt", ud.get("vigilance_prompt", "")),
        "cam_cfg": cam_cfg,
    }

@app.put("/api/cameras/{camera_id}/vigilance")
async def save_camera_vigilance(camera_id: str, request: dict = None):
    """Guarda configuracion de vigilancia de una camara."""
    body = request or {}
    user_id = _resolve_user_id_from_camera(camera_id) if not body.get("user_id") else body["user_id"]
    if not user_id:
        raise HTTPException(status_code=400, detail="user_id required")
    # M5.2: validar HH:MM antes de guardar (el frontend tambien valida, defensa en profundidad).
    _HhMm_re = re.compile(r"^\d{2}:\d{2}$")
    schedule_in = body.get("schedule") or {}
    _open = schedule_in.get("open", "") if isinstance(schedule_in, dict) else ""
    _close = schedule_in.get("close", "") if isinstance(schedule_in, dict) else ""
    def _valid_hhmm(s):
        if not _HhMm_re.match(str(s)):
            return False
        try:
            h, m = str(s).split(":")
            return 0 <= int(h) <= 23 and 0 <= int(m) <= 59
        except Exception:
            return False
    if schedule_in and (not _valid_hhmm(_open) or not _valid_hhmm(_close)):
        raise HTTPException(status_code=400, detail=f"Horario inválido (usa HH:MM): open={_open} close={_close}")
    uf = find_user_json(user_id)
    if not uf or not uf.exists():
        raise HTTPException(status_code=404, detail="Usuario no encontrado")
    with open(uf) as f:
        ud = json.load(f)
    schedule = body.get("schedule")
    if schedule:
        ud["schedule"] = schedule
    vigilance = body.get("vigilance")
    if vigilance:
        ud["vigilance"] = vigilance
        cam_dir = Path(STORAGE_ROOT) / "users" / user_id / "cameras" / camera_id
        cam_cfg_path = cam_dir / "camera.json"
        if cam_cfg_path.exists():
            try:
                with open(cam_cfg_path) as f:
                    cam_cfg = json.load(f)
                cam_cfg["vigilance"] = vigilance
                with open(cam_cfg_path, "w") as f:
                    json.dump(cam_cfg, f, indent=2)
                _invalidate_cam_cfg_cache(user_id, camera_id)  # F3
            except Exception:
                logger.debug("silent: {exc}", exc=Exception)

    with open(uf, "w") as f:
        json.dump(ud, f, indent=2)
    system_prompt = ""
    try:
        from eva.vigilance_prompts import format_vision_prompt as _regen
        cam_cfg = get_camera_config_static(user_id, camera_id) or {}
        schedule = ud.get("schedule", {})
        is_after = _is_vigilante_mode(schedule, vigilance or ud.get("vigilance", {}), datetime.now().strftime("%H:%M"), cam_cfg.get("night_mode", False))
        new_prompt = _regen(
            business_type=ud.get("business_type", ""),
            zone=cam_cfg.get("zone", ""),
            business_name=ud.get("business_name", ""),
            is_after_hours=is_after,
            owner_notes=ud.get("owner_notes", [])
        )
        cam_cfg_path = Path(STORAGE_ROOT) / "users" / user_id / "cameras" / camera_id / "camera.json"
        if cam_cfg_path.exists():
            with open(cam_cfg_path) as f:
                cam_cfg_data = json.load(f)
            cam_cfg_data["system_prompt"] = new_prompt
            with open(cam_cfg_path, "w") as f:
                json.dump(cam_cfg_data, f, indent=2)
            _invalidate_cam_cfg_cache(user_id, camera_id)  # F3
        system_prompt = new_prompt
    except Exception as e:
        logger.warning(f"No se pudo regenerar prompt: {e}")
    return {"success": True, "mode": "vigilante" if _is_vigilante_mode(ud.get("schedule", {}), vigilance or ud.get("vigilance", {}), datetime.now().strftime("%H:%M"), False) else "normal", "system_prompt": system_prompt}

@app.get("/api/cameras/{camera_id}/grid")
async def get_camera_grid(camera_id: str, user_id: Optional[str] = None):
    grid = orchestrator._get_grid(user_id or "", camera_id)
    last_cam = grid.get_last_camera_id()
    frame_bytes = grid.get_last_frame_bytes() if last_cam == camera_id else b""
    image_b64 = base64.b64encode(frame_bytes).decode() if frame_bytes else ""
    return {
"success": True,
        "camera_id": camera_id,
        "image_b64": image_b64,
        "yolo": {"count": grid.get_last_yolo_count()},
        "qwen": {"violation": False}
    }


# ── Config para ESP32 (polling cada 30s) ─────────────────────────────────
# El ESP32 hace GET /camera/config/{id} para obtener su configuracion
# Este endpoint devuelve los valores que el ESP32 debe aplicar

@app.get("/camera/config/{camera_id}")
async def get_esp32_config(camera_id: str):
    """
    Endpoint de polling del ESP32.
    Devuelve la configuracion que la app ha guardado para esta camara:
    - quality, interval_ms, framesize, led_auto, led_bright, h_mirror, v_flip
    El ESP32 compara con su config local y aplica cambios si difiere.
    """
    # Buscar el user_id de esta camara
    user_id = _resolve_user_id_from_camera(camera_id)
    if not user_id:
        raise HTTPException(status_code=404, detail="Camera not found")

    uf = find_user_json(user_id)
    if not uf or not uf.exists():
        raise HTTPException(status_code=404, detail="User not found")

    with open(uf) as f:
        ud = json.load(f)

    for c in ud.get("cameras", []):
        if c.get("camera_id") == camera_id:
            # F2 (2026-09-01): trigger de escaneo de red — one-shot. Se
            # sirve UNA vez y se limpia (el firmware lo parsea en su poll).
            scan_request = bool(c.get("scan_request"))
            reboot_request = bool(c.get("reboot_request"))
            led_on_shot = bool(c.get("led_on"))
            if scan_request or reboot_request or led_on_shot:
                def _clear_flags(ud2):
                    for cc in ud2.get("cameras", []):
                        if cc.get("camera_id") == camera_id:
                            cc["scan_request"] = False
                            cc["reboot_request"] = False
                            cc["led_on"] = False  # NV one-shot consumido
                update_user_json(user_id, _clear_flags)
            if scan_request:
                c["scan_request"] = False
                update_user_json(user_id, lambda ud2: [
                    cc.__setitem__("scan_request", False)
                    for cc in ud2.get("cameras", [])
                    if cc.get("camera_id") == camera_id
                ])
            # Devolver solo los campos que el ESP32 entiende
            return {
                "camera_id": camera_id,
                "quality": c.get("quality", 10),
                "interval_ms": c.get("interval_ms", 500),
                "framesize": c.get("framesize", 10),
                "led_auto": c.get("led_auto", True),
                "led_bright": c.get("led_bright", 128),
                # NV (2026-09-03): led_on ONE-SHOT — igual que scan/reboot.
                # Antes: si el server mandaba led_on:true, el siguiente poll
                # servía false (default) y lo APAGABA al instante (el firmware
                # hace if/else if). Ahora: se sirve true UNA vez y se limpia.
                "led_on": bool(c.get("led_on")),
                "h_mirror": c.get("h_mirror", False),
                "v_flip": c.get("v_flip", False),
                "brightness": c.get("brightness", 0),
                "contrast": c.get("contrast", 0),
                "stream_always": c.get("stream_always", True),
                "scan_request": scan_request,
                # F-admin: reboot one-shot (panel admin / cámaras colgadas)
                "reboot_request": bool(c.get("reboot_request")),
            }

    raise HTTPException(status_code=404, detail="Camera not found in user")


# Ingesta de Frames (ESP32-CAM)
@app.post("/ingest/frame")
@app.post("/frames/ingest")
async def ingest_frame(request: Request, camera_id: str = Form(None),
                       user_id: str = Form("default"), image: UploadFile = File(None)):
    # Leer camera_id del header X-Camera-Id si no viene en el form
    cid = camera_id or request.headers.get("x-camera-id") or "unknown"
    uid = user_id or "default"
    if image is None:
        body = await request.body()
        ct = request.headers.get("content-type", "")
        if "image" in ct and body:
            from io import BytesIO
            image = UploadFile(filename=f"{cid}.jpg", file=BytesIO(body))
        else:
            raise HTTPException(status_code=400, detail="No image provided")
    return await _process_ingest(request, cid, uid, image)

@app.post("/ingest/photo")
@app.post("/ingest/snapshot")
async def ingest_photo(request: Request, filename: str = Form(None),
                       camera_id: str = Form(None), user_id: str = Form(None),
                       image: UploadFile = File(None)):
    cid = camera_id or filename or "unknown"
    uid = user_id or "default"
    if image is None:
        body = await request.body()
        ct = request.headers.get("content-type", "")
        if "image" in ct and body:
            from io import BytesIO
            image = UploadFile(filename=f"{cid}.jpg", file=BytesIO(body))
        else:
            raise HTTPException(status_code=400, detail="No image provided")
    return await _process_ingest(request, cid, uid, image)

@app.post("/ingest/raw")
async def ingest_raw(request: Request):
    """ESP32 envia bytes puros (image/jpeg) con query params camera_id y user_id."""
    try:
        body = await request.body()
        ct = request.headers.get("content-type", "")
        qp = request.query_params
        cid = qp.get("camera_id") or "unknown"
        uid = qp.get("user_id") or "default"
        if not body:
            return {"success": False, "error": "empty body"}
        from io import BytesIO
        fake = UploadFile(filename=f"{cid}.jpg", file=BytesIO(body))
        return await _process_ingest(request, cid, uid, fake)
    except Exception as e:
        logger.error(f"Ingest raw error: {e}")
        return {"success": False, "error": str(e)}

async def _resolve_unknown_camera(user_id: str, client_ip: str) -> str:
    """Si camera_id es 'unknown', redirigir a la camara activa del usuario."""
    try:
        uf = find_user_json(user_id)
        if uf and uf.exists():
            with open(uf) as f:
                ud = json.load(f)
            cams = ud.get("cameras", [])
            for c in reversed(cams):
                cid = c.get("camera_id", "")
                if cid and cid != "unknown":
                    return cid
    except Exception:
        logger.debug("silent: {exc}", exc=Exception)

    return "unknown"

def _adjust_brightness(img_bytes: bytes, target_brightness: int = 80) -> bytes:
    """Night-vision (2026-09-03): rescatar frames oscuros con CLAHE + gamma.

    Antes: boost ciego (factor hasta 3x) que amplificaba ruido 3x y
    quemaba luces. Ahora, si brillo < 60:
      1. CLAHE (equalización adaptativa) — texturas locales sin amplificar
         ruido global: el estándar forense.
      2. Gamma adaptativo según oscuridad real (más oscuro → más gamma,
         tope 2.2) — levanta SOMBRAS sin tocar altas luces.
      3. Si tras el rescate aún hay brillo < 45 → devuelve la mejor
         versión + el caller decide avisar oscuridad REAL.
    Frames con luz normal (>= 60) van sin tocar — cero degradación.
    El viewer/silueta/paneles Qwen reciben ESTA versión: mejor calidad
    visible en toda la app, no solo en el análisis.
    """
    try:
        from PIL import Image, ImageStat
        import io
        img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
        stat = ImageStat.Stat(img.convert("L"))
        avg = stat.mean[0]
        if avg < 60:
            # gamma por tabla LUT (rápido) según oscuridad
            gamma = 1.0 + min(1.2, (60 - avg) / 30.0)   # 27→2.0 | 45→1.5
            lut = [min(255, int(((i / 255.0) ** (1.0 / gamma)) * 255)) for i in range(256)]
            out = img.point(lut * 3)
            # CLAHE en L* (lab) — equalización local
            try:
                import cv2, numpy as _np
                arr = _np.array(out)
                lab = cv2.cvtColor(arr, cv2.COLOR_RGB2LAB)
                l = lab[:, :, 0]
                l2 = cv2.createCLAHE(clipLimit=2.5, tileGridSize=(8, 8)).apply(l)
                # limitar ganancia: no quemar lo que ya era visible
                lab[:, :, 0] = _np.maximum(l2, l)
                out = Image.fromarray(cv2.cvtColor(lab, cv2.COLOR_LAB2RGB))
            except ImportError:
                pass  # sin cv2: solo gamma (fallback válido)
            buf = io.BytesIO()
            out.save(buf, format="JPEG", quality=88)
            return buf.getvalue()
    except Exception:
        logger.debug("silent: {exc}", exc=Exception)

    return img_bytes


# ── B4 (2026-09-01): alerta de imagen oscura ──────────────────────────────
# El usuario debe saber cuándo su cámara ve mal (lente sucia, luz apagada,
# contraluz). Antes _adjust_brightness corregía en silencio y nadie se enteraba
# de que los análisis corrían sobre imágenes de baja calidad.
_DARK_ALERT_MIN_BRIGHTNESS = 40.0   # brillo medio RGB < 40 → oscura
_DARK_ALERT_COOLDOWN_S = 3600.0     # máx 1 alerta de oscuridad / hora / cámara
_dark_alert_last: Dict[str, float] = {}


def _frame_is_too_dark(img_bytes: bytes) -> bool:
    """B4: brillo medio de la imagen por debajo del umbral."""
    try:
        from PIL import Image, ImageStat
        import io as _io
        img = Image.open(_io.BytesIO(img_bytes)).convert("RGB")
        stat = ImageStat.Stat(img)
        return (sum(stat.mean[:3]) / 3) < _DARK_ALERT_MIN_BRIGHTNESS
    except Exception:
        return False


async def _maybe_alert_dark_frame(user_id: str, camera_id: str, img_bytes: bytes):
    """B4: registra evento + push (con cooldown por cámara) si el frame es muy oscuro."""
    now = time.time()
    key = f"{user_id}_{camera_id}"
    if now - _dark_alert_last.get(key, 0) < _DARK_ALERT_COOLDOWN_S:
        return
    if not _frame_is_too_dark(img_bytes):
        return
    _dark_alert_last[key] = now
    if len(_dark_alert_last) > 512:
        _dark_alert_last.clear()
        _dark_alert_last[key] = now
    try:
        from orchestrator import save_event_to_disk_v2, send_fcm_notification
        evt_id = save_event_to_disk_v2(
            user_id=user_id, camera_id=camera_id, event_type="dark_frame",
            frame_bytes=img_bytes,
            summary=f"⚠️ La imagen de la cámara llegó muy oscura. Revisa la iluminación, el lente o el ángulo (contraluz).",
            qwen_json={"scene": "Frame demasiado oscuro para analizar con calidad.",
                       "importance": "media", "importancia": "media"},
            metadata={"skipped_qwen": "too_dark", "brightness_alert": True},
        )
        asyncio.create_task(send_fcm_notification(
            title="📷 Problema de imagen",
            body=f"La cámara {camera_id} está enviando imágenes muy oscuras. Revisa la iluminación o el lente.",
            user_id=user_id,
            image_b64=base64.b64encode(img_bytes).decode()[:80000] if img_bytes else None,
            link=f"https://ojoia.com.do/#events?alert={evt_id}&camera={camera_id}",
            event_id=evt_id,
            notif_type="dark_frame",
        ))
        logger.warning(f"[B4] frame oscuro → evento {evt_id} + notif (cam={camera_id})")
    except Exception as e:
        logger.warning(f"[B4] dark frame alert falló: {e}")


def _resolve_user_id_from_camera(camera_id: str) -> Optional[str]:
    """Resolver user_id buscando en todos los usuarios por camera_id."""
    if not camera_id:
        return None
    users_dir = STORAGE_ROOT / "users"
    if users_dir.is_dir():
        for user_folder in users_dir.iterdir():
            user_file = user_folder / "user.json"
            if user_file.is_file():
                try:
                    with open(user_file) as f:
                        ud = json.load(f)
                    for cam in ud.get("cameras", []):
                        if cam.get("camera_id") == camera_id:
                            return ud.get("user_id", user_folder.name)
                except:
                    logger.debug("silent except")

    return None


def _is_vigilante_mode(schedule: dict, vigilance: dict, current_time: str, night_mode: bool = False, user_id: str = None, camera_id: str = None) -> bool:
    """Determinar si la cámara está en modo centinela.
    
    Lógica SIMPLE y ROBUSTA:
    1. Si sentinel_mode.enabled → SIEMPRE centinela
    2. Si night_mode → SIEMPRE centinela
    3. Si vigilance.enabled == False → NORMAL (no centinela)
    4. Si current_time está fuera de [open, close+grace) → CENTINELA
    """
    # [1] Sentinel mode activo → siempre centinela
    if vigilance.get("sentinel_mode", {}).get("enabled", False):
        return True
    
    # [2] Night mode forzado → centinela
    if night_mode:
        return True
    
    # [3] Vigilancia deshabilitada → modo normal
    if not vigilance.get("enabled", False):
        return False
    
    # [4] Modo centinela según horario
    # Si schedule está vacío, intentar cargar desde user.json
    if (not schedule or not schedule.get("open") or not schedule.get("close")) and user_id:
        try:
            from pathlib import Path
            _uf = user_root(user_id) / "user.json"
            if _uf.exists():
                with open(_uf) as _f:
                    _ud = json.load(_f)
                schedule = _ud.get("schedule", {}) or {}
                if not vigilance:
                    vigilance = _ud.get("vigilance", {}) or {}
        except Exception:
            logger.debug("silent: {exc}", exc=Exception)

    
    if not schedule or not schedule.get("open") or not schedule.get("close"):
        # Sin schedule → asumir horario laboral default 07:00-23:59
        schedule = {"open": "07:00", "close": "23:59"}
    
    open_t = schedule.get("open", "07:00")
    close_t = schedule.get("close", "23:59")
    grace_min = vigilance.get("grace_minutes", 15)
    
    try:
        from datetime import datetime as dt, timedelta
        now_dt = dt.strptime(current_time, "%H:%M")
        open_dt = dt.strptime(open_t, "%H:%M")
        close_dt = dt.strptime(close_t, "%H:%M")
        
        # Calcular fin del horario con gracia
        close_with_grace = close_dt + timedelta(minutes=grace_min)
        # Normalizar si pasa de medianoche (hour=24 → day+1, hour=0)
        if close_with_grace.hour == 0 and close_with_grace.minute > 0 and close_dt.hour >= 23:
            # Caso especial: horario extendido a día siguiente
            # En este caso, modo normal mientras now < open del día siguiente
            # Ahora estamos en horario "vivo" hasta 23:59
            # Centinela solo si now < open (antes de abrir)
            if now_dt < open_dt:
                return True
            return False
        
        # Caso normal: comparar directamente
        if now_dt < open_dt or now_dt >= close_with_grace:
            return True
        return False
    except Exception:
        # En caso de error, NO ser centinela (ser conservador)
        return False


_vigilance_cooldowns = {}  # {user_id_camera_id: last_alert_timestamp}

def _save_vigilance_event(user_id: str, camera_id: str, img_bytes: bytes, yolo_count: int, yolo_classes: list, client_ip: str):
    """Guardar evento de vigilancia (modo centinela) y notificar FCM. Usa cooldown_min de la config."""
    global _vigilance_cooldowns
    now = int(time.time())
    cam_key = f"{user_id}_{camera_id}"
    try:
        _cam_cfg = get_camera_config(user_id, camera_id)
        # M5.3: el frontend envia cooldown_min en MINUTOS (slider 5-60, label "min").
        # Antes se interpretaba como segundos -> con valor 5 habia spam cada 5s.
        # Ahora multiplicamos por 60. Default 5 min = 300s.
        _cooldown_min = int(_cam_cfg.get("cooldown_min", 5))
        if _cooldown_min < 1:
            _cooldown_min = 1
        if _cooldown_min > 1440:
            _cooldown_min = 1440  # cap 24h
        _cooldown_sec = _cooldown_min * 60
    except:
        _cooldown_sec = 300  # 5 min fallback
    _last = _vigilance_cooldowns.get(cam_key, 0)
    if now - _last < _cooldown_sec:
        logger.info(f"Vigilance alert suppressed (cooldown {_cooldown_sec}s): {camera_id}")
        return None
    _vigilance_cooldowns[cam_key] = now
    try:
        ts = now
        event_id = f"vigilance_{camera_id}_{ts}"
        events_dir = user_root(user_id) / "cameras" / camera_id / "events"
        events_dir.mkdir(parents=True, exist_ok=True)
        with open(events_dir / f"{event_id}.jpg", "wb") as f:
            f.write(img_bytes)
        with open(events_dir / "latest_vigilance.jpg", "wb") as f:
            f.write(img_bytes)
        event_data = {
            "event_id": event_id,
            "camera_id": camera_id,
            "user_id": user_id,
            "event_type": "vigilance_alert",
            "timestamp": ts,
            "yolo_count": yolo_count,
            "yolo_classes": yolo_classes,
            "source_ip": client_ip,
            "description": f"Modo centinela: {yolo_count} objeto(s) detectado(s): {', '.join(yolo_classes)}"
        }
        with open(events_dir / f"{event_id}.json", "w") as f:
            json.dump(event_data, f, indent=2, ensure_ascii=False)
        logger.info(f"Vigilance event saved: {event_id}")
        # Pre-codificar imagen miniatura (base64) para el push con foto
        _img_b64 = None
        try:
            from io import BytesIO
            from PIL import Image as _PILImage
            _im = _PILImage.open(BytesIO(img_bytes)).convert("RGB")
            _im.thumbnail((320, 240))
            _buf = BytesIO()
            _im.save(_buf, format="JPEG", quality=70)
            _img_b64 = base64.b64encode(_buf.getvalue()).decode("ascii")
        except Exception as _ie:
            logger.warning(f"[vigilance] No pude generar miniatura push: {_ie}")
        _send_vigilance_fcm(user_id, camera_id, event_id, yolo_count, yolo_classes, image_b64=_img_b64)
        return event_id
    except Exception as e:
        logger.error(f"Error saving vigilance event: {e}")
        return None


def _send_vigilance_fcm(user_id: str, camera_id: str, event_id: str, yolo_count: int, yolo_classes: list, image_b64: str = None):
    """Enviar notificación FCM de alerta de vigilancia."""
    try:
        uf = find_user_json(user_id)
        if not uf or not uf.exists():
            return
        with open(uf) as f:
            ud = json.load(f)
        tokens = ud.get("fcm_tokens", [])
        if not tokens:
            logger.info(f"No FCM tokens for user {user_id}")
            return
        cam_name = camera_id
        for c in ud.get("cameras", []):
            if c.get("camera_id") == camera_id:
                cam_name = c.get("name", camera_id)
                break
        title = f"🚨 Alerta: {cam_name}"
        body = f"Modo centinela activo. {yolo_count} objeto(s) detectado(s) fuera de horario."
        from orchestrator import send_fcm_notification
        # URL pública de la imagen del momento (servida por /vigilance-frame/)
        img_url = f"https://api.ojoia.com.do/vigilance-frame/{user_id}/{event_id}"
        for token in tokens[:3]:
            asyncio.create_task(send_fcm_notification(
                title=title,
                body=body,
                token=token,
                user_id=user_id,
                image_b64=image_b64,
                image_url=img_url,
                link=f"https://ojoia.com.do/#cameras?event={event_id}",
                # M6.1: dedupe por event_id - 3 alertas del mismo evento no apilan 3 notif
                tag=f"vigilance-{event_id}",
                event_id=event_id,
                notif_type="vigilance_alert"
            ))
        logger.info(f"Vigilance FCM queued to {len(tokens)} tokens (image_url={'yes' if img_url else 'no'})")
    except Exception as e:
        logger.error(f"Error sending vigilance FCM: {e}")


# F3: throttle de last_frame (por cámara). _LAST_FRAME_WRITE_TS sin lock es
# aceptable: es solo una heurística de frescura (peor caso = 2 escrituras
# simultáneas al mismo user.json, ya protegido por _get_user_lock dentro).
_LAST_FRAME_WRITE_TS: Dict[str, float] = {}
_LAST_FRAME_MIN_S = float(os.environ.get("OJOIA_LAST_FRAME_MIN_S", "5"))


def _update_camera_last_announce(user_id: str, camera_id: str, client_ip: str = None):
    """Registrar el announce de una cámara SIN tocar last_frame.

    fix (2026-09-04) 'En vivo' falso: los announces llegan cada ~20s aunque
    la cámara NO envíe frames (ej: ciclo OTA announce-only detectado hoy,
    frames muertos 1h+). Antes el announce pisaba last_frame → panel/app
    mostraban "En vivo" con la imagen congelada hace horas. Ahora:
      - last_announce = WiFi/pipeline de control vivo (diagnóstico)
      - last_frame   = frames reales llegando (estado online)
    Throttled 30s (el announce es cada ~20s; no necesita más resolución)."""
    if not user_id:
        return
    key = f"ann/{user_id}/{camera_id}"
    now = time.time()
    last = _LAST_FRAME_WRITE_TS.get(key, 0)
    if now - last < 30:
        return
    _LAST_FRAME_WRITE_TS[key] = now
    try:
        uf = find_user_json(user_id)
        if uf and uf.exists():
            with _get_user_lock(user_id):
                with open(uf) as f:
                    ud = json.load(f)
                for c in (ud.get("cameras") or []):
                    if c.get("camera_id") == camera_id:
                        c["last_announce"] = int(now)
                        if client_ip:
                            c["last_announce_ip"] = client_ip
                        break
                tmp = uf.with_suffix(".json.tmp")
                tmp.write_text(json.dumps(ud, indent=2, ensure_ascii=False))
                tmp.replace(uf)
    except Exception as e:
        logger.debug(f"[announce-timestamp] {user_id}/{camera_id}: {e}")


def _update_camera_last_frame(user_id: str, camera_id: str, client_ip: str = None):
    """Actualizar last_frame de una cámara en user.json. Auto-registra si no existe.

    fix (2026-09-04): SOLO el ingest real de frames debe llamar esta función.
    F3: throttled — por cámara solo escribe cada _LAST_FRAME_MIN_S (default 5s).
    El estado online usa umbral de 120s; escribir cada frame era I/O+lock x N cámaras."""
    if not user_id:
        return
    key = f"{user_id}/{camera_id}"
    now = time.time()
    last = _LAST_FRAME_WRITE_TS.get(key, 0)
    if now - last < _LAST_FRAME_MIN_S:
        return  # ya actualizado hace poco: el umbral online no lo nota
    _LAST_FRAME_WRITE_TS[key] = now
    try:
        uf = find_user_json(user_id)
        if uf and uf.exists():
            # [Fix B2] Encerrar todo el read-modify-write en _get_user_lock para
            # evitar race condition con _save_cam_config_to_user y otros
            # escritores de user.json. Sin lock, last_frame pisaba los
            # led_on/led_bright/etc escritos por el POST de la UI cada ~1s.
            with _get_user_lock(user_id):
                with open(uf) as f:
                    ud = json.load(f)
                cameras = ud.get("cameras", []) or []
                now = int(time.time())
                found = False
                for c in cameras:
                    if c.get("camera_id") == camera_id:
                        c["last_frame"] = now
                        if client_ip:
                            c["last_announce_ip"] = client_ip
                        found = True
                        break
                # Auto-registrar nueva cámara (basado en camera.json del FS)
                if not found and camera_id and camera_id != "unknown":
                    cam_cfg = get_camera_config_static(user_id, camera_id) if 'get_camera_config_static' in globals() else {}
                    cameras.append({
                        "camera_id": camera_id,
                        "name": cam_cfg.get("name") or camera_id,
                        "zone": cam_cfg.get("zone", ""),
                        "business_type": cam_cfg.get("business_type", ""),
                        "business_name": cam_cfg.get("business_name", ""),
                        "active": True,
                        "last_frame": now,
                        "last_announce_ip": client_ip or "",
                        "configured_at": cam_cfg.get("configured_at", now),
                    })
                    logger.info(f"[cameras] Auto-registrada cámara {camera_id} para user {user_id}")
                ud["cameras"] = cameras
                # [Fix B2] Escritura atómica (tmp + rename) sobre el lock:
                # reduce la ventana de carrera a casi cero.
                tmp = uf.with_suffix(".tmp")
                with open(tmp, "w") as f:
                    json.dump(ud, f, indent=2)
                tmp.replace(uf)
    except Exception as e:
        logger.error(f"Error updating camera last_frame: {e}")


@app.post("/api/cameras/{camera_id}/ingest-key/rotate")
async def rotate_ingest_key(camera_id: str, user_id: Optional[str] = None):
    """Genera una nueva ingest_key para la cámara y la guarda en camera.json.
    La key se devuelve UNA sola vez; el firmware se reprograma con ella.
    Auth: bearer del usuario (middleware A1 valida user_id del query)."""
    _validate_safe_path(camera_id, "camera_id")
    if not user_id:
        user_id = resolve_user_id(camera_id, None, None)
    if not user_id:
        raise HTTPException(status_code=404, detail="camera_id sin usuario asociado")
    cam_path = Path(STORAGE_ROOT) / "users" / user_id / "cameras" / camera_id / "camera.json"
    if not cam_path.exists():
        raise HTTPException(status_code=404, detail="camera.json no encontrado")
    new_key = secrets.token_urlsafe(32)
    async with _get_camera_lock(user_id, camera_id):
        with open(cam_path) as f:
            cfg = json.load(f)
        cfg["ingest_key"] = new_key
        cfg["ingest_key_rotated_at"] = time.time()
        tmp = cam_path.with_suffix(".json.tmp")
        with open(tmp, "w") as f:
            json.dump(cfg, f, indent=2)
        os.replace(tmp, cam_path)
    _invalidate_cam_cfg_cache(user_id, camera_id)  # F3
    logger.info(f"[A4] ingest_key rotada para {user_id}/{camera_id}")
    return {"success": True, "camera_id": camera_id, "ingest_key": new_key}


_INGEST_KEY_WARN_TS: dict = {}  # camera_id -> ts del último warning (rate-limit de log)

# F1-bis (2026-08-31) — IP pinning TOFU para cámaras SIN firmware nuevo.
# Las ESP32 en producción no pueden mandar X-Camera-Key (el firmware actual
# no lo soporta y no hay acceso al dispositivo ahora). La IP de origen TCP
# NO se puede falsificar: la cámara llega con la IP pública real del negocio
# (verificado: 152.0.63.68, Claro RD — el túnel Cloudflare preserva la IP).
#
# Política de autenticación de ingesta (orden):
#   1. camera.json tiene "ingest_key" → exige header X-Camera-Key (A4).
#   2. Si no, camera.json tiene "ingest_allowed_ips" (TOFU) → solo esas IPs.
#      - Se aprenden automáticamente las primeras N ips (OJOIA_TOFU_MAX, 2)
#      - IP fuera de la lista → 403 + push de aviso al dueño (1/día).
#   3. Si no hay nada → modo legado abierto + warning rate-limited (como A4).
_INGEST_IP_WARN_TS: dict = {}
_INGEST_TOFU_MAX = int(os.getenv("OJOIA_TOFU_MAX", "2"))
_INGEST_IP_NOTIFY_TS: dict = {}
_FW_SEEN: dict = {}  # "user/cam" -> última versión de firmware reportada


def _client_real_ip(request: Request) -> str:
    """IP real del cliente. Detrás de cloudflared, el conector local hace las
    conexiones desde la IP original (config del túnel sin proxying HTTP en el
    medio); si algún día hay proxy HTTP delante, respetamos CF-Connecting-IP."""
    cip = request.headers.get("cf-connecting-ip") or request.headers.get("x-forwarded-for")
    if cip:
        return cip.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _learn_or_check_ingest_ip(cam_cfg: dict, camera_id: str, client_ip: str,
                              user_id: str = "") -> bool:
    """F1-bis: TOFU sobre ingest_allowed_ips. Devuelve True si pasa.

    Aprende la IP en la ventana inicial (hasta OJOIA_TOFU_MAX) y después
    bloquea IPs desconocidas. Cualquier cambio de IP legítimo (ISP renovó
    la IP del negocio) se aprueba con el endpoint rotate/relearn."""
    allowed = cam_cfg.get("ingest_allowed_ips")
    if not isinstance(allowed, list) or not allowed:
        return True  # sin política configurada: modo legado
    if client_ip in allowed:
        return True
    # IP nueva: ¿ventana TOFU abierta?
    if len(allowed) < _INGEST_TOFU_MAX and not cam_cfg.get("ingest_ip_locked"):
        allowed.append(client_ip)
        cam_cfg["ingest_allowed_ips"] = allowed
        _persist_cam_cfg_field(user_id, camera_id, "ingest_allowed_ips", allowed)
        logger.warning(f"[F1-bis] TOFU: IP {client_ip} aprendida para {camera_id}")
        return True
    return False


def _persist_cam_cfg_field(user_id: str, camera_id: str, field: str, value):
    """Escribe un campo en camera.json (atómico) e invalida la caché F3."""
    try:
        cam_path = Path(STORAGE_ROOT) / "users" / user_id / "cameras" / camera_id / "camera.json"
        if not cam_path.exists():
            return
        with open(cam_path) as f:
            cfg = json.load(f)
        cfg[field] = value
        tmp = cam_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(cfg, indent=2, ensure_ascii=False))
        tmp.replace(cam_path)
        _invalidate_cam_cfg_cache(user_id, camera_id)
    except Exception as e:
        logger.warning(f"[F1-bis] persist {field} para {camera_id}: {e}")

# C4 (2026-08-31) — Rate limit de ingesta por cámara.
# Una cámara ESP32 normal envía ~1 fps. Sin límite, una cámara buggueada o
# un atacante con ingest_key podía saturar FRAME_QUEUE, YOLO y la GPU.
# Bucket simple: mínimo `interval = 1/max_fps` entre frames aceptados.
_INGEST_LAST_TS: Dict[str, float] = {}
_INGEST_RATE_DROPS: Dict[str, int] = {}


def _ingest_rate_ok(camera_id: str, cam_cfg: dict) -> bool:
    """True si el frame entra dentro del rate permitido para esta cámara.
    max_fps configurable por cámara en camera.json ("max_fps"), default 5.
    Los rechazados se cuentan para métricas (/health los expone)."""
    try:
        max_fps = float((cam_cfg or {}).get("max_fps") or 5)
    except (TypeError, ValueError):
        max_fps = 5.0
    min_interval = 1.0 / max(max_fps, 0.1)
    now = time.time()
    last = _INGEST_LAST_TS.get(camera_id, 0.0)
    if now - last < min_interval:
        _INGEST_RATE_DROPS[camera_id] = _INGEST_RATE_DROPS.get(camera_id, 0) + 1
        return False
    _INGEST_LAST_TS[camera_id] = now
    return True


def _enforce_ingest_key(request: Request, cam_cfg: dict, camera_id: str, user_id: str = ""):
    """A4 — Autenticación de ingesta por cámara.

    Antes, /ingest/* era público y solo se filtraba por camera_id conocido:
    cualquiera que adivinara un camera_id podía inyectar frames falsos
    (alertas falsas) o saturar la cola (DoS).

    Ahora: si camera.json tiene `ingest_key`, el frame debe traer el header
    `X-Camera-Key` con el mismo valor (comparación constante). Si la cámara
    aún no tiene key configurada, se permite (retrocompatible con firmware
    actual) pero se loguea un warning rate-limited para forzar la migración.
    """
    expected = (cam_cfg or {}).get("ingest_key") or ""
    if not expected:
        # F1-bis: sin ingest_key → IP pinning TOFU. La primera IP que veamos
        # queda pineada; después, cualquier IP desconocida → 403.
        allowed = (cam_cfg or {}).get("ingest_allowed_ips")
        client_ip = _client_real_ip(request)
        if not (isinstance(allowed, list) and allowed):
            # Primera IP vista = IP legítima del negocio. Se pinea Y se cierra
            # la ventana (locked): una segunda IP distinta no se aprende sola,
            # la aprueba el dueño vía /api/cameras/{id}/ingest-ips (action=add)
            # o reset si cambió de ISP.
            cam_cfg["ingest_allowed_ips"] = [client_ip]
            cam_cfg["ingest_ip_locked"] = True
            _persist_cam_cfg_field(user_id, camera_id, "ingest_allowed_ips", [client_ip])
            _persist_cam_cfg_field(user_id, camera_id, "ingest_ip_locked", True)
            logger.info(f"[F1-bis] TOFU: primera IP {client_ip} pineada+locked para {camera_id}")
            return
        if not _learn_or_check_ingest_ip(cam_cfg, camera_id, client_ip, user_id):
            logger.warning(f"[F1-bis] ingest RECHAZADO: IP {client_ip} no pineada "
                           f"para {camera_id} (permitidas: {allowed})")
            raise HTTPException(status_code=403,
                                detail="IP no autorizada para esta cámara. "
                                       "Apruébala desde /api/cameras/{id}/ingest-ips")
        return
    provided = request.headers.get("x-camera-key", "")
    if not provided or not hmac.compare_digest(str(expected), str(provided)):
        logger.warning(f"[A4] ingest RECHAZADO: X-Camera-Key inválida para {camera_id} "
                       f"desde {request.client.host if request.client else '?'}")
        raise HTTPException(status_code=401, detail="X-Camera-Key inválida o ausente")


# ── HOT-COLD (2026-09-02): lo efímero en NVMe con path FIJO ────────────────────
# latest_raw.jpg / latest_yolo.json se sobreescriben por frame: no son datos,
# son ESTADO. Viven en el NVMe (rápido, path fijo) — el viewer/silueta lee
# aquí SIEMPRE, imposible de romper con migraciones de disco (el bug de hoy
# x3). Los EVENTOS (lo que crece) siguen en el disco del usuario.
HOT_ROOT = Path(STORAGE_ROOT) / "hot"


def hot_dir(user_id: str, camera_id: str) -> Path:
    """Path FIJO del hot-storage de una cámara (NVMe). Nunca migra."""
    d = HOT_ROOT / (user_id or "default") / (camera_id or "unknown")
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write_frame_files(frames_dir_v: Path, img_bytes: bytes, user_id: str, camera_id: str):
    """C7: escritura síncrona del frame (corre en thread pool vía to_thread)."""
    frames_dir_v.mkdir(parents=True, exist_ok=True)
    with open(frames_dir_v / "latest_raw.jpg", "wb") as f:
        f.write(img_bytes)


def _write_json_atomic(path: Path, data, **kwargs):
    """C7: JSON write atómico (tmp+rename) para llamar vía to_thread."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=kwargs.get("indent", 2), ensure_ascii=False))
    tmp.replace(path)


async def _process_ingest(request: Request, camera_id: str, user_id: str, image: UploadFile):
    """Flujo OPTIMIZADO: guardar + YOLO rápido + encolar grid/Qwen + responder.
    
    Regla de oro: NUNCA se descarta imagen. Frame original SIEMPRE se guarda.
    - YOLO corre SÍNCRONO para que el viewer vea siluetas en tiempo real (~100-200ms).
    - Grid/Qwen corre en background (worker) para no bloquear al ESP32.
    - Modo centinela: alerta directa si YOLO detecta personas fuera de horario.
    """
    try:
        client_ip = request.client.host if request.client else "unknown"
        user_id = resolve_user_id(camera_id, user_id, client_ip)
        if camera_id == "unknown":
            camera_id = await _resolve_unknown_camera(user_id, client_ip)

        # ── [0] AUTENTICACIÓN DE INGESTA POR CÁMARA (A4) ──
        # Se verifica ANTES de guardar/procesar nada: si la cámara tiene
        # ingest_key configurada y el header no coincide, se rechaza aquí.
        _cam_cfg_ingest = get_camera_config_static(user_id, camera_id)
        _enforce_ingest_key(request, _cam_cfg_ingest, camera_id, user_id)

        # ── [0b] RATE LIMIT por cámara (C4) — antes de leer/gastar nada ──
        if not _ingest_rate_ok(camera_id, _cam_cfg_ingest):
            raise HTTPException(status_code=429,
                                detail=f"Rate limit: máximo {(_cam_cfg_ingest or {}).get('max_fps', 5)} fps por cámara")

        # ── [0c] ENFORCEMENT DE PLAN (P0-billing) ──────────────────────────
        # Un suspendido/vencido-sin-gracia ya NO consume YOLO/Qwen ni disco:
        # se rechaza el frame ANTES de guardar/gastar. Política suave en
        # gracia y trial (allowed) — solo corta el servicio realmente
        # suspendido. El ESP32 recibe 403 con reason y reintenta luego.
        try:
            _ud_pf = _load_user_data_for_ingest(user_id)
            if _ud_pf is not None:
                _pf = _enforce_plan_on_ingest(_ud_pf)
                if not _pf.get("allowed"):
                    logger.info(f"[plan-enforce] ingest bloqueado user={user_id[:8]}… "
                                f"cam={camera_id}: {_pf.get('reason','')}")
                    raise HTTPException(status_code=403,
                                        detail=f"Servicio suspendido: {_pf.get('reason','plan vencido')}")
        except HTTPException:
            raise

        # ── [0d] TELEMETRÍA de firmware (X-Firmware del ESP32) ──────────────
        # El firmware v9.3.1+ reporta su versión en cada frame. La guardamos
        # en camera.json (throttled) para saber qué corre cada cámara sin
        # acceso físico ni OTA.
        _fw = (request.headers.get("x-firmware") or "").strip()
        if _fw:
            _fw_key = f"{user_id}/{camera_id}"
            _last_fw = _FW_SEEN.get(_fw_key)
            if _last_fw != _fw:
                _FW_SEEN[_fw_key] = _fw
                _persist_cam_cfg_field(user_id, camera_id, "firmware_version", _fw)
                _persist_cam_cfg_field(user_id, camera_id, "firmware_seen_at", time.time())
                logger.info(f"[FW] {camera_id} reporta firmware {_fw}")

        img_bytes = await image.read()
        frame_size = len(img_bytes)
        now_dt = datetime.now()
        frame_id = f"{camera_id}_{int(time.time()*1000)}"
        mode = "normal"
        processing = "queued"  # F3: YOLO va en el worker
        yolo_count = 0
        yolo_classes = []
        yolo_detections = []
        is_vigilante = False

        # ── [1] GUARDAR FRAME ORIGINAL (siempre) ──
        # HOT-COLD: el latest_raw va al HOT (NVMe path fijo — el viewer
        # siempre lo encuentra ahí) Y al layout del disco del usuario
        # (transición: durante unos días ambos, hasta confirmar estabilidad).
        async def _save_frame_disk():
            frames_dir_v = user_root(user_id) / "cameras" / camera_id / "frames"
            hot = hot_dir(user_id, camera_id)
            def _both():
                _write_frame_files(frames_dir_v, img_bytes, user_id, camera_id)
                _write_frame_files(hot, img_bytes, user_id, camera_id)
            await asyncio.to_thread(_both)
        try:
            await _save_frame_disk()
            # Cache en RAM para MJPEG stream
            _cache_frame(user_id, camera_id, img_bytes)
            # ── EVA WIZARD: alimentar el buffer de frames de Eva para que el
            # wizard de "instalar cámara nueva" pueda detectar el frame de la
            # cámara física nueva (que todavía no está registrada en user.json).
            # Sin esto, _get_unconfigured_frame en eva_v2.py nunca encuentra el
            # frame y el wizard se queda bucleando "Esperando imagen...".
            try:
                from eva_v2 import ingest_frame_for_eva
                ingest_frame_for_eva(img_bytes, camera_id)
            except Exception as ingest_e:
                logger.debug(f"ingest_frame_for_eva skip: {ingest_e}")
        except Exception as e:
            logger.error(f"Error guardando frame: {e}")

        # ── [2] Leer config y determinar modo centinela ──
        cam_cfg = get_camera_config_static(user_id, camera_id) or {}
        vigilance = cam_cfg.get("vigilance", {})
        schedule = cam_cfg.get("schedule") or {}
        if not schedule:
            try:
                _uf = find_user_json(user_id)
                if _uf and _uf.exists():
                    with open(_uf) as _f:
                        _ud = json.load(_f)
                    schedule = _ud.get("schedule", {})
            except:
                logger.debug("silent except")

        current_time = now_dt.strftime("%H:%M")
        is_vigilante = _is_vigilante_mode(schedule, vigilance, current_time, cam_cfg.get("night_mode", False), user_id=user_id, camera_id=camera_id)
        mode = "vigilante" if is_vigilante else "normal"

        # ── [3] F3: SIN YOLO SÍNCRONO — el request no paga GPU ─────────────
        # El frame se encola con yolo_count=-1 (PENDIENTE). El worker hace
        # YOLO en batches (XREADGROUP count=16 → 1 forward por tanda), aplica
        # el gate, escribe latest_yolo.json y dispara el centinela.
        # Resultado: el ingest responde en ~15-30ms aunque haya 200 cámaras.
        yolo_count = -1  # -1 = pendiente de YOLO en el worker
        yolo_classes: list = []
        yolo_detections: list = []

        # ── [4] (movido al worker) latest_yolo.json lo escribe el worker ──

        # ── [5] (movido al worker) centinela tras YOLO batched ──

        # ── [6] ENCOLAR SIEMPRE (el gate de YOLO lo aplica el worker) ─────
        # Antes: solo se encolaba si yolo_count>0 (gate en request, caro).
        # Ahora: el gate corre en el worker con batch GPU compartido.
        frameData = {
            "frame_id": frame_id,
            "user_id": user_id,
            "camera_id": camera_id,
            "img_bytes": img_bytes,
            "timestamp": time.time(),
            "client_ip": client_ip,
            "cam_cfg": cam_cfg,
            "schedule": schedule,
            "vigilance": vigilance,
            "mode": mode,
            "yolo_count": -1,   # F3: YOLO pendiente en worker
            "yolo_classes": [],
            "yolo_detections": []
        }
        # C2: encolar via bus (Redis Streams o memoria). put() nunca
        # bloquea al ESP32; si la cola está presionada, descarta + cuenta.
        ok = await frame_bus.put(frameData)
        if ok:
            processing = "queued"
        else:
            global FRAME_QUEUE_DROPS, FRAME_QUEUE_DROP_TS
            FRAME_QUEUE_DROPS += 1
            processing = "dropped"
            if time.time() - FRAME_QUEUE_DROP_TS > 30:
                FRAME_QUEUE_DROP_TS = time.time()
                logger.warning(
                    f"[FRAME_QUEUE] lleno, frame descartado: {frame_id} "
                    f"(cam={camera_id} user={user_id} mode={mode}) "
                    f"total_drops={FRAME_QUEUE_DROPS} bus_mode={frame_bus.mode}"
                )

        # ── [7] Actualizar last_frame de la cámara ──
        _update_camera_last_frame(user_id, camera_id, client_ip)

        # ── [8] Responder RÁPIDO al ESP32 / viewer ──
        return {
            "success": True,
            "camera_id": camera_id,
            "user_id": user_id,
            "client_ip": client_ip,
            "frame_size": frame_size,
            "mode": mode,
            "timestamp": now_dt.isoformat(),
            "frame_id": frame_id,
            "processing": processing,
            "queue_size": frame_bus.qsize(),
            # F3: YOLO corre en el worker (batched). El viewer consulta
            # latest_yolo.json via /frames/latest_yolo (poll existente).
            "yolo": {"count": -1, "pending": True}
        }

    except HTTPException:
        # Errores intencionales (401 key inválida, 429 rate limit) deben
        # llegar al cliente con su código real, no tragarse como 200.
        raise
    except Exception as e:
        logger.error(f"Error en _process_ingest: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


# ═══════════════════════════════════════════════════════════════════════════
# WORKER YOLO — Procesamiento asíncrono en background
# ═══════════════════════════════════════════════════════════════════════════

# C6 (2026-08-31) — Micro-batching YOLO.
# Antes: cada frame hacía 1 llamada YOLO individual (1 imagen = 1 forward GPU).
# En ráfagas (N cámaras a la vez) eso desperdicia la GPU: ultralytics puede
# hacer UN forward con N imágenes. Este batcher colecta los frames que llegan
# en una ventana de ~150ms y los manda juntos a /detect_batch.
# La latencia extra por frame es ≤150ms; el throughput sube hasta ~8x(16/batch).
_YOLO_BATCH_MAX = 16
_YOLO_BATCH_WINDOW_S = 0.15
_YOLO_PENDING: list = []          # [(img_bytes, camera_id, future)]
_YOLO_PENDING_LOCK = asyncio.Lock() if sys.version_info >= (3, 10) else None
_YOLO_BATCH_TASK = None
_YOLO_BATCH_STATS = {"batches": 0, "images": 0, "errors": 0}


async def _yolo_batch_flush_items(items):
    """Ejecuta /detect_batch con una lista de (img_bytes, cam, future) y
    resuelve cada future con (count, classes, detections). Fallback: single."""
    if not items:
        return
    try:
        data = [("images", (f"f{i}.jpg", b, "image/jpeg")) for i, (b, _c, _f) in enumerate(items)]
        data.append(("camera_ids", (None, ",".join(c or f"cam{i}" for i, (_b, c, _f) in enumerate(items)))))
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post("http://localhost:8002/detect_batch", files=data)
            results = resp.json().get("results", []) if resp.status_code == 200 else []
        # /detect_batch devuelve results EN EL MISMO ORDEN que las imágenes
        # enviadas → mapeo posicional (una cámara puede aparecer 2 veces en
        # un batch durante ráfagas; con by_cam colisionarían).
        for idx, (_b, cam, fut) in enumerate(items):
            r = results[idx] if idx < len(results) and isinstance(results[idx], dict) else {}
            dets = [d for d in r.get("detections", []) if d.get("confidence", 0) >= 0.25]
            uniq = {d.get("track_id") for d in dets if d.get("track_id")}
            if not fut.done():
                fut.set_result((len(uniq) if uniq else len(dets),
                                [d.get("class", "") for d in dets], dets))
        _YOLO_BATCH_STATS["batches"] += 1
        _YOLO_BATCH_STATS["images"] += len(items)
    except Exception as e:
        _YOLO_BATCH_STATS["errors"] += 1
        logger.warning(f"[C6] batch YOLO falló ({e}); fallback individual")
        for b, cam, fut in items:
            if not fut.done():
                try:
                    fut.set_result(await _run_yolo_detection(b))
                except Exception as e2:
                    fut.set_exception(e2)


async def _yolo_batch_loop():
    """Vacía la ventana de micro-batching cada _YOLO_BATCH_WINDOW_S."""
    while True:
        await asyncio.sleep(_YOLO_BATCH_WINDOW_S)
        try:
            async with _YOLO_PENDING_LOCK:
                items, _YOLO_PENDING[:] = _YOLO_PENDING[:], []
            await _yolo_batch_flush_items(items)
        except Exception as e:
            logger.error(f"[C6] batch loop error: {e}")


async def _yolo_detect(img_bytes: bytes, camera_id: str = "") -> tuple:
    """YOLO con micro-batching. Si el batcher no está activo, fallback directo."""
    global _YOLO_BATCH_TASK
    try:
        if _YOLO_BATCH_TASK is None or _YOLO_BATCH_TASK.done():
            _YOLO_BATCH_TASK = asyncio.create_task(_yolo_batch_loop())
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        burst = None
        async with _YOLO_PENDING_LOCK:
            _YOLO_PENDING.append((img_bytes, camera_id or "cam", fut))
            if len(_YOLO_PENDING) >= _YOLO_BATCH_MAX:
                burst, _YOLO_PENDING[:] = _YOLO_PENDING[:], []
        if burst:
            asyncio.create_task(_yolo_batch_flush_items(burst))
        return await asyncio.wait_for(fut, timeout=15)
    except Exception as e:
        logger.warning(f"[C6] batch path error ({e}); directo")
        return await _run_yolo_detection(img_bytes)


async def _yolo_batch_http(imgs: list, cams: list) -> list:
    """F3: UN forward YOLO para una tanda completa vía /detect_batch.
    Devuelve lista de resultados EN EL MISMO ORDEN que imgs."""
    data = [("images", (f"f{i}.jpg", b, "image/jpeg")) for i, b in enumerate(imgs)]
    data.append(("camera_ids", (None, ",".join(c or f"cam{i}" for i, c in enumerate(cams)))))
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post("http://localhost:8002/detect_batch", files=data)
        if resp.status_code != 200:
            raise RuntimeError(f"detect_batch HTTP {resp.status_code}")
        results = resp.json().get("results", [])
    if len(results) < len(imgs):
        results += [{}] * (len(imgs) - len(results))
    return results


def _yolo_parse_result(r: dict) -> tuple:
    """F3: extrae (count, classes, detections) de UN resultado de batch."""
    dets = [d for d in (r or {}).get("detections", []) if d.get("confidence", 0) >= 0.25]
    uniq = {d.get("track_id") for d in dets if d.get("track_id")}
    count = len(uniq) if uniq else len(dets)
    return count, [d.get("class", "") for d in dets], dets


# ═══════════════════════════════════════════════════════════════════════════
# FASE 2 (F2.2): asignación GEOMÉTRICA de zonas — bbox YOLO ∩ rect zona
# ═══════════════════════════════════════════════════════════════════════════
# Antes las zonas viajaban al prompt de Qwen solo como texto con coords y el
# modelo deducía "en qué zona está cada persona". Ahora el servidor calcula
# la intersección bbox∩zona y la inyecta como DATO DE SENSOR (ya confirmado).
# El modelo no deduce: recibe el hecho. Menos alucinación, más precisión.

def _detect_zone_for_bbox(bbox: list, img_w: int, img_h: int, zones: list) -> Optional[dict]:
    """Devuelve la zona con mayor solape con el bbox (intersección sobre área del bbox).

    bbox: [x1, y1, x2, y2] en píxeles. zones: rects relativos 0-1 {x,y,w,h}.
    Umbral mínimo: 25% del bbox dentro de la zona para considerarla.
    """
    if not bbox or len(bbox) != 4 or not zones or not img_w or not img_h:
        return None
    x1, y1, x2, y2 = bbox
    if x2 <= x1 or y2 <= y1:
        return None
    bbox_area = (x2 - x1) * (y2 - y1)
    best = None
    best_ratio = 0.0
    for z in zones:
        c = z.get("coords") or {}
        try:
            zx1 = float(c.get("x", 0)) * img_w
            zy1 = float(c.get("y", 0)) * img_h
            zx2 = zx1 + float(c.get("w", 0)) * img_w
            zy2 = zy1 + float(c.get("h", 0)) * img_h
        except (TypeError, ValueError):
            continue
        iw = max(0.0, min(x2, zx2) - max(x1, zx1))
        ih = max(0.0, min(y2, zy2) - max(y1, zy1))
        inter = iw * ih
        if inter <= 0:
            continue
        ratio = inter / bbox_area
        if ratio > best_ratio:
            best_ratio = ratio
            best = z
    return best if best_ratio >= 0.25 else None


def _assign_zones_to_detections(user_id: str, camera_id: str,
                                yolo_detections: list, img_bytes: bytes) -> dict:
    """F2.2: anota cada detección con zone_name y construye resumen por zonas.

    Devuelve {"per_detection": {track_id|idx: zone_name},
              "zone_counts": {zone_name: n_personas},
              "img_size": [w, h]} — listo para inyectar al frame del grid.
    B5: para la ASIGNACIÓN DE ZONA se exige conf de persona >= 0.50 (el gate
    YOLO general queda en 0.35): una persona con detección débil en el borde
    de una zona no debe contaminar el zone_counts que viaja al prompt como
    HECHO confirmado.
    """
    out = {"per_detection": {}, "zone_counts": {}, "img_size": [0, 0]}
    if not yolo_detections:
        return out
    zones = camera_zones.get_camera_zones_cached(user_id, camera_id)
    if not zones:
        return out
    img_w = img_h = 0
    try:
        import io as _io
        from PIL import Image as _PILImage
        with _PILImage.open(_io.BytesIO(img_bytes)) as im:
            img_w, img_h = im.size
    except Exception:
        # Fallback: estimar del primer bbox (mala opción, pero mejor que nada)
        return out
    if not img_w or not img_h:
        return out
    out["img_size"] = [img_w, img_h]

    zone_counts = {}
    for i, det in enumerate(yolo_detections):
        if str(det.get("class", "")).lower() != "person":
            continue
        # B5: conf alta para asignación de zona (hecho confirmado al prompt)
        try:
            if float(det.get("confidence", 0)) < 0.50:
                continue
        except (TypeError, ValueError):
            continue
        z = _detect_zone_for_bbox(det.get("bbox"), img_w, img_h, zones)
        if z:
            zname = z.get("name") or z.get("type") or "zona"
            key = det.get("track_id") if det.get("track_id") is not None else i
            out["per_detection"][str(key)] = zname
            zone_counts[zname] = zone_counts.get(zname, 0) + 1
    out["zone_counts"] = zone_counts
    return out


async def _run_yolo_detection(img_bytes: bytes) -> tuple:
    """Ejecuta YOLO detection y retorna (count, classes, detections)."""
    yolo_count = 0
    yolo_classes = []
    yolo_detections = []
    
    try:
        yolo_bytes = _adjust_brightness(img_bytes)
        async with httpx.AsyncClient(timeout=10) as client:
            yolo_resp = await client.post(
                "http://localhost:8002/detect",
                files={"image": ("frame.jpg", yolo_bytes, "image/jpeg")},
            )
            if yolo_resp.status_code == 200:
                yolo_data = yolo_resp.json()
                for d in yolo_data.get("detections", []):
                    if d.get("confidence", 0) >= 0.25:
                        yolo_classes.append(d.get("class", ""))
                        yolo_detections.append(d)
                unique_track_ids = set(d.get("track_id") for d in yolo_detections if d.get("track_id"))
                yolo_count = len(unique_track_ids) if unique_track_ids else len(yolo_detections)
    except Exception as e:
        logger.warning(f"YOLO detection error: {e}")
    
    return yolo_count, yolo_classes, yolo_detections


# ── NV (2026-09-03): Auto-LED con mensaje honesto ────────────────────────────
_LED_STATE = {}  # cam_key -> {"on": bool, "since": ts, "still_dark": bool}


def _auto_led_check(user_id: str, camera_id: str, img_bytes: bytes,
                    yolo_count: int) -> None:
    """El worker decide el LED: frame oscuro + personas → encender flash.
    Frame claro → apagar. Mensaje honesto: si CON LED sigue oscuro, el
    evento de oscuridad lo dice ('aun con la luz de la cámara encendida').
    NV: usa el brillo del frame RESCATADO por CLAHE (post _adjust_brightness
    ya corre antes en el batch) — medimos el raw para la decisión de LED.
    """
    try:
        from PIL import Image, ImageStat
        import io as _io
        g = Image.open(_io.BytesIO(img_bytes)).convert("L")
        avg = ImageStat.Stat(g).mean[0]
    except Exception:
        return
    key = f"{user_id}_{camera_id}"
    st_ = _LED_STATE.get(key, {"on": False, "since": 0, "still_dark": False})
    dark = avg < 45
    bright_enough = avg > 90

    def _set_led(on: bool):
        try:
            def _mut(ud):
                for c in ud.get("cameras", []):
                    if c.get("camera_id") == camera_id:
                        # NV-fix: led_auto SIGUE TRUE (la capa A del firmware —
                        # reacción local por frame, instantánea) y el server
                        # añade su one-shot led_on como capa B coordinada.
                        c["led_bright"] = c.get("led_bright") or 200
                        if on:
                            c["led_on"] = True    # one-shot: encender ya
                        # apagar NO se fuerza por one-shot: el firmware
                        # (autoLED bright>140) lo hace solo al haber luz —
                        # el server solo asegura brillo del flash.
            update_user_json(user_id, _mut)
        except Exception:
            pass

    if dark and yolo_count > 0 and not st_["on"]:
        # hay personas en la oscuridad → encender la luz de la cámara
        _set_led(True)
        st_["on"] = True
        st_["since"] = time.time()
        st_["still_dark"] = True  # el frame actual sigue oscuro (pre-LED)
        logger.info(f"[NV] {camera_id}: oscuro({avg:.0f}) + {yolo_count} personas → "
                    f"LED ON (mensaje: 'encendí la luz de la cámara')")
    elif st_["on"] and dark and time.time() - st_["since"] > 120:
        # LED encendido >2min y SIGUE oscuro → luz insuficiente, avisar
        st_["still_dark"] = True
        logger.warning(f"[NV] {camera_id}: LED encendido pero brillo {avg:.0f} — "
                       f"luz de cámara insuficiente")
    elif bright_enough and st_["on"]:
        # ya hay luz (natural o el LED ayudó) → apagar para ahorrar
        _set_led(False)
        st_["on"] = False
        st_["still_dark"] = False
        logger.info(f"[NV] {camera_id}: brillo {avg:.0f} OK → LED OFF")
    _LED_STATE[key] = st_


async def yolo_worker():
    """F3 — Worker batched: YOLO por tandas + grid + Qwen.

    Flujo (arquitectura ganadora, 2026-08-31):
    1. XREADGROUP count=BATCH (hasta 16 frames de una vez)
    2. UN solo forward YOLO para toda la tanda (/detect_batch)
    3. Gate por frame: yolo_count==0 → ack y descartar (no llega a Qwen)
    4. Centinela: modo vigilante + count>0 → _save_vigilance_event + ack
    5. Resto → grid por cámara (lock por cámara) → grid lleno → process_grid (Qwen)
    6. latest_yolo.json por cámara (viewer poll)

    El request HTTP de ingest ya NO paga YOLO (responde ~15-30ms).
    """
    global WORKER_RUNNING
    WORKER_RUNNING = True
    logger.info("🔧 YOLO Worker (batched F3) iniciado")

    consumer_name = f"{os.uname().nodename}-{os.getpid()}-w{id(asyncio.current_task()) % 997}"
    BATCH = 16  # F3: tamaño de tanda YOLO (= máx forward batch del yolo_server)
    # F3: tamaño del grid por cámara. 16 = análisis cada ~16s por cámara;
    # 32 = mitad de llamadas Qwen/s (para nodos con muchas cámaras).
    # El techo real es Qwen: N_cams × fps / GRID_SIZE grids/s, cada uno 2-5s GPU.
    GRID_SIZE = int(os.getenv("OJOIA_GRID_SIZE", "16"))

    from orchestrator import orchestrator  # import único, no por frame

    while True:
        batch_items = []
        try:
            # 1. Jalar una tanda del stream (bloquea hasta 1s si no hay nada)
            got = await frame_bus.get(consumer_name, count=BATCH)
            if not got:
                # F3: rescatar huérfanos Y PROCESARLOS (fix: antes quedaban pending)
                got = await frame_bus.rescue_stale(consumer_name)
                if not got:
                    continue
            batch_items = got

            # 2. YOLO batched para TODA la tanda en un solo forward GPU.
            #    (brightness adjust por imagen, como el path single original)
            try:
                imgs = []
                cams = []
                for _mid, fd in batch_items:
                    imgs.append(_adjust_brightness(fd.get("img_bytes") or b""))
                    cams.append(fd.get("camera_id") or "cam")
                results = await _yolo_batch_http(imgs, cams)
            except Exception as e:
                logger.warning(f"[F3] batch YOLO falló ({e}); frames pasan sin gate")
                results = [{}] * len(batch_items)

            # 3. Procesar cada frame con su resultado
            for (msg_id, frame_data), yres in zip(batch_items, results):
                try:
                    yolo_count, yolo_classes, yolo_detections = _yolo_parse_result(yres)
                except Exception:
                    yolo_count, yolo_classes, yolo_detections = 0, [], []

                frame_id = frame_data.get("frame_id")
                user_id = frame_data.get("user_id")
                camera_id = frame_data.get("camera_id")
                img_bytes = frame_data.get("img_bytes")
                # fix (2026-09-01): frames viejos en el stream pueden traer
                # cam_cfg como string vacío (None serializado) → crash
                # 'str' object has no attribute get' en el worker entero.
                _cc = frame_data.get("cam_cfg")
                cam_cfg = _cc if isinstance(_cc, dict) else {}
                _sc = frame_data.get("schedule")
                schedule = _sc if isinstance(_sc, dict) else {}
                _vg = frame_data.get("vigilance")
                vigilance = _vg if isinstance(_vg, dict) else {}
                mode = frame_data.get("mode", "normal")

                # NV: auto-LED (oscuridad + personas → flash de la cámara)
                try:
                    _auto_led_check(user_id, camera_id, img_bytes, yolo_count)
                except Exception:
                    pass

                # latest_yolo.json para el viewer — HOT (path fijo NVMe) +
                # disco del usuario (transición). El bug de la silueta era
                # el viewer leyendo el disco viejo tras migración: ahora el
                # hot es la fuente primaria del viewer.
                try:
                    await asyncio.to_thread(_write_json_atomic, hot_dir(user_id, camera_id) / "latest_yolo.json", {
                        "timestamp": time.time(),
                        "count": yolo_count,
                        "detections": yolo_detections,
                        "classes": yolo_classes,
                        "mode": mode,
                    })
                except Exception as e:
                    logger.debug(f"latest_yolo write: {e}")

                # Gate YOLO: sin objetos → descartar (no llega al grid/Qwen)
                if yolo_count <= 0:
                    logger.info(f"YOLO gate: 0 objects → frame REJECTED (frame_id={frame_id})")
                    await frame_bus.ack(msg_id)
                    continue

                # B4: alerta de imagen oscura (con cooldown 1h por cámara).
                # El gate ya pasó: hay contenido YOLO pero la imagen puede seguir
                # siendo demasiado oscura para el análisis VLM.
                try:
                    if img_bytes:
                        await _maybe_alert_dark_frame(user_id, camera_id, img_bytes)
                except Exception as _e_dark:
                    logger.debug(f"[B4] dark check falló: {_e_dark}")

                # Centinela (movido del endpoint [5]): modo vigilante + detección
                current_time = datetime.now().strftime("%H:%M")
                is_vigilante = _is_vigilante_mode(schedule, vigilance, current_time,
                                                  cam_cfg.get("night_mode", False),
                                                  user_id=user_id, camera_id=camera_id)
                if is_vigilante:
                    logger.warning(f"MODO CENTINELA: {yolo_count} objects {yolo_classes} → alerta directa")
                    _save_vigilance_event(user_id, camera_id, img_bytes, yolo_count,
                                          yolo_classes, frame_data.get("client_ip", ""))
                    await frame_bus.ack(msg_id)
                    continue

                # F2.2: asignación GEOMÉTRICA de zonas (bbox∩rect) — el hecho
                # calculado viaja al grid; Qwen lo recibe como dato de sensor.
                zone_assignment = {}
                try:
                    zone_assignment = _assign_zones_to_detections(
                        user_id, camera_id, yolo_detections, img_bytes or b"")
                except Exception as _e_zone:
                    logger.debug(f"[F2.2] zone assign falló: {_e_zone}")

                # Grid por cámara (lock → add_frame → capture si lleno)
                grid = orchestrator._get_grid(user_id, camera_id, grid_size=GRID_SIZE)
                cam_lock = _get_camera_lock(user_id, camera_id)
                captured_frames = None
                async with cam_lock:
                    grid_is_full = grid.add_frame(
                        image_bytes=img_bytes,
                        camera_id=camera_id,
                        user_id=user_id,
                        yolo_count=yolo_count,
                        yolo_classes=yolo_classes,
                        yolo_detections=yolo_detections,
                        mode=mode,
                        zone_assignment=zone_assignment or None,
                    )
                    if grid_is_full:
                        captured_frames = grid.get_and_reset()
                    current_count = grid.get_frame_count()

                if captured_frames:
                    grid_result = await orchestrator.process_grid(
                        user_id=user_id,
                        camera_id=camera_id,
                        mode="normal",
                        use_grid_image=True,
                        grid_size=GRID_SIZE,
                        frames=captured_frames
                    )
                    logger.info(f"Grid procesado: {grid_result.get('frame_count', 0)}/16 frames")
                else:
                    logger.debug(f"Grid {camera_id}: {current_count}/16, esperando más frames")

                await frame_bus.ack(msg_id)  # XACK solo tras procesar

        except Exception as e:
            logger.error(f"Error en yolo_worker: {e}", exc_info=True)
            # SIN ack: Redis retiene los mensajes y otro worker los rescata
            await asyncio.sleep(0.1)
            # C2: SIN ack → Redis retiene el mensaje y otro worker lo rescata
            # via XAUTOCLAIM (rescue_stale). En memory el frame se pierde igual
            # que antes (no hay peor caso nuevo).
            if msg_id is None:
                pass  # memory mode: no ack que hacer
            await asyncio.sleep(0.1)


# ═══════════════════════════════════════════════════════════════════════════
# Entry point — Solo se ejecuta cuando se corre directamente (no cuando se importa)
# ═══════════════════════════════════════════════════════════════════════════

# Variable global para evitar múltiples workers
_WORKER_STARTED = False

@app.on_event("startup")
async def _start_background_tasks():
    """Iniciar tareas en background: YOLO Worker(s) + Scheduler de Reportes."""
    global _WORKER_STARTED
    if not _WORKER_STARTED:
        _WORKER_STARTED = True
        # C2: conectar el bus de frames (Redis Streams si está configurado,
        # memoria como fallback explícito).
        await frame_bus.start()
        logger.info(f"🚌 FrameBus listo en modo '{frame_bus.mode}'")
        # C1: pool de WORKER_COUNT workers (default 4, env OJOIA_WORKER_COUNT).
        # Cada worker consume FRAME_QUEUE de forma independiente y procesa en
        # paralelo (subject al lock por camara - C2). Mas workers = mas cameras
        # concurrentes ( Semaphore(12) de Qwen es el proximo ceiling real).
        logger.info(f"🚀 Iniciando {WORKER_COUNT} YOLO Worker(s) en background...")
        for i in range(WORKER_COUNT):
            asyncio.create_task(yolo_worker())
        
        # Iniciar scheduler de reportes automáticos (7:30 AM diario)
        logger.info("⏰ Iniciando scheduler de reportes diarios (7:30 AM)...")
        from reportes.scheduler import start_scheduler
        asyncio.create_task(start_scheduler())


@app.on_event("shutdown")
async def _shutdown_tasks():
    """Cerrar recursos asíncronos compartidos al detener el servicio.
    P-shutdown: TODO lo que quede vivo retrasa el cierre — con cámaras
    haciendo keep-alive el proceso retenía el puerto infinitamente."""
    try:
        # C4: httpx.AsyncClient compartido del QwenOrchestrator
        from orchestrator import orchestrator
        await orchestrator.close()
        logger.info("🛑 AsyncClient compartido cerrado (C4)")
    except Exception as e:
        logger.warning(f"shutdown orchestrator.close() falló: {e}")
    try:
        # C2: cerrar cliente Redis del bus (conexiones pendientes del pool)
        await frame_bus.close()
        logger.info("🛑 FrameBus cerrado (C2)")
    except Exception as e:
        logger.warning(f"shutdown frame_bus falló: {e}")
    # detener loop de workers (el flag corta el while tras el ciclo actual)
    global WORKER_RUNNING
    WORKER_RUNNING = False
    logger.info("🛑 Workers señalados a parar (graceful 10s máx)")


@app.get("/api/business/is_open")
async def is_open(user_id: str, timestamp: float = None):
    """Determina si el negocio está abierto."""
    if not user_id:
        return {"success": False, "is_open": False}
    import json, os
    from datetime import datetime
    schedule_file = f"/home/sam/storage/users/{user_id}/business/schedule.json"
    if not os.path.exists(schedule_file):
        return {"success": True, "is_open": False, "reason": "schedule no encontrado"}
    try:
        with open(schedule_file) as f:
            sched = json.load(f)
        ts = timestamp or datetime.now().timestamp()
        now = datetime.fromtimestamp(ts)
        date_str = now.strftime("%Y-%m-%d")
        weekday = now.strftime("%a")
        current_hour = now.strftime("%H:%M")
        holidays_rd = ["2026-07-06", "2026-01-01"]
        if date_str in holidays_rd or date_str in sched.get("holidays", []):
            return {"success": True, "is_open": False, "reason": "festivo"}
        biz_hours = sched.get("schedule", {}).get(weekday, "08:00–18:00")
        biz_start, biz_end = biz_hours.split("–")
        biz_start = biz_start.strip()
        biz_end = biz_end.strip()
        if biz_start <= biz_end:
            is_open = biz_start <= current_hour < biz_end
        else:
            is_open = current_hour >= biz_start or current_hour < biz_end
        return {
            "success": True,
            "is_open": is_open,
            "confidence": "high",
            "weekday": weekday,
            "current_hour": current_hour,
            "business_hours": biz_hours
        }
    except Exception as e:
        return {"success": False, "error": str(e)}




# ═══════════════════════════════════════════════════════════
# REPORTES AUTOMÁTICOS - ENDPOINTS ADMIN
# ═══════════════════════════════════════════════════════════

@app.get("/api/reports/config")
async def get_report_config(user_id: str):
    """Obtiene configuración de reportes automáticos para un usuario."""
    try:
        from reportes.scheduler import get_user_report_config
        config = await get_user_report_config(user_id)
        return {
            "success": True,
            "config": config
        }
    except Exception as e:
        logger.error(f"Error obteniendo config de reportes: {e}")
        return {"success": False, "error": str(e)}


@app.post("/api/reports/config")
async def update_report_config(user_id: str, request: dict):
    """
    Actualiza configuración de reportes automáticos.
    
    Body:
        enabled: bool - Activar/desactivar reportes
        hour: int - Hora de envío (0-23)
        minute: int - Minuto de envío (0-59)
        cameras: list - Lista de camera_id (vacío = todas)
        format: str - "html" o "pdf"
        recipients: list - Emails adicionales
    """
    try:
        from reportes.scheduler import save_user_report_config
        
        config = {
            "enabled": request.get("enabled", True),
            "hour": request.get("hour", 7),
            "minute": request.get("minute", 30),
            "cameras": request.get("cameras", []),
            "format": request.get("format", "html"),
            "recipients": request.get("recipients", [])
        }
        
        success = await save_user_report_config(user_id, config)
        
        return {
            "success": success,
            "message": "Configuración guardada" if success else "Error guardando configuración",
            "config": config
        }
    except Exception as e:
        logger.error(f"Error actualizando config de reportes: {e}")
        return {"success": False, "error": str(e)}


@app.get("/api/reports/send")
async def send_report_manual(user_id: str, camera_id: str = None, date: str = "yesterday"):
    """
    Envía reporte manualmente (para testing).
    
    Params:
        camera_id: str - Cámara específica (opcional, usa todas si no se especifica)
        date: str - Fecha del reporte (today/yesterday/YYYY-MM-DD)
    """
    try:
        from reportes.daily_report import send_daily_report_to_chat
        
        # Si no hay camera_id, obtener primera cámara activa
        if not camera_id:
            from reportes.scheduler import _get_user_cameras
            cameras = await _get_user_cameras(user_id)
            camera_id = cameras[0] if cameras else None
        
        if not camera_id:
            return {"success": False, "error": "No hay cámaras disponibles"}
        
        result = await send_daily_report_to_chat(user_id, camera_id, date)
        
        return result
    except Exception as e:
        logger.error(f"Error enviando reporte manual: {e}")
        return {"success": False, "error": str(e)}


@app.get("/api/reports/list")
async def list_reports(user_id: str, limit: int = 10):
    """
    Lista reportes generados para un usuario.
    
    Params:
        limit: int - Cantidad máxima de reportes a retornar
    """
    try:
        reports_dir = user_root(user_id) / "daily_reports"
        
        if not reports_dir.exists():
            return {"success": True, "reports": [], "count": 0}
        
        # Listar archivos ordenados por fecha (más recientes primero)
        files = sorted(
            reports_dir.glob("*.html"),
            key=lambda f: f.stat().st_mtime,
            reverse=True
        )[:limit]
        
        reports = []
        for f in files:
            reports.append({
                "filename": f.name,
                "url": f"/storage/users/{user_id}/daily_reports/{f.name}",
                "size": f.stat().st_size,
                "created_at": datetime.fromtimestamp(f.stat().st_mtime).isoformat(),
                "type": "html"
            })
        
        return {
            "success": True,
            "reports": reports,
            "count": len(reports),
            "directory": str(reports_dir)
        }
    except Exception as e:
        logger.error(f"Error listando reportes: {e}")
        return {"success": False, "error": str(e)}


@app.get("/api/reports/stats")
async def get_report_stats(user_id: str):
    """
    Obtiene estadísticas de reportes para un usuario.
    """
    try:
        from reportes.scheduler import get_user_report_config
        
        config = await get_user_report_config(user_id)
        
        # Contar reportes generados
        reports_dir = user_root(user_id) / "daily_reports"
        total_files = len(list(reports_dir.glob("*.html"))) if reports_dir.exists() else 0
        
        return {
            "success": True,
            "stats": {
                "enabled": config.get("enabled", True),
                "schedule": f"{config.get('hour', 7):02d}:{config.get('minute', 30):02d}",
                "total_sent": config.get("total_sent", 0),
                "last_sent": config.get("last_sent"),
                "total_files": total_files,
                "cameras_configured": len(config.get("cameras", [])),
                "format": config.get("format", "html")
            }
        }
    except Exception as e:
        logger.error(f"Error obteniendo stats: {e}")
        return {"success": False, "error": str(e)}


# ═══════════════════════════════════════════════════════════
# NOTIFICACIONES - Endpoint para el frontend
# ═══════════════════════════════════════════════════════════

@app.get("/api/notifications/list")
async def get_notifications(user_id: str, limit: int = 10):
    """
    Obtiene notificaciones para un usuario (incluye reportes diarios).
    El frontend puede pollerar este endpoint cada 30 segundos.
    """
    try:
        notifications_dir = user_root(user_id) / "notifications"
        
        if not notifications_dir.exists():
            return {"success": True, "notifications": [], "count": 0}
        
        # Listar archivos ordenados por fecha (más recientes primero)
        files = sorted(
            notifications_dir.glob("*.json"),
            key=lambda f: f.stat().st_mtime,
            reverse=True
        )[:limit]
        
        notifications = []
        for f in files:
            try:
                data = json.loads(f.read_text())
                notifications.append({
                    "id": f.stem,
                    "type": data.get("type", "unknown"),
                    "title": "📊 Reporte Diario Disponible" if data.get("type") == "daily_report" else "Notificación",
                    "message": data.get("message", "")[:200],
                    "pdf_url": data.get("pdf_url"),
                    "action": data.get("action", "open_chat"),  # "open_chat" o "open_events"
                    "sent_at": data.get("sent_at"),
                    "read": data.get("read", False)
                })
            except:
                logger.debug("silent except")

        
        return {
            "success": True,
            "notifications": notifications,
            "count": len(notifications),
            "unread_count": sum(1 for n in notifications if not n.get("read"))
        }
    except Exception as e:
        logger.error(f"Error obteniendo notificaciones: {e}")
        return {"success": False, "error": str(e)}


@app.post("/api/notifications/mark-read")
async def mark_notification_read(user_id: str, notification_id: str):
    """Marca una notificación como leída."""
    try:
        notifications_dir = user_root(user_id) / "notifications"
        notification_file = notifications_dir / f"{notification_id}.json"
        
        if notification_file.exists():
            data = json.loads(notification_file.read_text())
            data["read"] = True
            notification_file.write_text(json.dumps(data, indent=2, ensure_ascii=False))
            return {"success": True, "message": "Notificación marcada como leída"}
        
        return {"success": False, "error": "Notificación no encontrada"}
    except Exception as e:
        logger.error(f"Error marcando notificación: {e}")
        return {"success": False, "error": str(e)}


# ═══════════════════════════════════════════════════════════
# WHATSAPP - Endpoint genérico (webhook para enviar reportes)
# ═══════════════════════════════════════════════════════════

@app.post("/api/reports/send-to-channel")
async def send_report_to_channel(user_id: str, request: dict):
    """
    Envía un reporte diario por múltiples canales: chat, push, WhatsApp.
    
    Body:
        channel: str - "chat" | "push" | "whatsapp" | "all"
        camera_id: str (opcional)
        whatsapp_number: str (requerido si channel=whatsapp) ej: +18091234567
    """
    try:
        from reportes.daily_report import send_full_daily_report_v2
        
        channel = request.get("channel", "all")
        camera_id = request.get("camera_id")
        whatsapp_number = request.get("whatsapp_number")
        date = request.get("date", "yesterday")
        
        if not user_id:
            return {"success": False, "error": "user_id required"}
        
        # Generar y enviar chat + push
        result = await send_full_daily_report_v2(user_id, camera_id, date)
        
        if not result.get("success"):
            return result
        
        # Si pidió WhatsApp, intentar enviar
        whatsapp_sent = False
        if channel in ("whatsapp", "all"):
            whatsapp_sent = await _send_daily_report_whatsapp(
                user_id=user_id,
                message=result.get("message", ""),
                pdf_url=result.get("pdf_url"),
                phone=whatsapp_number
            )
        
        # Actualizar historial con info de WhatsApp
        if result.get("chat_injected") or result.get("push_sent"):
            notifications_dir = user_root(user_id) / "notifications"
            notif_files = sorted(notifications_dir.glob("report_*.json"), 
                                 key=lambda f: f.stat().st_mtime, 
                                 reverse=True)
            if notif_files:
                data = json.loads(notif_files[0].read_text())
                data["channels_delivered"]["whatsapp"] = whatsapp_sent
                notif_files[0].write_text(json.dumps(data, indent=2, ensure_ascii=False))
        
        return {
            "success": True,
            "channels_delivered": {
                "chat": result.get("chat_injected"),
                "push_fcm": result.get("push_sent"),
                "whatsapp": whatsapp_sent
            },
            "message": result.get("message"),
            "pdf_url": result.get("pdf_url"),
            "report": result.get("report")
        }
    except Exception as e:
        logger.error(f"Error send_report_to_channel: {e}")
        return {"success": False, "error": str(e)}


async def _send_daily_report_whatsapp(user_id: str, message: str, pdf_url: str = None, phone: str = None):
    """
    Envía el reporte por WhatsApp usando la Meta WhatsApp Business API.
    
    Configuración requerida:
    - WHATSAPP_TOKEN en .env
    - WHATSAPP_PHONE_ID en .env  
    - phone: número del destinatario (sin '+', solo dígitos)
    """
    try:
        import os
        import requests as _req
        
        whatsapp_token = os.environ.get("WHATSAPP_TOKEN")
        whatsapp_phone_id = os.environ.get("WHATSAPP_PHONE_ID")
        
        if not whatsapp_token or not whatsapp_phone_id:
            logger.warning("WhatsApp no configurado (falta WHATSAPP_TOKEN o WHATSAPP_PHONE_ID)")
            return False
        
        # Si no se pasó phone, leerlo del user.json
        if not phone:
            user_file = user_root(user_id) / "user.json"
            if user_file.exists():
                with open(user_file) as f:
                    user_data = json.loads(f.read_text())
                phone = user_data.get("phone", "").replace("+", "").replace(" ", "").replace("-", "")
        
        if not phone:
            logger.warning(f"Sin número WhatsApp para {user_id}")
            return False
        
        # Construir mensaje (WhatsApp usa *bold* igual)
        wa_message = f"📊 *Reporte Diario*\n\n{message}"
        if pdf_url:
            full_url = f"https://ojoia.com.do{pdf_url}" if pdf_url.startswith("/") else pdf_url
            wa_message += f"\n\n🔗 Ver reporte: {full_url}"
        
        # Enviar vía Meta WhatsApp Business API
        api_url = f"https://graph.facebook.com/v18.0/{whatsapp_phone_id}/messages"
        headers = {
            "Authorization": f"Bearer {whatsapp_token}",
            "Content-Type": "application/json"
        }
        
        payload = {
            "messaging_product": "whatsapp",
            "to": phone,
            "type": "text",
            "text": {"body": wa_message[:4000]}  # WhatsApp limit
        }
        
        # B3: requests.post sincrono - offload al thread pool (10s timeout).
        resp = await asyncio.to_thread(
            lambda: _req.post(api_url, headers=headers, json=payload, timeout=10)
        )
        
        if resp.status_code in (200, 201):
            result = resp.json()
            logger.info(f"✅ WhatsApp enviado a {phone}: {result.get('messages', [{}])[0].get('id', '')}")
            return True
        else:
            logger.error(f"❌ WhatsApp error {resp.status_code}: {resp.text[:200]}")
            return False
            
    except Exception as e:
        logger.error(f"Error WhatsApp: {e}")
        return False


@app.post("/api/reports/inject-to-active-chat")
async def inject_report_to_active_chat(user_id: str, request: dict = None):
    """
    Inyecta el reporte diario directamente en la sesión ACTIVA del chat de Eva.
    Esto funciona porque usa el mismo diccionario _sessions que el chat en vivo.
    """
    try:
        from eva_v2 import _sessions
        from reportes.daily_report import generate_daily_report_pdf
        
        if not user_id:
            return {"success": False, "error": "user_id required"}
        
        # 1. Generar el reporte
        report = await generate_daily_report_pdf(user_id, None, "yesterday")
        if not report.get("success"):
            return {"success": False, "error": report.get("error")}
        
        # 2. Construir el mensaje
        business_name = report.get("business_name", "Tu negocio")
        summary = report.get("summary", {})
        
        message = f"""🍽️ *Reporte Diario - {business_name}*

📊 Análisis realizados: {summary.get('total_events', 0)}
👥 Personas únicas detectadas: {summary.get('persons_total', 0)}

📄 [Ver reporte completo](https://ojoia.com.do{report.get('pdf_url', '')})

_Este reporte se genera automáticamente todos los días a las 7:30 AM_"""
        
        # 3. Buscar la sesión ACTIVA de este usuario en _sessions
        active_session_id = None
        for sid, sdata in _sessions.items():
            if sdata.get("user_id") == user_id:
                active_session_id = sid
                break
        
        if not active_session_id:
            # Crear una nueva sesión si no existe
            active_session_id = f"chat_{user_id}_{int(time.time())}"
            _sessions[active_session_id] = {
                "user_id": user_id,
                "camera_id": "",
                "msgs": [],
                "messages": [],
                "last_activity": time.time()
            }
        
        # 4. Inyectar el mensaje en la sesión ACTIVA
        _sessions[active_session_id]["msgs"].append({
            "role": "assistant",
            "content": message,
            "timestamp": time.time(),
            "summary": True,
            "is_daily_report": True
        })
        
        _sessions[active_session_id]["messages"].append({
            "role": "assistant",
            "content": message,
            "timestamp": time.time()
        })
        
        logger.info(f"✅ Reporte inyectado en sesión activa {active_session_id} para {user_id}")
        
        # 5. También enviar push FCM
        from reportes.daily_report import send_daily_report_push_notification
        push_sent = await send_daily_report_push_notification(
            user_id=user_id,
            report_message=message,
            pdf_url=report.get("pdf_url")
        )
        
        return {
            "success": True,
            "session_id": active_session_id,
            "message": message,
            "pdf_url": report.get("pdf_url"),
            "push_sent": push_sent,
            "injected_to_active_chat": True
        }
        
    except Exception as e:
        logger.error(f"Error inyectando reporte: {e}")
        import traceback
        traceback.print_exc()
        return {"success": False, "error": str(e)}


@app.post("/api/reports/send-daily")
async def send_daily_report_production(user_id: str, request: dict = None):
    """
    Endpoint optimizado para producción:
    - Envía reporte completo (chat + push)
    - Mide tiempos de entrega
    - Botón de descarga directa
    - Push apunta al chat por 15 segundos
    
    Usage:
        POST /api/reports/send-daily?user_id=xxx
        Body: {"camera_id": "cam_001", "date": "yesterday"}
    """
    try:
        from reportes.daily_report_prod import send_daily_report_complete
        
        if not user_id:
            return {"success": False, "error": "user_id required"}
        
        camera_id = request.get("camera_id") if request else None
        date = request.get("date", "yesterday") if request else "yesterday"
        
        result = await send_daily_report_complete(user_id, camera_id, date)
        
        if result.get("success"):
            logger.info(
                f"✅ Reporte diario enviado a {user_id} | "
                f"Chat: {result.get('chat_injected')} | "
                f"Push: {result.get('push_sent')} | "
                f"Tiempo push: {result.get('push_delivery_time_ms', 0)}ms | "
                f"Total: {result.get('timing', {}).get('total_ms', 0)}ms"
            )
        
        return result
        
    except Exception as e:
        logger.error(f"Error send_daily_report_production: {e}")
        return {"success": False, "error": str(e)}


# ═══════════════════════════════════════════════════════════
# REPORTES - Páginas HTML y PDF
# ═══════════════════════════════════════════════════════════

@app.get("/api/reportes/view/{user_id}/{date}")
async def get_report_page(user_id: str, date: str):
    """
    Sirve la página HTML del reporte.
    URL: /api/reportes/view/{user_id}/2026-07-09
    """
    try:
        _validate_safe_path(user_id, "user_id")  # A5
        _validate_safe_path(date, "date")
        from reportes.page_generator import generate_report_page
        
        # Generar página
        result = await generate_report_page(user_id, date)
        
        if not result.get("success"):
            return {"error": result.get("error")}
        
        # Servir HTML
        from fastapi.responses import HTMLResponse
        html_file = Path(result.get("html_path"))
        if html_file.exists():
            return HTMLResponse(content=html_file.read_text(encoding='utf-8'))
        
        return {"error": "Página no generada"}
        
    except Exception as e:
        logger.error(f"Error sirviendo página: {e}")
        return {"error": str(e)}


@app.get("/api/reportes/download/{user_id}/{date}.pdf")
async def download_report_pdf(user_id: str, date: str):
    """
    Descarga el PDF del reporte.
    URL: /api/reportes/download/{user_id}/2026-07-09.pdf
    """
    try:
        # A5: validar que user_id y date no contengan path components maliciosos.
        _validate_safe_path(user_id, "user_id")
        _validate_safe_path(date, "date")
        from reportes.page_generator import generate_report_page

        # Generar página (esto también genera PDF)
        result = await generate_report_page(user_id, date)

        if not result.get("success"):
            return {"error": result.get("error")}

        # Servir PDF
        from fastapi.responses import FileResponse
        # A5: el pdf_path viene de generate_report_page (interno), pero validamos
        # que este dentro de STORAGE_ROOT/report_pages para evititar path traversal
        # si algun bug interno devolviera una ruta arbitraria.
        pdf_file = Path(result.get("pdf_path") or "")
        try:
            if not pdf_file.is_file():
                return {"error": "PDF no generado"}
            report_base = (STORAGE_ROOT / "report_pages").resolve()
            if not pdf_file.resolve().is_relative_to(report_base):
                logger.warning(f"[traversal] pdf_path fuera de base: {pdf_file}")
                return {"error": "PDF invalido"}
        except Exception:
            return {"error": "PDF invalido"}
        return FileResponse(
            str(pdf_file),
            media_type='application/pdf',
            filename=f"reporte_{date}.pdf"
        )

    except Exception as e:
        logger.error(f"Error sirviendo PDF: {e}")
        return {"error": str(e)}


@app.get("/api/reportes/url/{user_id}/{date}")
async def get_report_urls(user_id: str, date: str):
    """
    Devuelve URLs públicas del reporte (HTML y PDF).
    Útil para compartir en chat, push, etc.
    """
    try:
        _validate_safe_path(user_id, "user_id")  # A5: no path components en user_id
        base_url = "https://api.ojoia.com.do"
        date_str = _resolve_report_date(date)
        html_url = f"{base_url}/reportes/{user_id}/reporte_{date_str}.html"
        pdf_url = f"{base_url}/reportes/{user_id}/reporte_{date_str}.pdf"
        
        return {
            "success": True,
            "html_url": html_url,
            "pdf_url": pdf_url,
            "share_url": html_url
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


def _resolve_report_date(date: str) -> str:
    """Resuelve 'today'/'yesterday' a fecha ISO."""
    from datetime import datetime, timedelta
    today = datetime.now().date()
    if date == "today":
        return today.strftime("%Y-%m-%d")
    if date == "yesterday":
        return (today - timedelta(days=1)).strftime("%Y-%m-%d")
    return date or today.strftime("%Y-%m-%d")


# (El endpoint POST /api/reportes/send-v2 se define arriba, línea ~313)


# ═══════════════════════════════════════════════════════════════════════════
# ADMIN ENDPOINTS — auth, users, billing, plans, cameras, events, storage, support
# ═══════════════════════════════════════════════════════════════════════════

ADMIN_CONFIG_FILE = STORAGE_ROOT / "admin_config.json"
ADMIN_SESSION_TTL = 8 * 3600  # 8 horas


def _load_admin_config() -> dict:
    """Cargar admin_config.json; crear con token aleatorio si no existe."""
    if ADMIN_CONFIG_FILE.exists():
        try:
            with open(ADMIN_CONFIG_FILE) as f:
                return json.load(f)
        except Exception:
            logger.debug("silent: {exc}", exc=Exception)

    cfg = {
        "admin_token": "oj_admin_" + secrets.token_urlsafe(32),
        "admin_email": "admin@ojoia.com.do",
        "created_at": int(time.time()),
        "sessions": {}
    }
    _save_admin_config(cfg)
    return cfg


def _save_admin_config(cfg: dict):
    try:
        tmp = ADMIN_CONFIG_FILE.with_suffix(".tmp")
        with open(tmp, "w") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
        os.replace(str(tmp), str(ADMIN_CONFIG_FILE))
    except Exception:
        with open(ADMIN_CONFIG_FILE, "w") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)


def _admin_session_redis() -> Optional[object]:
    """R5: cliente Redis síncrono para sesiones admin (lazy, opcional)."""
    global _ADMIN_REDIS
    if _ADMIN_REDIS is False:
        return None
    try:
        import redis as _redis_mod
        if _ADMIN_REDIS is None:
            url = os.environ.get("REDIS_URL")
            if not url:
                _ADMIN_REDIS = False
                return None
            _ADMIN_REDIS = _redis_mod.from_url(url, socket_timeout=2)
        _ADMIN_REDIS.ping()
        return _ADMIN_REDIS
    except Exception:
        return None  # Redis caído → fallback JSON (no romper el login)


_ADMIN_REDIS = None  # None=ni probado, cliente=conectado, False=no disponible


def _verify_admin(authorization: str = Header(None)) -> dict:
    """Valida sesión admin. R5: Redis-first (sobrevive reinicios de API) con
    fallback a admin_config.json (compat con sesiones viejas)."""
    if not authorization:
        raise HTTPException(status_code=401, detail="Authorization requerido")
    token = authorization.replace("Bearer ", "").strip()
    if not token:
        raise HTTPException(status_code=401, detail="Token invalido")
    cfg = _load_admin_config()
    # R5: Redis primero
    r = _admin_session_redis()
    if r is not None:
        try:
            raw = r.get(f"admin_sess:{token}")
            if raw:
                import json as _json
                s = _json.loads(raw)
                if int(time.time()) > s.get("expires_at", 0):
                    r.delete(f"admin_sess:{token}")
                    raise HTTPException(status_code=401, detail="Sesion expirada")
                return {"session_token": token, "cfg": cfg, "store": "redis"}
        except HTTPException:
            raise
        except Exception:
            pass  # Redis falló → seguir con JSON
    # fallback JSON (legado)
    sessions = cfg.get("sessions", {})
    # A6: comparar token en tiempo constante (anti timing-attack).
    s = None
    for sess_tok, sess_data in sessions.items():
        if hmac.compare_digest(str(sess_tok), token):
            s = sess_data
            break
    if s is None:
        # migrar: si existe en Redis pero no en JSON ya se manejó arriba
        raise HTTPException(status_code=401, detail="Sesion no encontrada")
    if int(time.time()) > s.get("expires_at", 0):
        sessions.pop(token, None)
        _save_admin_config(cfg)
        raise HTTPException(status_code=401, detail="Sesion expirada")
    return {"session_token": token, "cfg": cfg, "store": "json"}


@app.post("/admin/auth/login")
async def admin_auth_login(request: dict):
    """Login admin: valida admin_token, crea sesion."""
    token = (request.get("token") or "").strip() if isinstance(request, dict) else ""
    if not token:
        raise HTTPException(status_code=400, detail="token requerido")
    cfg = _load_admin_config()
    # A6: comparacion en tiempo constante (anti timing-attack). Antes: token != cfg.get("admin_token")
    if not hmac.compare_digest(token, str(cfg.get("admin_token") or "")):
        raise HTTPException(status_code=401, detail="Credencial invalida")
    session_token = secrets.token_urlsafe(32)
    sess = {
        "created_at": int(time.time()),
        "expires_at": int(time.time()) + ADMIN_SESSION_TTL,
        "user_agent": request.get("user_agent", "admin")
    }
    # R5: sesión en Redis con TTL (sobrevive reinicios de la API; expira sola)
    r = _admin_session_redis()
    stored = "json"
    if r is not None:
        try:
            r.setex(f"admin_sess:{session_token}", ADMIN_SESSION_TTL,
                    json.dumps(sess))
            stored = "redis"
        except Exception:
            pass
    # legacy JSON: solo como respaldo si Redis no está (no crecer aquí)
    if r is None:
        cfg.setdefault("sessions", {})[session_token] = sess
        _save_admin_config(cfg)
    logger.info(f"Admin login OK (store: {stored})")
    return {"success": True, "session_token": session_token}


@app.post("/admin/auth/logout")
async def admin_auth_logout(authorization: str = Header(None)):
    cfg = _load_admin_config()
    token = (authorization or "").replace("Bearer ", "").strip()
    if token:
        r = _admin_session_redis()
        if r is not None:
            try:
                r.delete(f"admin_sess:{token}")
            except Exception:
                pass
        cfg.get("sessions", {}).pop(token, None)
        _save_admin_config(cfg)
    return {"success": True}


@app.get("/admin/auth/me")
async def admin_auth_me(authorization: str = Header(None)):
    _ = _verify_admin(authorization)
    return {"success": True, "admin_email": _.get("cfg", {}).get("admin_email", "admin@ojoia.com.do")}


# ── Admin: Support Contact (editable) ─────────────────────────────────────

@app.get("/admin/support-contact")
async def admin_get_support_contact(authorization: str = Header(None)):
    _verify_admin(authorization)
    cfg = get_disk_config()
    return cfg.get("support_contact", {})


@app.put("/admin/support-contact")
async def admin_set_support_contact(request: dict, authorization: str = Header(None)):
    _verify_admin(authorization)
    cfg = get_disk_config()
    sc = cfg.get("support_contact", {})
    for k in ("whatsapp", "email", "phone", "bank_info"):
        if k in request:
            sc[k] = request[k]
    cfg["support_contact"] = sc
    save_disk_config(cfg)
    logger.info("Admin: support-contact actualizado")
    return {"success": True, "support_contact": sc}


# ── Admin: Plan CRUD ───────────────────────────────────────────────────

@app.get("/admin/plans")
async def admin_list_plans(authorization: str = Header(None)):
    _verify_admin(authorization)
    cfg = get_disk_config()
    return {"plans": cfg.get("plans", {})}


@app.post("/admin/plans")
async def admin_create_plan(request: dict, authorization: str = Header(None)):
    _verify_admin(authorization)
    plan_id = request.get("plan_id", "").strip().lower()
    if not plan_id:
        raise HTTPException(status_code=400, detail="plan_id is required")
    cfg = get_disk_config()
    plans = cfg.get("plans", {})
    if plan_id in plans:
        raise HTTPException(status_code=409, detail=f"Plan '{plan_id}' already exists")
    plan_def = {
        "name": request.get("name", plan_id),
        "price_monthly": float(request.get("price_monthly", 0)),
        "price_yearly": float(request.get("price_yearly", 0)),
        "currency": request.get("currency", "USD"),
        "duration_days": int(request.get("duration_days", 30)),
        "trial_days": int(request.get("trial_days", 0)),
        "max_storage_gb": int(request.get("max_storage_gb", 10)),
        "max_cameras": int(request.get("max_cameras", 1)),
        "max_rules_per_camera": int(request.get("max_rules_per_camera", 2)),
        "features": request.get("features", {
            "ai_analysis": True, "alerts_push": True, "grid_detection": False,
            "multi_zone": False, "priority_disk": "", "support": "community"
        }),
        "public": bool(request.get("public", True))
    }
    plans[plan_id] = plan_def
    cfg["plans"] = plans
    save_disk_config(cfg)
    logger.info(f"Plan created: {plan_id}")
    return {"success": True, "plan_id": plan_id, "plan": plan_def}


@app.put("/admin/plans/{plan_id}")
async def admin_update_plan(plan_id: str, request: dict, authorization: str = Header(None)):
    _verify_admin(authorization)
    cfg = get_disk_config()
    plans = cfg.get("plans", {})
    if plan_id not in plans:
        raise HTTPException(status_code=404, detail=f"Plan '{plan_id}' not found")
    existing = plans[plan_id]
    updatable = ["name", "price_monthly", "price_yearly", "currency", "duration_days",
                 "trial_days", "max_storage_gb", "max_cameras", "max_rules_per_camera",
                 "features", "public"]
    for field in updatable:
        if field in request:
            if field == "features":
                existing["features"].update(request[field])
            else:
                existing[field] = request[field]
    plans[plan_id] = existing
    cfg["plans"] = plans
    save_disk_config(cfg)
    logger.info(f"Plan updated: {plan_id}")
    return {"success": True, "plan": existing}


@app.delete("/admin/plans/{plan_id}")
async def admin_delete_plan(plan_id: str, authorization: str = Header(None)):
    _verify_admin(authorization)
    cfg = get_disk_config()
    plans = cfg.get("plans", {})
    if plan_id not in plans:
        raise HTTPException(status_code=404, detail=f"Plan '{plan_id}' not found")
    if plan_id == "free":
        raise HTTPException(status_code=400, detail="Cannot delete the 'free' plan")
    del plans[plan_id]
    cfg["plans"] = plans
    save_disk_config(cfg)
    migrated = 0
    for disk in cfg.get("disks", []):
        users_base = Path(disk.get("mount", "")) / disk.get("user_folder", "users")
        if not users_base.is_dir():
            continue
        for uid in users_base.iterdir():
            uf = uid / "user.json"
            if uf.is_file():
                try:
                    with open(uf) as f:
                        ud = json.load(f)
                    if ud.get("plan") == plan_id:
                        ud["plan"] = "free"
                        with open(uf, "w") as f:
                            json.dump(ud, f, indent=2, ensure_ascii=False)
                        migrated += 1
                except Exception as e_5414:
                    # P0 (Sección #8): loggeo en lugar de pass silencioso
                    logger.warning(f"[plan-delete] {uf.name} migrate failed: {e_5414}")
    logger.info(f"Plan deleted: {plan_id}, migrated {migrated} users to free")
    return {"success": True, "migrated_users": migrated}


# ── Admin: Billing Management ──────────────────────────────────────────

@app.post("/admin/billing/run-expirations")
async def admin_run_expirations(authorization: str = Header(None, alias="Authorization")):
    """F-billing: ejecutar el job de vencimientos AHORA (avisos push al
    usuario a 3d/1d, aviso de gracia, suspensión real al acabar gracia,
    y resumen al admin). También corre diario por cron."""
    _verify_admin(authorization)
    from expirations_job import process_all
    result = await asyncio.to_thread(process_all)
    return {"success": True, "result": result}


@app.get("/admin/billing")
async def admin_billing_overview(authorization: str = Header(None)):
    _verify_admin(authorization)
    cfg = get_disk_config()
    now = time.time()
    users = []
    total_revenue = 0
    active_count = 0
    expired_count = 0
    warning_count = 0
    trial_count = 0
    for disk in cfg.get("disks", []):
        users_base = Path(disk.get("mount", "")) / disk.get("user_folder", "users")
        if not users_base.is_dir():
            continue
        for uid in users_base.iterdir():
            uf = uid / "user.json"
            if not uf.is_file():
                continue
            try:
                with open(uf) as f:
                    ud = json.load(f)
            except Exception:
                continue
            check = _plan_check(ud)
            access_status = _compute_access_status(ud)
            plan = ud.get("plan", "free")
            plan_def = cfg.get("plans", {}).get(plan, {})
            confirmed_payments = [p for p in ud.get("payments", []) if p.get("status") == "confirmed"]
            total_paid = sum(p.get("amount", 0) for p in confirmed_payments)
            total_revenue += total_paid
            if access_status == "active":
                active_count += 1
            elif access_status == "warning":
                warning_count += 1
            elif access_status == "expired":
                expired_count += 1
            if check["status"] == "trial":
                trial_count += 1
            users.append({
                "user_id": uid.name, "name": ud.get("name", ""), "email": ud.get("email", ""),
                "business_name": ud.get("business_name", ""), "plan": plan,
                "plan_name": plan_def.get("name", plan), "status": ud.get("status", "active"),
                "access_status": access_status, "plan_end": ud.get("plan_end", 0),
                "trial_end": ud.get("trial_end"), "days_left": check["days_left"],
                "grace_days_left": check.get("grace_days_left"),
                "trial_days_left": check.get("trial_days_left"),
                "camera_count": len(ud.get("cameras", [])),
                "payment_count": len(confirmed_payments),
                "total_paid": total_paid, "last_payment": ud.get("last_payment"),
                "next_due": ud.get("next_due", 0),
                "pending_payments": len([p for p in ud.get("payments", []) if p.get("status") == "pending"]),
                "created_at": ud.get("created_at", 0)
            })
    return {
        "users": users,
        "summary": {
            "total_users": len(users), "active": active_count, "warning": warning_count,
            "expired": expired_count, "trial": trial_count, "total_revenue": round(total_revenue, 2)
        }
    }


@app.post("/admin/users")
async def admin_create_user(request: dict, authorization: str = Header(None)):
    _verify_admin(authorization)
    user_id = request.get("user_id") or ("u_" + secrets.token_hex(6))
    user_dir = user_root(user_id)
    user_dir.mkdir(parents=True, exist_ok=True)
    now_ts = int(time.time())
    ud = {
        "user_id": user_id, "name": request.get("name", ""), "email": request.get("email", ""),
        "phone": request.get("phone", ""), "business_name": request.get("business_name", ""),
        "business_type": request.get("business_type", "retail"),
        "plan": request.get("plan", "free"), "status": request.get("status", "active"),
        "created_at": now_ts, "plan_start": now_ts,
        "plan_end": int(request.get("plan_end", 0)) or (now_ts + 30 * 86400),
        "trial_end": int(request.get("trial_end", now_ts + 14 * 86400)),
        "billing_cycle": "monthly", "grace_period_days": 3, "next_due": 0,
        "payments": [], "last_payment": None,
        "access_token": request.get("access_token") or ("oj_live_" + secrets.token_urlsafe(32)),
        "schedule": {"open": "08:00", "close": "22:00", "enabled": False},
        "cameras": [], "fcm_tokens": [], "fcm_devices": [],
        "storage_path": str(user_dir), "disk_mount": str(STORAGE_ROOT),
        "vigilance_rules": [], "vigilance_prompt": "", "rules_es": [],
        "eva_sessions": []
    }
    with _get_user_lock(user_id):  # C1
        _atomic_write_user_json(user_dir / "user.json", ud)
    logger.info(f"Admin: user created {user_id}")
    return {"success": True, "user_id": user_id}


@app.get("/admin/users")
async def admin_users(authorization: str = Header(None)):
    _verify_admin(authorization)
    users = []
    cfg = get_disk_config()
    for disk in cfg.get("disks", []):
        users_base = Path(disk.get("mount", "")) / disk.get("user_folder", "users")
        if not users_base.is_dir():
            continue
        for uid in users_base.iterdir():
            user_file = uid / "user.json"
            if not user_file.is_file():
                continue
            try:
                with open(user_file) as f:
                    ud = json.load(f)
                check = _plan_check(ud)
                access_status = _compute_access_status(ud)
                plan_def = cfg.get("plans", {}).get(ud.get("plan", "free"), {})
                sp = Path(ud.get("storage_path", str(user_file.parent)))
                # B3: _dir_used_mb hace subprocess.run(du) - bloquea el event loop.
                # Mover a thread para no serializar el endpoint admin_users cuando
                # hay muchos usuarios (cada du ~5-50ms, 100 users = 500ms-5s).
                used_mb = await asyncio.to_thread(_dir_used_mb, sp) if sp.exists() else 0
                ud["_plan_status"] = check["status"]
                ud["_access_status"] = access_status
                ud["_days_left"] = check["days_left"]
                ud["_grace_days_left"] = check.get("grace_days_left")
                ud["_trial_days_left"] = check.get("trial_days_left")
                ud["_storage_used_mb"] = used_mb
                ud["_max_storage_gb"] = plan_def.get("max_storage_gb", 10)
                ud["_max_cameras"] = plan_def.get("max_cameras", 1)
                ud["_payment_count"] = len([p for p in ud.get("payments", []) if p.get("status") == "confirmed"])
                ud["_pending_payments"] = len([p for p in ud.get("payments", []) if p.get("status") == "pending"])
                users.append(ud)
            except Exception:
                logger.debug("silent: {exc}", exc=Exception)

    return {"users": users}


@app.get("/admin/users/{user_id}")
async def admin_get_user(user_id: str, authorization: str = Header(None)):
    _verify_admin(authorization)
    user_file = find_user_json(user_id)
    if user_file and user_file.exists():
        with open(user_file) as f:
            user_data = json.load(f)
        check = _plan_check(user_data)
        return {
            "user_id": user_id, "name": user_data.get("name", "-"), "email": user_data.get("email", "-"),
            "phone": user_data.get("phone", ""), "business_name": user_data.get("business_name", ""),
            "business_type": user_data.get("business_type", ""), "plan": user_data.get("plan", "free"),
            "plan_status": check["status"], "access_status": _compute_access_status(user_data),
            "plan_end": user_data.get("plan_end", 0), "trial_end": user_data.get("trial_end"),
            "days_left": check["days_left"], "status": user_data.get("status", "active"),
            "grace_days_left": check.get("grace_days_left"), "trial_days_left": check.get("trial_days_left"),
            "access_token": user_data.get("access_token", ""),
            "camera_count": len(user_data.get("cameras", [])),
            "max_cameras": _get_plan_field(user_data.get("plan", "free"), "max_cameras", 1),
            "payments": user_data.get("payments", []), "last_payment": user_data.get("last_payment"),
            "created_at": user_data.get("created_at", "-"), "cameras": user_data.get("cameras", [])
        }
    raise HTTPException(status_code=404, detail="Usuario no encontrado")


@app.put("/admin/users/{user_id}")
async def admin_update_user(user_id: str, request: dict, authorization: str = Header(None)):
    _verify_admin(authorization)
    user_file = find_user_json(user_id)
    if not user_file or not user_file.exists():
        raise HTTPException(status_code=404, detail="Usuario no encontrado")
    def _mut_admin(ud):
        for k in ("name", "email", "phone", "business_name", "business_type", "plan", "status",
                  "plan_end", "trial_end", "next_due", "access_token"):
            if k in request:
                ud[k] = request[k]
    update_user_json(user_id, _mut_admin)  # C1
    logger.info(f"Admin: user updated {user_id}")
    return {"success": True}


@app.delete("/admin/users/{user_id}")
async def admin_delete_user(user_id: str, authorization: str = Header(None)):
    _verify_admin(authorization)
    user_file = find_user_json(user_id)
    if not user_file or not user_file.exists():
        raise HTTPException(status_code=404, detail="Usuario no encontrado")
    user_dir = user_file.parent
    try:
        import shutil
        shutil.rmtree(str(user_dir))
        logger.info(f"Admin: user deleted {user_id}")
        return {"success": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/admin/users/{user_id}/renew")
async def admin_renew_user(user_id: str, request: dict, authorization: str = Header(None)):
    _verify_admin(authorization)
    user_file = find_user_json(user_id)
    if not user_file or not user_file.exists():
        raise HTTPException(status_code=404, detail="User not found")
    with open(user_file) as f:
        ud = json.load(f)
    plan = request.get("plan", ud.get("plan", "free"))
    duration_days = int(request.get("duration_days", 0))
    amount = float(request.get("amount", 0))
    method = request.get("method", "manual")
    notes = request.get("notes", "")
    cfg = get_disk_config()
    plan_def = cfg.get("plans", {}).get(plan, {})
    # P0-4 fix: duration_days=0 significa CAMBIAR plan SIN extender (el
    # panel changePlan lo usa así). Antes se rellenaba con el duration del
    # plan destino → cambiar de plan regalaba 30/365 días sin cobro.
    extend_days = duration_days
    if not extend_days and not request.get("extend", False):
        extend_days = 0
    elif not extend_days:
        extend_days = plan_def.get("duration_days", 30)
    now_ts = int(time.time())
    current_end = ud.get("plan_end", 0) or 0
    if extend_days > 0:
        new_end = (now_ts if current_end < now_ts else current_end) + (extend_days * 86400)
    else:
        # solo cambio de plan: el fin se mantiene; si nunca hubo, arranca HOY
        # con el duration del plan SOLO si no tenía uno previo válido
        new_end = current_end if current_end > now_ts else now_ts
    payment_id = f"pay_admin_{now_ts}_{secrets.token_hex(4)}"
    payment = {
        "id": payment_id, "user_id": user_id, "amount": amount,
        "currency": plan_def.get("currency", "USD"), "method": method,
        "reference": f"admin_renewal_{now_ts}",
        "notes": notes or f"Renovacion admin: {duration_days} dias",
        "status": "confirmed", "created_at": now_ts, "confirmed_at": now_ts, "confirmed_by": "admin",
        "duration_days": duration_days
    }
    def _mut_renew(ud):
        ud["plan"] = plan
        ud["plan_end"] = new_end
        ud["next_due"] = new_end
        ud["status"] = "active"
        ud["trial_end"] = None
        payments = ud.get("payments", [])
        payments.append(payment)
        ud["payments"] = payments
        ud["last_payment"] = payment
    update_user_json(user_id, _mut_renew)  # C1
    logger.info(f"User renewed: {user_id} plan={plan} end={new_end} amount={amount}")
    _invalidate_ud_ingest_cache(user_id)  # P0-billing
    return {"success": True, "plan": plan, "plan_end": new_end, "payment_id": payment_id}


@app.post("/admin/users/{user_id}/payments")
async def admin_create_payment(user_id: str, request: dict,
                               authorization: str = Header(None, alias="Authorization")):
    """P1-billing: registrar un pago PENDIENTE de cobrar (transferencia
    reportada por el cliente, cheque, etc.). El panel lo muestra en el
    filtro 'Pago pendiente' y se confirma con el botón existente.
    Body: {amount, method, notes}"""
    _verify_admin(authorization)
    user_file = find_user_json(user_id)
    if not user_file or not user_file.exists():
        raise HTTPException(status_code=404, detail="User not found")
    amount = float(request.get("amount") or 0)
    if amount <= 0:
        raise HTTPException(status_code=400, detail="amount debe ser > 0")
    method = (request.get("method") or "transfer").strip()[:30]
    notes = (request.get("notes") or "").strip()[:200]
    now_ts = int(time.time())
    payment_id = f"pay_{now_ts}_{secrets.token_hex(4)}"
    payment = {"id": payment_id, "amount": amount, "method": method,
               "notes": notes, "status": "pending",
               "created_at": now_ts, "reported_by": "admin"}

    def _mut(ud):
        ud.setdefault("payments", []).append(payment)
    update_user_json(user_id, _mut)
    logger.info(f"[billing] pago pending creado: {user_id[:8]}… {payment_id} "
                f"(${amount} {method})")
    return {"success": True, "payment_id": payment_id, "payment": payment}


@app.get("/admin/users/{user_id}/payments")
async def admin_list_payments(user_id: str, authorization: str = Header(None, alias="Authorization")):
    """P1-billing: historial de pagos del usuario (para la tabla del panel
    — reemplaza el dump JSON crudo del detalle)."""
    _verify_admin(authorization)
    user_file = find_user_json(user_id)
    if not user_file or not user_file.exists():
        raise HTTPException(status_code=404, detail="User not found")
    with open(user_file) as f:
        ud = json.load(f)
    payments = ud.get("payments", [])
    payments.sort(key=lambda p: p.get("created_at") or p.get("confirmed_at") or 0, reverse=True)
    confirmed = [p for p in payments if p.get("status") == "confirmed"]
    return {
        "payments": payments,
        "summary": {
            "total_confirmed": round(sum(p.get("amount", 0) for p in confirmed), 2),
            "pending_count": len([p for p in payments if p.get("status") == "pending"]),
            "last_payment": payments[0] if payments else None,
        }
    }


@app.post("/admin/users/{user_id}/payment/{payment_id}/confirm")
async def admin_confirm_payment(user_id: str, payment_id: str, request: dict, authorization: str = Header(None)):
    _verify_admin(authorization)
    user_file = find_user_json(user_id)
    if not user_file or not user_file.exists():
        raise HTTPException(status_code=404, detail="User not found")
    result_holder = {}

    def _mut_confirm(ud):
        payments = ud.get("payments", [])
        found = False
        for p in payments:
            if p.get("id") == payment_id:
                p["status"] = "confirmed"
                p["confirmed_at"] = int(time.time())
                p["confirmed_by"] = "admin"
                p["notes"] = request.get("notes", p.get("notes", ""))
                found = True
                break
        if not found:
            return  # se valida abajo con result_holder
        result_holder["found"] = True
        # Al confirmar pago: reactivar servicio + extender plan_end si trae duration_days
        ud["status"] = "active"
        dur = 0
        for p in payments:
            if p.get("id") == payment_id:
                dur = int(p.get("duration_days", 0))
                break
        if dur > 0:
            now_ts = int(time.time())
            current_end = ud.get("plan_end", 0) or 0
            base = now_ts if current_end < now_ts else current_end
            ud["plan_end"] = base + (dur * 86400)
            ud["next_due"] = ud["plan_end"]
        ud["payments"] = payments
        ud["last_payment"] = next((p for p in payments if p["id"] == payment_id), None)

    ud = update_user_json(user_id, _mut_confirm)  # C1: lock + atomic
    if not result_holder.get("found"):
        raise HTTPException(status_code=404, detail="Payment not found")
    logger.info(f"Payment confirmed: {user_id} {payment_id} -> servicio activo")
    return {"success": True, "payment_id": payment_id, "status": "confirmed",
            "service": "active", "plan_end": ud.get("plan_end")}


@app.post("/admin/users/{user_id}/suspend")
async def admin_suspend_user(user_id: str, request: dict, authorization: str = Header(None)):
    _verify_admin(authorization)
    user_file = find_user_json(user_id)
    if not user_file or not user_file.exists():
        raise HTTPException(status_code=404, detail="User not found")
    update_user_json(user_id, lambda ud: ud.__setitem__("status", "suspended"))  # C1
    _invalidate_ud_ingest_cache(user_id)  # P0-billing
    logger.info(f"User suspended: {user_id}")
    return {"success": True, "status": "suspended"}


@app.post("/admin/users/{user_id}/reactivate")
async def admin_reactivate_user(user_id: str, request: dict, authorization: str = Header(None)):
    """P0-3 fix: reactivar ya no es un no-op engañoso. Si el plan_end ya
    venció, reactivar sin renovar solo devolvería a 'suspended' al momento
    (o dejaría el servicio cortado por el enforcement). Se exige renovar
    primero, salvo gracia vigente."""
    _verify_admin(authorization)
    user_file = find_user_json(user_id)
    if not user_file or not user_file.exists():
        raise HTTPException(status_code=404, detail="User not found")
    with open(user_file) as f:
        ud = json.loads(f.read())
    chk = _plan_check(ud)
    if chk["status"] == "expired":
        raise HTTPException(
            status_code=409,
            detail="El plan ya venció (gracia agotada). Renueva primero con "
                   "/renew — reactivar no extiende el plan.")
    update_user_json(user_id, lambda u: u.__setitem__("status", "active"))  # C1
    _invalidate_ud_ingest_cache(user_id)  # P0-billing
    logger.info(f"User reactivated: {user_id}")
    return {"success": True, "status": "active"}


@app.post("/admin/users/{user_id}/regen-token")
async def admin_regen_token(user_id: str, authorization: str = Header(None)):
    _verify_admin(authorization)
    user_file = find_user_json(user_id)
    if not user_file or not user_file.exists():
        raise HTTPException(status_code=404, detail="User not found")
    new_token = "oj_live_" + secrets.token_urlsafe(32)
    update_user_json(user_id, lambda ud: ud.__setitem__("access_token", new_token))  # C1
    return {"success": True, "access_token": new_token}


# ── Admin: Cameras ─────────────────────────────────────────────────────

@app.get("/admin/cameras")
async def admin_list_cameras(authorization: str = Header(None)):
    _verify_admin(authorization)
    cfg = get_disk_config()
    now = time.time()
    cameras = []
    for disk in cfg.get("disks", []):
        users_base = Path(disk.get("mount", "")) / disk.get("user_folder", "users")
        if not users_base.is_dir():
            continue
        for uid in users_base.iterdir():
            user_file = uid / "user.json"
            if not user_file.is_file():
                continue
            try:
                with open(user_file) as fh:
                    udata = json.load(fh)
            except Exception:
                continue
            for cam in udata.get("cameras", []):
                cid = cam.get("camera_id", "") or ""
                # F-panel: camera.json es la fuente de verdad de telemetría
                # (last_frame/last_announce/fw/local_ip se actualizan ahí por
                # ingest/announce). El user.json NO lleva last_frame desde el
                # throttle F3 → leía 0 → "Offline" con la cámara transmitiendo.
                cam_cfg = {}
                cj = uid / "cameras" / cid / "camera.json"
                if cj.exists():
                    try:
                        cam_cfg = json.loads(cj.read_text())
                    except Exception:
                        cam_cfg = {}
                last_announce = cam_cfg.get("last_announce") or cam.get("last_announce") or 0
                last_frame = cam.get("last_frame") or cam_cfg.get("last_frame") or 0
                announce_age = now - last_announce if last_announce else None
                # F-panel: señal de vida REAL = mtime de latest_raw.jpg (se
                # reescribe en CADA frame del ingest). El last_frame de
                # user.json está throttled y el de camera.json no se escribe.
                raw_jpg = uid / "cameras" / cid / "frames" / "latest_raw.jpg"
                try:
                    frame_age = now - raw_jpg.stat().st_mtime
                    last_frame = raw_jpg.stat().st_mtime
                except OSError:
                    frame_age = now - last_frame if last_frame else None
                is_online = (announce_age is not None and announce_age < 180) or (frame_age is not None and frame_age < 180)
                last_seen_ts = max(last_announce or 0, last_frame or 0)
                cameras.append({
                    "camera_id": cid, "name": cam_cfg.get("name") or cam.get("name", cid),
                    "zone": cam_cfg.get("zone") or cam.get("zone", ""),
                    "user_id": uid.name, "business_name": udata.get("business_name", ""),
                    "status": "online" if is_online else "offline", "active": is_online,
                    "last_announce": datetime.fromtimestamp(last_announce).isoformat() if last_announce else None,
                    "last_frame": datetime.fromtimestamp(last_frame).isoformat() if last_frame else None,
                    "last_seen": datetime.fromtimestamp(last_seen_ts).isoformat() if last_seen_ts else None,
                    "announce_age_s": int(announce_age) if announce_age else None,
                    "frame_age_s": int(frame_age) if frame_age else None,
                    # F-panel: telemetría completa (para columna fw y modal)
                    "firmware_version": cam_cfg.get("firmware_version", ""),
                    "local_ip": cam_cfg.get("local_ip", ""),
                    "rssi": cam_cfg.get("rssi"),
                    "type": cam_cfg.get("type", "esp32"),
                    "ingest_key": bool(cam_cfg.get("ingest_key")),
                    "stream_down": bool(cam_cfg.get("stream_down", False)),
                    "latest_frame_url": f"/frames/{uid.name}/{cid}/latest.jpg",
                    "disk_mount": str(uid.parent),
                })
    return {"cameras": cameras}


@app.put("/admin/cameras/{camera_id}")
async def admin_update_camera(camera_id: str, request: dict, authorization: str = Header(None)):
    _verify_admin(authorization)
    user_id = request.get("user_id", "")
    if not user_id:
        raise HTTPException(status_code=400, detail="user_id requerido")
    uf = find_user_json(user_id)
    if not uf or not uf.exists():
        raise HTTPException(status_code=404, detail="usuario no encontrado")
    with open(uf) as f:
        ud = json.load(f)
    for cam in ud.get("cameras", []):
        if cam.get("camera_id") == camera_id:
            for k in ("name", "zone", "cooldown_min", "brightness", "rotation"):
                if k in request:
                    cam[k] = request[k]
            break
    with open(uf, "w") as f:
        json.dump(ud, f, indent=2, ensure_ascii=False)
    return {"success": True}


@app.delete("/admin/cameras/{camera_id}")
async def admin_delete_camera(camera_id: str, authorization: str = Header(None)):
    _verify_admin(authorization)
    # buscar el user_id que tiene esta camara
    cfg = get_disk_config()
    for disk in cfg.get("disks", []):
        users_base = Path(disk.get("mount", "")) / disk.get("user_folder", "users")
        if not users_base.is_dir():
            continue
        for uid in users_base.iterdir():
            uf = uid / "user.json"
            if not uf.is_file():
                continue
            try:
                with open(uf) as f:
                    ud = json.load(f)
            except Exception:
                continue
            cams = ud.get("cameras", [])
            if any(c.get("camera_id") == camera_id for c in cams):
                ud["cameras"] = [c for c in cams if c.get("camera_id") != camera_id]
                with open(uf, "w") as f:
                    json.dump(ud, f, indent=2, ensure_ascii=False)
                return {"success": True, "user_id": uid.name}
    raise HTTPException(status_code=404, detail="Camara no encontrada")


# ── Admin: Events ───────────────────────────────────────────────────────

@app.get("/admin/events")
async def admin_list_events(limit: int = 50, user_id: str = None, event_type: str = None, authorization: str = Header(None)):
    _verify_admin(authorization)
    cfg = get_disk_config()
    events = []
    for disk in cfg.get("disks", []):
        users_base = Path(disk.get("mount", "")) / disk.get("user_folder", "users")
        if not users_base.is_dir():
            continue
        for uid in users_base.iterdir():
            if user_id and uid.name != user_id:
                continue
            for _cam_id, events_dir in resolve_user_events_dirs(uid.name):
                if not events_dir.exists():
                    continue
                for fname in sorted(events_dir.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
                    if not fname.name.endswith(".json"):
                        continue
                    if len(events) >= limit:
                        break
                    try:
                        with open(fname) as fh:
                            ev = json.load(fh)
                    except Exception:
                        continue
                    if event_type and ev.get("event_type") != event_type:
                        continue
                    ev["_user_id"] = uid.name
                    events.append(ev)
    events.sort(key=lambda e: e.get("timestamp", 0), reverse=True)
    return {"events": events[:limit], "total": len(events)}


@app.get("/admin/events/{event_id}")
async def admin_get_event(event_id: str, user_id: str = None,
                          authorization: str = Header(None)):
    """F-panel: detalle de un evento (lo llaman muro/monitoreo/tab eventos
    del panel — estaba en 404). Busca evt_*.json en todos los usuarios,
    devuelve el evento + imagen + datos de confirmación si existen."""
    _verify_admin(authorization)
    _validate_safe_path(event_id, "event_id")
    if not event_id.startswith("evt_"):
        event_id = "evt_" + event_id
    users_dir = STORAGE_ROOT / "users"
    if users_dir.is_dir():
        for udir in users_dir.iterdir():
            if not udir.is_dir() or (user_id and udir.name != user_id):
                continue
            for cdir in (udir / "cameras").glob("*/events") if (udir / "cameras").is_dir() else []:
                ef = cdir / f"{event_id}.json"
                if ef.exists():
                    try:
                        ev = json.loads(ef.read_text())
                    except Exception:
                        continue
                    ev["_user_id"] = udir.name
                    img = ef.with_suffix(".jpg")
                    if img.exists():
                        ev["image_url"] = f"/frames/{udir.name}/{ev.get('camera_id','')}/events/{img.name}"
                    return ev
    raise HTTPException(status_code=404, detail="Evento no encontrado")


@app.get("/admin/events/stats")
async def admin_events_stats(days: int = 7, authorization: str = Header(None)):
    _verify_admin(authorization)
    from collections import Counter, defaultdict
    cfg = get_disk_config()
    now = time.time()
    cutoff = now - days * 86400
    per_day = defaultdict(int)
    per_type = Counter()
    top_rules = Counter()
    total = 0
    for disk in cfg.get("disks", []):
        users_base = Path(disk.get("mount", "")) / disk.get("user_folder", "users")
        if not users_base.is_dir():
            continue
        for uid in users_base.iterdir():
            for cid, events_dir in resolve_user_events_dirs(uid.name):
                if not events_dir.exists():
                    continue
                for fname in events_dir.iterdir():
                    if not fname.name.endswith(".json"):
                        continue
                    try:
                        with open(fname) as fh:
                            ev = json.load(fh)
                    except Exception:
                        continue
                    ts = ev.get("timestamp", 0)
                    if ts < cutoff:
                        continue
                    day = datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d")
                    per_day[day] += 1
                    etype = ev.get("event_type", "unknown")
                    per_type[etype] += 1
                    if etype == "violation":
                        for r in ev.get("metadata", {}).get("violated_rules", []):
                            top_rules[r] += 1
                    total += 1
    daily = [{"date": d, "count": c} for d, c in sorted(per_day.items())]
    return {"days": days, "total_events": total, "by_day": daily,
            "by_type": dict(per_type), "top_rules": top_rules.most_common(10)}


@app.post("/admin/events/{event_id}/confirm")
async def admin_confirm_event(event_id: str, request: dict, authorization: str = Header(None)):
    _verify_admin(authorization)
    return {"success": True, "event_id": event_id, "action": "confirmed"}


@app.post("/admin/events/{event_id}/dismiss")
async def admin_dismiss_event(event_id: str, request: dict, authorization: str = Header(None)):
    _verify_admin(authorization)
    return {"success": True, "event_id": event_id, "action": "dismissed"}


# ── Admin: Stats ────────────────────────────────────────────────────────

@app.get("/admin/stats")
async def admin_stats(authorization: str = Header(None)):
    _verify_admin(authorization)
    cfg = get_disk_config()
    events_count = 0
    violations_count = 0
    for disk in cfg.get("disks", []):
        users_base = Path(disk.get("mount", "")) / disk.get("user_folder", "users")
        if not users_base.is_dir():
            continue
        for uid in users_base.iterdir():
            for _cam_id, events_dir in resolve_user_events_dirs(uid.name):
                if not events_dir.exists():
                    continue
                try:
                    # A4: contar *.json sin sh -c (antes: f"find '{events_dir}' ..."
                    # que era inyectable desde disks_config.json + bloqueaba el loop
                    # hasta 15s por camara). Ahora iterdir puro en Python.
                    for _f in events_dir.iterdir():
                        if _f.suffix == ".json":
                            events_count += 1
                except Exception:
                    logger.debug("silent: {exc}", exc=Exception)

    total_cameras = set()
    total_users = 0
    for disk in cfg.get("disks", []):
        users_base = Path(disk.get("mount", "")) / disk.get("user_folder", "users")
        if not users_base.is_dir():
            continue
        for uid in users_base.iterdir():
            total_users += 1
            user_file = uid / "user.json"
            if user_file.is_file():
                try:
                    with open(user_file) as fh:
                        udata = json.load(fh)
                    for cam in udata.get("cameras", []):
                        total_cameras.add(cam.get("camera_id", ""))
                except Exception:
                    logger.debug("silent: {exc}", exc=Exception)

    storage_used_gb = sum(d.get("used_gb", 0) or 0 for d in cfg.get("disks", []))
    # [Fix] active_cameras NO debe depender de la grilla en memoria del orquestador:
    # esa grilla se vacía en cada reinicio del backend y devolvía 0 aunque las
    # cámaras estuvieran enviando frames. Se calcula desde last_frame/last_announce
    # de user.json (misma lógica que /admin/cameras: online si < 120s).
    active_cams = 0
    try:
        _now = time.time()
        for disk in cfg.get("disks", []):
            _ub = Path(disk.get("mount", "")) / disk.get("user_folder", "users")
            if not _ub.is_dir():
                continue
            for _uid in _ub.iterdir():
                _uf = _uid / "user.json"
                if not _uf.is_file():
                    continue
                try:
                    _ud = json.loads(_uf.read_text())
                except Exception:
                    continue
                for _cam in _ud.get("cameras", []):
                    _la = _cam.get("last_announce") or 0
                    _lf = _cam.get("last_frame") or 0
                    _aa = (_now - _la) if _la else None
                    _fa = (_now - _lf) if _lf else None
                    if (_aa is not None and _aa < 120) or (_fa is not None and _fa < 120):
                        active_cams += 1
    except Exception as _e:
        logger.error(f"[admin_stats] active_cams calc error: {_e}")
    return {
        "total_users": total_users, "total_cameras": len(total_cameras),
        "active_cameras": active_cams, "storage_used_gb": round(storage_used_gb, 2),
        "total_events": events_count, "total_violations": violations_count
    }


# ── Admin: Server status ─────────────────────────────────────────────────

@app.get("/admin/server/status")
async def admin_server_status(authorization: str = Header(None)):
    _verify_admin(authorization)
    return {
        "status": "ok", "backend": "https://api.ojoia.com.do",
        "ngrok_url": "https://api.ojoia.com.do", "hostname": "ojoia-server",
        "local_ip": "10.0.0.44", "backend_port": 8005, "uptime_seconds": 0
    }


@app.post("/admin/server/cloudflared/save")
async def admin_cloudflared_save(request: dict, authorization: str = Header(None)):
    _verify_admin(authorization)
    return {"success": True}


@app.post("/admin/server/sync-firestore")
async def admin_sync_firestore(authorization: str = Header(None)):
    _verify_admin(authorization)
    return {"success": True}


# ── Admin: Disks ────────────────────────────────────────────────────────

@app.get("/admin/disks")
async def admin_disks(authorization: str = Header(None)):
    _verify_admin(authorization)
    return get_disk_config()


@app.get("/admin/disks/autodetect")
async def admin_disks_autodetect(authorization: str = Header(None)):
    _verify_admin(authorization)
    detected = []
    for base in ["/mnt", "/media", "/home"]:
        if Path(base).exists():
            try:
                for c in Path(base).iterdir():
                    mp = c.resolve()
                    if mp.is_mount() or (c.is_dir() and str(c) != base):
                        detected.append({"mount": str(mp), "label": c.name})
            except PermissionError:
                logger.debug("silent: {exc}", exc=PermissionError)

    return {"detected": detected}


@app.post("/admin/disks/scan")
async def admin_disks_scan(authorization: str = Header(None)):
    _verify_admin(authorization)
    cfg = get_disk_config()
    for disk in cfg.get("disks", []):
        mount = disk.get("mount", "")
        if mount and Path(mount).exists():
            try:
                st = os.statvfs(mount)
                total = st.f_blocks * st.f_frsize
                free = st.f_bavail * st.f_frsize
                disk["total_gb"] = round(total / (1024 ** 3), 1)
                disk["free_gb"] = round(free / (1024 ** 3), 1)
                disk["used_gb"] = round((total - free) / (1024 ** 3), 1)
            except OSError:
                logger.debug("silent: {exc}", exc=OSError)

    save_disk_config(cfg)
    return {"success": True, "disks": cfg.get("disks", [])}


@app.post("/admin/disks/save")
async def admin_disks_save(request: dict, authorization: str = Header(None)):
    _verify_admin(authorization)
    if "disks" not in request:
        raise HTTPException(status_code=400, detail="Missing disks")
    cfg = get_disk_config()
    cfg["disks"] = request["disks"]
    if "plans" in request:
        cfg["plans"] = request["plans"]
    save_disk_config(cfg)
    return {"success": True}


@app.post("/admin/disks/add")
async def admin_disks_add(request: dict, authorization: str = Header(None)):
    _verify_admin(authorization)
    cfg = get_disk_config()
    mount = request.get("mount", "")
    if not mount or mount in [d.get("mount") for d in cfg.get("disks", [])]:
        raise HTTPException(status_code=400, detail="Mount path empty or already exists")
    cfg.setdefault("disks", []).append({
        "mount": mount, "label": request.get("label", mount),
        "total_gb": 0, "used_gb": 0, "free_gb": 0,
        "user_folder": request.get("user_folder", "users")
    })
    save_disk_config(cfg)
    return {"success": True}


@app.get("/admin/logs")
async def admin_logs(authorization: str = Header(None), limit: int = 200, tail: int = 120):
    # [Fix] Endpoint que el SPA admin llama (loadLogs -> /admin/logs?limit=200&tail=120)
    # pero no existia -> 404 y la pestaña Logs quedaba rota. Devuelve:
    #   audit_logs: entradas de /home/sam/storage/admin_logs/*.jsonl (ts,action,actor,target,data)
    #   api_tail:    ultimas N lineas de api_eva.log (archivo grande -> solo la cola)
    _verify_admin(authorization)
    audit = []
    logs_dir = STORAGE_ROOT / "admin_logs"
    if logs_dir.is_dir():
        for fp in logs_dir.glob("*.jsonl"):
            try:
                with open(fp) as fh:
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            audit.append(json.loads(line))
                        except Exception:
                            logger.debug("silent: {exc}", exc=Exception)

            except Exception:
                logger.debug("silent: {exc}", exc=Exception)

    # Mas recientes primero
    audit.sort(key=lambda x: x.get("ts", ""), reverse=True)
    n_tail = max(1, tail)
    audit = audit[:n_tail]
    if limit and limit < len(audit):
        audit = audit[:limit]
    # Cola del log del API (archivo puede ser grande; leer solo el final)
    api_tail = []
    api_log = STORAGE_ROOT / "api_eva.log"
    if api_log.is_file():
        try:
            size = api_log.stat().st_size
            chunk = min(size, 200_000)
            with open(api_log, "rb") as f:
                f.seek(max(0, size - chunk))
                data = f.read().decode("utf-8", errors="ignore")
            api_tail = data.splitlines()[-n_tail:]
        except Exception:
            logger.debug("silent: {exc}", exc=Exception)

    return {"audit_logs": audit, "api_tail": api_tail}


@app.post("/admin/disks/remove")
async def admin_disks_remove(request: dict, authorization: str = Header(None)):
    _verify_admin(authorization)
    cfg = get_disk_config()
    mount = request.get("mount", "")
    cfg["disks"] = [d for d in cfg.get("disks", []) if d.get("mount") != mount]
    save_disk_config(cfg)
    return {"success": True}


# ── Admin: Storage ───────────────────────────────────────────────────────

@app.get("/admin/storage")
async def admin_storage_list(authorization: str = Header(None)):
    _verify_admin(authorization)
    cfg = get_disk_config()
    result = []
    for disk in cfg.get("disks", []):
        users_base = Path(disk.get("mount", "")) / disk.get("user_folder", "users")
        if not users_base.is_dir():
            continue
        for uid in users_base.iterdir():
            user_file = uid / "user.json"
            if user_file.is_file():
                try:
                    with open(user_file) as f:
                        ud = json.load(f)
                    check = _plan_check(ud)
                    result.append({
                        "user_id": uid.name, "name": ud.get("name", ""),
                        "plan": ud.get("plan", "free"),
                        "business_name": ud.get("business_name", ""),
                        "access_status": _compute_access_status(ud),
                        "days_left": check["days_left"],
                        "camera_count": len(ud.get("cameras", []))
                    })
                except Exception:
                    logger.debug("silent: {exc}", exc=Exception)

    return {"users": result}


@app.get("/admin/storage/{user_id}")
async def admin_get_user_storage(user_id: str, authorization: str = Header(None)):
    _verify_admin(authorization)
    cfg = get_disk_config()
    plan = "free"
    user_file = find_user_json(user_id)
    ud = {}
    if user_file and user_file.exists():
        with open(user_file) as f:
            ud = json.load(f)
        plan = ud.get("plan", "free")
    storage_path = get_user_storage_path(user_id, plan)
    # B3: _dir_used_mb subprocess du - offload al thread pool.
    used_mb = await asyncio.to_thread(_dir_used_mb, storage_path) if storage_path.exists() else 0
    plan_data = cfg.get("plans", {}).get(plan, {})
    return {
        "user_id": user_id, "disk_mount": str(storage_path.parent),
        "quota_gb": plan_data.get("max_storage_gb", 500), "used_mb": used_mb,
        "usage_percent": round(used_mb / (plan_data.get("max_storage_gb", 500) * 1024) * 100, 1) if plan_data.get("max_storage_gb") else 0,
        "plan": plan, "plan_status": _compute_access_status(ud) if ud else "active",
        "days_left": _plan_check(ud)["days_left"] if ud else 0,
        "max_cameras": plan_data.get("max_cameras", 1),
        "camera_count": len(ud.get("cameras", [])) if ud else 0
    }


@app.post("/admin/storage/{user_id}/update")
async def admin_update_user_storage(user_id: str, request: dict, authorization: str = Header(None)):
    _verify_admin(authorization)
    user_file = find_user_json(user_id)
    if not user_file or not user_file.exists():
        raise HTTPException(status_code=404, detail="Usuario no encontrado")
    def _mut_storage(user_data):
        if "plan" in request:
            user_data["plan"] = request["plan"]
        # el panel envía storage_quota_gb; aceptar ambas claves
        q = request.get("quota_gb", request.get("storage_quota_gb"))
        if q:
            try:
                user_data["quota_gb"] = float(q)
            except (TypeError, ValueError):
                pass
        if request.get("disk_mount"):
            user_data["disk_mount"] = request["disk_mount"]
        if request.get("max_cameras"):
            try:
                user_data["max_cameras"] = int(request["max_cameras"])
            except (TypeError, ValueError):
                pass
    update_user_json(user_id, _mut_storage)  # C1
    invalidate_user_disk_cache(user_id)
    _q = request.get("quota_gb", request.get("storage_quota_gb"))
    logger.info(f"[ST-2] storage update {user_id}: "
                f"plan={request.get('plan')} quota={_q} disk={request.get('disk_mount')}")
    return {"success": True}


@app.post("/admin/storage/{user_id}/migrate")
async def admin_migrate_user(user_id: str, request: dict, authorization: str = Header(None)):
    """ST-2: migración REAL de un usuario a otro disco.
    1. rsync de todos sus datos (cámaras/eventos/frames) al disco destino
    2. disk_mount en user.json (compat + nuevo) → resolución apunta al nuevo
    3. sin borrar el origen hasta que el operador lo confirme (safe)
    El ingest empieza a escribir en el nuevo disco apenas cambia el
    disk_mount (caché de 5s)."""
    _verify_admin(authorization)
    new_disk = request.get("disk_mount", "").rstrip("/")
    if not new_disk or not Path(new_disk).exists():
        raise HTTPException(status_code=400, detail="disk_mount inexistente")
    # ST-3b: sanitizar — solo se acepta un MOUNT registrado en disks_config
    # (el bug '/mnt/.../users' provenía de formatos inconsistentes)
    _cfgd = get_disk_config()
    _valid = [d.get("mount") for d in _cfgd.get("disks", [])]
    if new_disk not in _valid:
        raise HTTPException(status_code=400,
                            detail=f"disk_mount no es un disco registrado: {new_disk} (válidos: {_valid})")
    if new_disk.endswith("/users"):
        raise HTTPException(status_code=400, detail="disk_mount no debe incluir /users")
    old_file = find_user_json(user_id)
    if not old_file or not old_file.exists():
        raise HTTPException(status_code=404, detail="Usuario no encontrado")
    existing = json.loads(old_file.read_text())
    old_dir = old_file.parent  # carpeta users/<uid> real (puede ser otra)

    new_dir = Path(new_disk) / "users" / user_id

    def _rsync():
        import subprocess
        new_dir.mkdir(parents=True, exist_ok=True)
        r = subprocess.run(
            ["rsync", "-a", "--exclude", "user.json", str(old_dir) + "/", str(new_dir) + "/"],
            capture_output=True, text=True, timeout=3600)
        return r.returncode == 0, r.stderr[-500:] if r.returncode else ""
    ok, err = await asyncio.to_thread(_rsync)
    if not ok:
        raise HTTPException(status_code=500, detail=f"rsync falló: {err}")

    existing["disk_mount"] = new_disk
    existing["migrated_at"] = time.time()
    with _get_user_lock(user_id):
        _atomic_write_user_json(new_dir / "user.json", existing)
        # ST-3b: el compat es SIEMPRE en STORAGE_ROOT (el index estable de
        # user.json) — antes usaba user_root() que ya apuntaba al disco
        # NUEVO, dejando el NVMe con un user.json desactualizado.
        compat = STORAGE_ROOT / "users" / user_id
        compat.mkdir(parents=True, exist_ok=True)
        _atomic_write_user_json(compat / "user.json", existing)
    invalidate_user_disk_cache(user_id)
    logger.info(f"[ST-2] usuario {user_id} migrado a {new_dir} "
                f"(origen conservado en {old_dir})")
    return {"success": True, "new_path": str(new_dir), "old_path": str(old_dir),
            "note": "Datos copiados; el origen se conserva. Bórralo manualmente "
                    "cuando confirmes que todo funciona."}


# ── Admin: Queue & Eva config ────────────────────────────────────────────

@app.get("/admin/queue")
async def admin_queue(authorization: str = Header(None)):
    _verify_admin(authorization)
    return {"queue_length": 0, "processing": 0, "done": 0, "error": 0,
            "queue_size_mb": 0, "oldest_item": None, "last_processed": None,
            "worker_running": True, "pending_frames": [], "grid_frames": 0,
            "grid_ready": False}


@app.post("/admin/queue/clear")
async def admin_clear_queue(authorization: str = Header(None)):
    _verify_admin(authorization)
    return {"success": True}


@app.get("/admin/eva-config")
async def admin_eva_config(authorization: str = Header(None)):
    _verify_admin(authorization)
    try:
        with open(EVA_CONFIG_FILE) as f:
            cfg = json.load(f)
    except Exception:
        cfg = {"prompt": ""}
    return {"prompt": cfg.get("prompt", ""), "docs": cfg.get("docs", []),
            "violation_cooldown_min": cfg.get("violation_cooldown_min", 5)}


@app.post("/admin/system/prompts")
async def admin_save_prompt(request: dict, authorization: str = Header(None)):
    _verify_admin(authorization)
    try:
        with open(EVA_CONFIG_FILE) as f:
            cfg = json.load(f)
    except Exception:
        cfg = {}
    cfg["prompt"] = request.get("prompt", cfg.get("prompt", ""))
    if "violation_cooldown_min" in request:
        cfg["violation_cooldown_min"] = request["violation_cooldown_min"]
    with open(EVA_CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2)
    return {"success": True}


@app.get("/admin/eva-docs")
async def admin_eva_docs(authorization: str = Header(None)):
    _verify_admin(authorization)
    try:
        with open(EVA_CONFIG_FILE) as f:
            cfg = json.load(f)
    except Exception:
        cfg = {}
    return {"documents": cfg.get("docs", [])}


@app.get("/admin/eva-docs/{doc_name}")
async def admin_get_eva_doc(doc_name: str, authorization: str = Header(None)):
    _verify_admin(authorization)
    try:
        with open(EVA_CONFIG_FILE) as f:
            cfg = json.load(f)
    except Exception:
        cfg = {}
    docs = cfg.get("docs_content", {})
    return {"name": doc_name, "content": docs.get(doc_name, "# " + doc_name)}


@app.post("/admin/eva-docs/save")
async def admin_save_eva_doc(request: dict, authorization: str = Header(None)):
    _verify_admin(authorization)
    try:
        with open(EVA_CONFIG_FILE) as f:
            cfg = json.load(f)
    except Exception:
        cfg = {}
    docs_content = cfg.get("docs_content", {})
    name = request.get("name", "")
    docs_content[name] = request.get("content", "")
    cfg["docs_content"] = docs_content
    if name and name not in cfg.get("docs", []):
        cfg.setdefault("docs", []).append(name)
    with open(EVA_CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2)
    return {"success": True}


@app.post("/admin/calc-tokens")
async def admin_calc_tokens(request: dict, authorization: str = Header(None)):
    _verify_admin(authorization)
    prompt = request.get("prompt", "")
    return {"tokens": max(1, len(prompt) // 3)}


@app.post("/admin/queue/firestore/process")
async def admin_process_firestore_queue(request: dict, authorization: str = Header(None)):
    _verify_admin(authorization)
    return {"success": True, "processed": 0, "message": "Cola Firestore: sin pendientes"}


@app.post("/admin/queue/firestore/clear")
async def admin_clear_firestore_queue(request: dict, authorization: str = Header(None)):
    _verify_admin(authorization)
    return {"success": True, "deleted": 0}


@app.get("/admin/queue/firestore/status")
async def admin_firestore_queue_status(authorization: str = Header(None)):
    _verify_admin(authorization)
    return {"success": True, "queue_length": 0, "total_size_kb": 0, "frames": []}


@app.get("/admin/queue/status")
async def admin_queue_status(authorization: str = Header(None)):
    _verify_admin(authorization)
    frame_count = 0
    try:
        frame_count = sum(g.get_frame_count() for g in orchestrator.grids.values())
    except Exception:
        logger.debug("silent: {exc}", exc=Exception)

    return {
        "queue_length": frame_count, "processing": 0, "done": 0, "error": 0,
        "queue_size_mb": 0, "oldest_item": None, "last_processed": None,
        "worker_running": True, "pending_frames": [], "grid_frames": frame_count,
        "grid_ready": frame_count >= 16,
        "firebase_queue": {"pending_frames": 0, "total_kb": 0, "bucket": "ojoia-67216.firebasestorage.app"}
    }


@app.post("/admin/firebase/update-server-status")
async def admin_update_server_status(request: dict, authorization: str = Header(None)):
    _verify_admin(authorization)
    return {"success": True, "backend": "https://api.ojoia.com.do"}


# ═══════════════════════════════════════════════════════════════════════════
# B2 — Configuracion de retencion de eventos (editable desde admin panel)
# ═══════════════════════════════════════════════════════════════════════════
# Persiste en admin_config.json bajo "retention.days_by_plan" para que el
# cron cleanup_frames.py lo lea. Si falta la key, usa defaults codificados.

# Defaults hardcoded (single source of truth para el script y la API):
DEFAULT_RETENTION = {
    "days_by_plan": {
        "active": 7,
        "trial": 7,
        "free": 1,
        "expired": 1,
    },
    "frames_hours_by_plan": {
        "active": 24,
        "trial": 24,
        "free": 0.75,
        "expired": 0.75,
    },
    "cleanup_cron": "0 3 * * *",  # frecuencia default (3 AM diaria)
}


def get_retention_config() -> dict:
    """Lee retention de admin_config.json + aplica defaults donde falte."""
    cfg = _load_admin_config()
    saved = cfg.get("retention", {}) or {}
    out = {
        "days_by_plan": {**(DEFAULT_RETENTION["days_by_plan"]),
                          **(saved.get("days_by_plan") or {})},
        "frames_hours_by_plan": {**(DEFAULT_RETENTION["frames_hours_by_plan"]),
                                  **(saved.get("frames_hours_by_plan") or {})},
        "cleanup_cron": saved.get("cleanup_cron") or DEFAULT_RETENTION["cleanup_cron"],
    }
    return out


@app.post("/admin/cleanup/run")
async def admin_cleanup_run(authorization: str = Header(None, alias="Authorization")):
    """F-robustez: ejecutar la limpieza AHORA desde el panel (async, no
    bloquea; corre el mismo cleanup_frames.py del cron)."""
    _verify_admin(authorization)
    import subprocess

    def _run():
        return subprocess.run(
            ["/opt/ojoia/venv/bin/python", "-u", "/opt/ojoia/code/cleanup_frames.py"],
            capture_output=True, text=True, timeout=900)
    try:
        r = await asyncio.to_thread(_run)
        ok = r.returncode == 0
        # extraer la línea final de resumen
        tail = (r.stdout or "").strip().splitlines()[-8:]
        return {"success": ok, "message": "Limpieza ejecutada (ver log)",
                "tail": tail}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"cleanup falló: {e}")


# ── HOT-COLD F3: alerta de disco al operador ────────────────────────────────
_DISK_ALERT_TS = {"last": 0.0}


def _disks_usage() -> list:
    """Uso real (statvfs) de todos los mounts registrados + STORAGE_ROOT."""
    out = []
    seen = set()
    try:
        cfg = get_disk_config()
        mounts = [d.get("mount") for d in cfg.get("disks", [])]
    except Exception:
        mounts = []
    for m in mounts + [str(STORAGE_ROOT), "/"]:
        if not m or m in seen:
            continue
        seen.add(m)
        try:
            st = os.statvfs(m)
            total_gb = (st.f_blocks * st.f_frsize) / 1e9
            free_gb = (st.f_bavail * st.f_frsize) / 1e9
            if total_gb < 1:
                continue
            pct = 100 * (1 - st.f_bavail / st.f_blocks) if st.f_blocks else 0
            out.append({"mount": m, "total_gb": round(total_gb, 1),
                        "free_gb": round(free_gb, 1), "used_pct": round(pct, 1),
                        "level": "critical" if pct > 92 else
                                 ("warning" if pct > 80 else "ok")})
        except OSError:
            continue
    return out


# ── HOT-COLD F2: protocolo de cambio de disco con ARCHIVADO ────────────────
@app.post("/admin/disks/{mount_path:path}/archive")
async def admin_archive_disk(mount_path: str, authorization: str = Header(None, alias="Authorization"),
                             request: dict = None):
    """HOT-COLD F2: archivar un disco lleno SIN mover TODO.

    Protocolo de cambio de disco (el que pediste: 'cuando se cambie el
    almacenimiento de un disco a otro no falle el sistema'):
    1. Marca el disco como archived=True en disks_config (los usuarios
       siguen RESOLVIENDO a él para LECTURA de eventos históricos — el
       reader de eventos busca en TODOS los discos no-archivados primero
       y luego archivados).
    2. Los usuarios con disk_mount = ese disco se reasignan al disco
       destino indicado → los eventos NUEVOS van al disco nuevo.
    3. NO mueve nada: el histórico sigue accesible en el disco viejo
       (read-only conceptual). El cleanup respeta archivado (no purga
       huérfanos de discos archivados que sigan montados).

    Body: {target_mount: "/mnt/disco-nuevo"}"""
    _verify_admin(authorization)
    mount = "/" + mount_path.lstrip("/")
    if not Path(mount).is_dir():
        raise HTTPException(status_code=404, detail=f"montaje no existe: {mount}")
    return await _archive_disk_impl(mount, authorization,
                                   (request or {}).get("target_mount"))


async def _archive_disk_impl(mount: str, authorization: str = None,
                             target_mount: Optional[str] = None):
    cfg = get_disk_config()
    disks = cfg.get("disks", [])
    src = next((d for d in disks if d.get("mount") == mount), None)
    if not src:
        raise HTTPException(status_code=404, detail="disco no registrado")
    if src.get("archived"):
        return {"success": True, "already_archived": True}
    # disco destino: el de más espacio que no esté archivado
    if not target_mount:
        cands = [d for d in disks if not d.get("archived") and d.get("mount") != mount]
        if not cands:
            raise HTTPException(status_code=400, detail="no hay disco destino disponible")
        target_mount = max(cands, key=lambda d: d.get("free_gb", 0)).get("mount")
    if target_mount == mount:
        raise HTTPException(status_code=400, detail="destino = origen")
    tgt = next((d for d in disks if d.get("mount") == target_mount), None)
    if not tgt:
        raise HTTPException(status_code=400, detail=f"destino no registrado: {target_mount}")

    # 1) marcar archivado
    src["archived"] = True
    src["archived_at"] = time.time()
    src["archive_target"] = target_mount
    # 2) reasignar usuarios del disco archivado
    moved = []
    users_dir = STORAGE_ROOT / "users"
    if users_dir.is_dir():
        for uf in users_dir.glob("*/user.json"):
            try:
                ud = json.loads(uf.read_text())
                if ud.get("disk_mount") == mount:
                    ud["disk_mount"] = target_mount
                    uid = uf.parent.name
                    with _get_user_lock(uid):
                        _atomic_write_user_json(uf, ud)
                        # espejo en el disco destino
                        tdir = Path(target_mount) / "users" / uid
                        tdir.mkdir(parents=True, exist_ok=True)
                        _atomic_write_user_json(tdir / "user.json", ud)
                    invalidate_user_disk_cache(uid)
                    moved.append(uid)
            except Exception:
                continue
    # 3) persistir config
    cfg["disks"] = disks
    save_disk_config(cfg)
    logger.info(f"[F2] disco {mount} ARCHIVADO → {target_mount}; "
                f"{len(moved)} usuarios reasignados: {moved[:5]}")
    return {"success": True, "archived": mount, "target": target_mount,
            "users_moved": moved,
            "note": "Eventos históricos siguen legibles en el disco archivado "
                    "(read-only). Los NUEVOS van al destino."}


@app.get("/admin/disks/{mount_path:path}/archive-status")
async def admin_archive_status(mount_path: str, authorization: str = Header(None, alias="Authorization")):
    """F2: estado de archivado de un disco + qué usuarios lo usan."""
    _verify_admin(authorization)
    mount = "/" + mount_path.lstrip("/")
    cfg = get_disk_config()
    d = next((x for x in cfg.get("disks", []) if x.get("mount") == mount), {})
    users_on = []
    for uf in (STORAGE_ROOT / "users").glob("*/user.json"):
        try:
            if json.loads(uf.read_text()).get("disk_mount") == mount:
                users_on.append(uf.parent.name)
        except Exception:
            pass
    return {"mount": mount, "archived": d.get("archived", False),
            "archived_at": d.get("archived_at"), "users_on_disk": users_on}


@app.get("/admin/disks/usage")
async def admin_disks_usage(authorization: str = Header(None, alias="Authorization")):
    """HOT-COLD F3: uso real por disco con niveles ok/warning/critical
    (para el semáforo del tab Almacenamiento)."""
    _verify_admin(authorization)
    return {"disks": _disks_usage()}


@app.post("/admin/disks/check-alerts")
async def admin_disks_check_alerts(authorization: str = Header(None, alias="Authorization")):
    """HOT-COLD F3: alerta de disco >80% al email del operador.
    Anti-spam: máx 1 por hora. Pensado para el cron del health-monitor
    o el expirations_job diario; también manual desde el panel."""
    _verify_admin(authorization)
    disks = _disks_usage()
    problem = [d for d in disks if d["level"] != "ok"]
    if not problem:
        return {"success": True, "alerts_sent": 0, "disks": disks}
    now = time.time()
    if now - _DISK_ALERT_TS["last"] < 3600:
        return {"success": True, "alerts_sent": 0, "reason": "antispam 1h",
                "disks": disks}
    _DISK_ALERT_TS["last"] = now
    lines = [f"⚠️ {d['mount']}: {d['used_pct']}% usado ({d['free_gb']} GB libres) — nivel {d['level']}"
             for d in problem]
    title = f"[OjoIA] Disco {'CRÍTICO' if any(d['level']=='critical' for d in problem) else 'al 80%'}"
    body = "Almacenamiento requiere atención:\n" + "\n".join(lines) + \
           "\n\nAcciones: ojoia.com.do/admin → Almacenamiento (retención/cleanup), o montar el siguiente disco."
    sent = False
    try:
        from health_monitor import _send_alert
        sent = await asyncio.to_thread(_send_alert, title, body)
    except Exception:
        # fallback directo SMTP (mismo canal de alertas)
        try:
            import smtplib
            from email.mime.text import MIMEText
            smtp_user = os.environ.get("ALERT_SMTP_USER", "")
            if smtp_user:
                msg = MIMEText(body)
                msg["Subject"] = title
                msg["From"] = smtp_user
                msg["To"] = os.environ.get("ALERT_EMAIL_TO", "samuel0117@gmail.com")
                with smtplib.SMTP("smtp.gmail.com", 587, timeout=15) as s:
                    s.starttls(); s.login(smtp_user, os.environ.get("ALERT_SMTP_PASS", ""))
                    s.send_message(msg)
                sent = True
        except Exception:
            pass
    logger.warning(f"[F3] alerta disco: {problem} (enviada={sent})")
    return {"success": True, "alerts_sent": 1 if sent else 0,
            "problems": problem, "channel_ok": sent}


@app.get("/admin/health/summary")
async def admin_health_summary(authorization: str = Header(None, alias="Authorization")):
    """F-robustez: salud integral para el semáforo del panel.
    Disco (ok/warning/critical), última corrida del cleanup (del log),
    drops 24h de la cola, cámaras offline >1h y estado de Redis."""
    _verify_admin(authorization)
    # disco
    import os as _os
    try:
        st = _os.statvfs(STORAGE_ROOT)
        free_gb = st.f_bavail * st.f_frsize / 1e9
        pct = 100 * (1 - st.f_bavail / st.f_blocks)
        disk = {"free_gb": round(free_gb, 1), "used_pct": round(pct, 1),
                "level": "critical" if pct > 90 else ("warning" if pct > 80 else "ok")}
    except Exception as e:
        disk = {"level": "error", "error": str(e)}
    # última corrida del cleanup (del log rotativo)
    cleanup_last = None
    try:
        logp = Path("/home/sam/storage/cleanup_frames.log")
        if logp.exists():
            for line in reversed(logp.read_text(errors="replace").splitlines()):
                if "Limpieza v2 completada" in line:
                    # "[2026-09-01 02:37:13] Limpieza v2 completada."
                    cleanup_last = line.split("]")[0].lstrip("[")
                    break
    except Exception:
        pass
    # drops 24h aproximados: desde el estado del bus
    queue = await frame_bus.stats()
    # cámaras offline >1h
    cams = _scan_all_cameras_admin()
    offline_1h = [c["camera_id"] for c in cams
                  if not c["online"] and c["last_frame"]
                  and (time.time() - c["last_frame"]) > 3600]
    return {
        "disk": disk,
        "cleanup": {"last_run": cleanup_last,
                    "cron": get_retention_config().get("cleanup_cron", "0 3 * * *")},
        "queue": {"mode": queue.get("mode"), "drops": FRAME_QUEUE_DROPS,
                  "pending": queue.get("pending", 0)},
        "cameras_offline_1h": offline_1h,
        "overall": disk["level"],
    }


@app.get("/admin/retention")
async def admin_get_retention(authorization: str = Header(None)):
    """Devuelve configuracion de retencion actual (工期days_by_plan, frames_hours_by_plan, cron)."""
    _verify_admin(authorization)
    return {"success": True, "retention": get_retention_config(),
            "defaults": DEFAULT_RETENTION}


@app.put("/admin/retention")
async def admin_update_retention(request: dict, authorization: str = Header(None)):
    """
    Actualiza configuracion de retencion. Body parcial aceptado:
      {"days_by_plan": {"active": 7, "trial": 7, "free": 1, "expired": 1},
       "frames_hours_by_plan": {"active": 24, ...},
       "cleanup_cron": "0 3 * * *"}
    Solo persiste keys presentes; las ausentes conservan defaults.

    NOTA sobre cleanup_cron: se persiste como referencia para el operador,
    pero NO se aplica automaticamente al crontab del sistema (riesgo: el
    proceso API no deberia escribir el crontab del usuario). Si el operador
    cambia la frecuencia, debe actualizar `crontab -e` manualmente con el
    valor guardado aqui. El valor default es "0 3 * * *" (3 AM diaria).
    """
    _verify_admin(authorization)
    current = get_retention_config()
    # merge defensivo: solo aceptar keys conocidas y tipos validos
    if "days_by_plan" in request and isinstance(request["days_by_plan"], dict):
        for plan, val in request["days_by_plan"].items():
            if plan not in DEFAULT_RETENTION["days_by_plan"]:
                continue
            try:
                iv = int(val)
                if iv == 0 or iv >= 1:  # min 1 día de retention (0 = borrar todo, peligroso)
                    current["days_by_plan"][plan] = max(1, iv)
            except (TypeError, ValueError):
                continue
    if "frames_hours_by_plan" in request and isinstance(request["frames_hours_by_plan"], dict):
        for plan, val in request["frames_hours_by_plan"].items():
            if plan not in DEFAULT_RETENTION["frames_hours_by_plan"]:
                continue
            try:
                fv = float(val)
                if 0 < fv <= 24 * 7:  # 1 min a 7 días
                    current["frames_hours_by_plan"][plan] = fv
            except (TypeError, ValueError):
                continue
    if "cleanup_cron" in request and isinstance(request["cleanup_cron"], str):
        # Validacion minima de formato cron (5 campos)
        parts = request["cleanup_cron"].split()
        if len(parts) == 5:
            current["cleanup_cron"] = request["cleanup_cron"]
    # persistir
    cfg = _load_admin_config()
    cfg["retention"] = {
        "days_by_plan": current["days_by_plan"],
        "frames_hours_by_plan": current["frames_hours_by_plan"],
        "cleanup_cron": current["cleanup_cron"],
        "updated_at": int(time.time()),
    }
    _save_admin_config(cfg)
    logger.info(f"[admin] retention actualizada: days={current['days_by_plan']} "
                f"frames_h={current['frames_hours_by_plan']} cron={current['cleanup_cron']}")
    return {"success": True, "retention": current}


if __name__ == "__main__":
    import uvicorn
    # A8: bind loopback. Antes host="0.0.0.0" exponía el API a todas las
    # interfaces (incluso interfaces externas si las hubiera). Ahora solo
    # escucha en 127.0.0.1:8005; todo el trafico legitimo llega via nginx
    # (upstream backend_eva -> 127.0.0.1:8005) o desde cloudflared/tunel.
    # No se pierde acceso: backend_eva en nginx.conf apunta a 127.0.0.1:8005.
    # P-shutdown (2026-09-01): timeout_graceful=10s. El shutdown de uvicorn
    # espera INDEFINIDAMENTE conexiones keep-alive abiertas — las cámaras
    # ESP32 mantienen el socket vivo y el proceso viejo retiene el puerto
    # 8005 tras cada deploy (hubo que kill -9 dos veces hoy). Con esto:
    # 10s de gracia para terminar requests en vuelo y cierre forzado limpio.
    uvicorn.run(app, host="127.0.0.1", port=8005, loop="asyncio",
                timeout_graceful_shutdown=10)
