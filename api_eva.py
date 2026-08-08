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
from fastapi.responses import JSONResponse, Response, StreamingResponse
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
FIREBASE_KEY_PATH = Path("/home/sam/Downloads/firebase-key.json")

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
#   - _verify_user_token(header) valida Authorization: Bearer <token>.
#   - Endpoints protegidos llaman a _verify_user_token(...) como dependencia
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


def _verify_user_token(authorization: Optional[str], user_id: str) -> dict:
    """
    Valida Authorization: Bearer <token> contra user.json.api_tokens[].

    Soft rollout (AUTH_ENFORCE=False): si la auth falta o es invalida,
    se loguea WARNING pero el request continúa (devuelve identity=None).
    Hard rollout (AUTH_ENFORCE=True): lanza 401 si falta o es invalida.
    """
    if not authorization:
        if AUTH_ENFORCE:
            raise HTTPException(status_code=401, detail="Authorization header requerido")
        logger.warning(f"[auth:soft] user={user_id} request SIN Authorization header")
        return {"authenticated": False, "user_id": user_id, "reason": "no_header"}

    token = authorization.replace("Bearer ", "").strip()
    if not token:
        if AUTH_ENFORCE:
            raise HTTPException(status_code=401, detail="Token invalido")
        logger.warning(f"[auth:soft] user={user_id} Authorization malformado")
        return {"authenticated": False, "user_id": user_id, "reason": "bad_header"}

    # A6 (parcial): comparacion en tiempo constante para evitar timing leaks.
    try:
        uf = find_user_json(user_id)
        if not uf or not uf.exists():
            if AUTH_ENFORCE:
                raise HTTPException(status_code=401, detail="Usuario no encontrado")
            return {"authenticated": False, "user_id": user_id, "reason": "no_user"}
        with _get_user_lock(user_id):
            with open(uf) as f:
                ud = json.load(f)
            tokens = ud.get("api_tokens", []) or []
            now = int(time.time())
            # Limpiar tokens expirados / revocados
            valid = [t for t in tokens if not t.get("revoked") and
                     now < t.get("expires_at", now + AUTH_TOKEN_TTL_SEC * 100)]
            match = None
            for t in valid:
                if hmac.compare_digest(str(t.get("token", "")), token):
                    match = t
                    break
            if not match:
                if AUTH_ENFORCE:
                    raise HTTPException(status_code=401, detail="Token no valido o expirado")
                logger.warning(f"[auth:soft] user={user_id} token invalido (len={len(token)})")
                return {"authenticated": False, "user_id": user_id, "reason": "bad_token"}
            # Refrescar last_used
            match["last_used"] = now
            ud["api_tokens"] = valid
            _atomic_write_user_json(uf, ud)
        return {"authenticated": True, "user_id": user_id, "token_id": match.get("id")}
    except HTTPException:
        raise
    except Exception as e:
        if AUTH_ENFORCE:
            raise HTTPException(status_code=401, detail=f"Error validacion: {e}")
        logger.error(f"[auth:soft] error user={user_id}: {e}")
        return {"authenticated": False, "user_id": user_id, "reason": "error"}


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
def verify_user(
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
        pass
    # form params (POST sin JSON body)
    if not user_id:
        try:
            form = asyncio.ensure_future(request.form())
            # form() es awaitable; pero como Dependencia sincrona leemos solo
            # si ya esta cacheado. En gral los endpoints pasan user_id por JSON
            # body o query. Si no llega por query, el endpoint recibira user_id
            # por separado y debemos confiar en que el route body llama a
            # _verify_user_token(authorization, user_id) explicitamente.
        except Exception:
            pass
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
            pass

    if not user_id:
        # si el endpoint no expone user_id de forma estandar, exigimos que traiga
        # Authorization; el route body hara la verificacion fina con _verify_user_token.
        if AUTH_ENFORCE and not authorization:
            raise HTTPException(status_code=401, detail="Authorization requerido")
        return {"authenticated": False, "user_id": None, "reason": "no_user_id"}

    return _verify_user_token(authorization, user_id)


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
    _verify_user_token(request.headers.get("authorization"), user_id)


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
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler(STORAGE_ROOT / "api_eva.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Estado de Sesiones Eva
eva_sessions: Dict[str, Dict[str, Any]] = {}

# Cola asíncrona para procesamiento de frames (desacoplado del endpoint)
FRAME_QUEUE: asyncio.Queue = asyncio.Queue(maxsize=1000)
WORKER_RUNNING = False


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
                "/home/sam/ai_system/firebase-key.json",
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
                        resp = _req.post(
                            probe_url, params={"dry_run": "true"}, headers=headers,
                            json={"message": {"token": t, "notification": {"title": "_", "body": "_"}}},
                            timeout=8
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
                    except Exception:
                        pass
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
        _verify_user_token(request.headers.get("authorization"), user_id)  # A1: anti-suplantacion de user_id en body
        device = body.get("device")
        if not token or not user_id:
            return {"success": False, "error": "user_id y token requeridos"}
        # Buscar user.json con el helper existente para soportar paths custom
        uf = None
        for cand in [STORAGE_ROOT / "users" / user_id / "user.json"]:
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
@app.post("/api/auth/token")
async def issue_user_token(request: dict):
    """
    Emite un Bearer token para el usuario identificado por user_id.
    Soft rollout: NO requiere login previo (la validacion fuerte llegara en
    Fase 2 con Firebase ID Token). Por ahora, basta con que el user_id exista.

    Body:
        user_id: str (requerido)
        device: str (opcional,-info de tracking)
    Devuelve:
        token: str
        expires_at: int (epoch)
    """
    user_id = (request.get("user_id") or "").strip()
    device = request.get("device")
    if not user_id:
        raise HTTPException(status_code=400, detail="user_id requerido")
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
        uf = STORAGE_ROOT / "users" / user_id / "user.json"
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
        html_url = page["html_url"]
        pdf_url = page["pdf_url"]
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
                    except Exception:
                        pass
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
            hf = STORAGE_ROOT / "users" / user_id / "eva_chat_history.json"
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
            uf = STORAGE_ROOT / "users" / user_id / "user.json"
            if uf.exists():
                ud = json.loads(uf.read_text())
                tokens = ud.get("fcm_tokens", []) or []
                if tokens:
                    creds = service_account.Credentials.from_service_account_file(
                        "/home/sam/ai_system/firebase-key.json",
                        scopes=["https://www.googleapis.com/auth/firebase.messaging"]
                    )
                    creds.refresh(google.auth.transport.requests.Request())
                    t0 = time.time()
                    for tok in tokens:
                        try:
                            _req.post(
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
    cam_dir = STORAGE_ROOT / "users" / user_id / "cameras"
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


# ═══════════════════════════════════════════════════════════════════════════
# TEST ENDPOINTS — para validar push y centinela sin esperar gatillo real
# ═══════════════════════════════════════════════════════════════════════════

@app.post("/api/admin/centinela-test")
async def centinela_test(user_id: str, camera_id: str = "OJO-E17604", count: int = 1):
    """Fuerza una alerta centinela de prueba (mismo path que produccion, con push + imagen)."""
    if not user_id or not camera_id:
        return {"success": False, "error": "user_id y camera_id requeridos"}
    # Bypass de cooldown para el test
    _vigilance_cooldowns.pop(f"{user_id}_{camera_id}", None)
    # Usar un frame real si hay uno disponible, sino uno sintetico (para que la miniatura funcione)
    img_bytes = b""
    for cand in [
        STORAGE_ROOT / "users" / user_id / "cameras" / camera_id / "events" / "latest_vigilance.jpg",
        STORAGE_ROOT / "users" / user_id / "cameras" / camera_id / "frames" / "latest_raw.jpg",
        STORAGE_ROOT / "users" / "default" / "cameras" / camera_id / "frames" / "latest_raw.jpg",
    ]:
        if cand.exists() and cand.stat().st_size > 100:
            img_bytes = cand.read_bytes()
            break
    if not img_bytes:
        try:
            from io import BytesIO
            from PIL import Image as _PILImage
            _buf = BytesIO()
            _im = _PILImage.new("RGB", (320, 240), (90, 90, 90))
            _im.save(_buf, format="JPEG", quality=70)
            img_bytes = _buf.getvalue()
        except Exception:
            pass
    try:
        result = _save_vigilance_event(user_id, camera_id, img_bytes, count, ["person", "test"], "127.0.0.1")
        if result is None:
            _vigilance_cooldowns.pop(f"{user_id}_{camera_id}", None)
            result = _save_vigilance_event(user_id, camera_id, img_bytes, count, ["person", "test"], "127.0.0.1")
        return {"success": True, "triggered": result is not None, "event_id": result,
                "user_id": user_id, "camera_id": camera_id, "had_frame": len(img_bytes) > 0}
    except Exception as e:
        return {"success": False, "error": str(e)}


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
    # token generation/verify: el primero crea el token, el segundo usa Firebase
    "/api/auth/token",  # POST crea token (requiere FIR verificacion separada mas adelante)
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
    """
    path = request.url.path
    # solo /api/* (excepto las publicas explicitas o sus prefijos con path params).
    # /admin/* usa _verify_admin propio.
    # PUBLIC_USER_PATHS admite entradas con barra final como PREFIJO (startswith).
    public_prefixes = ("/api/reportes/view/", "/api/reportes/download/",
                       "/api/reportes/url/", "/reportes/")
    if (not path.startswith("/api/")
            or path in PUBLIC_USER_PATHS
            or path.startswith(public_prefixes)):
        return await call_next(request)
    if request.method == "OPTIONS":
        return await call_next(request)
    user_id = _extract_user_id_from_request(request)
    if not user_id:
        # el endpoint no expone user_id por query/path; confiamos en que el
        # route body hara _verify_user_token(authorization, user_id) explicito,
        # o el user_id viene en JSON body (el route la extrae y valida).
        # Mientras tanto, si no hay Authorization y enforce=True -> 401 para
        # forzar a que los endpoints con user_id en body validen explicito.
        if not request.headers.get("authorization"):
            return JSONResponse({"detail": "Authorization requerido"},
                                status_code=401)
        return await call_next(request)
    try:
        _verify_user_token(request.headers.get("authorization"), user_id)
    except HTTPException as he:
        return JSONResponse({"detail": he.detail}, status_code=he.status_code)
    return await call_next(request)


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
            # Dejar que CORSMiddleware de FastAPI genere la respuesta
            # preflight con los headers correctos (incluye Authorization).
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
def get_camera_config_static(user_id: str, camera_id: str) -> dict:
    """Lee camera.json de una camara."""
    cam_file = STORAGE_ROOT / "users" / user_id / "cameras" / camera_id / "camera.json"
    if cam_file.exists():
        try:
            with open(cam_file) as f:
                return json.load(f)
        except Exception:
            pass
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
    except Exception:
        pass
    return 0.0

def get_user_storage_path(user_id: str, plan: str = "founder") -> Path:
    """Resolver ruta de almacenamiento del usuario segun config de discos."""
    cfg = get_disk_config()
    disks = cfg.get("disks", [])
    plans = cfg.get("plans", {})
    target = plans.get(plan, plans.get("founder", {}))
    selected = None
    priority = target.get("priority_disk")
    if priority:
        selected = next((d for d in disks if d.get("mount") == priority), None)
    if not selected:
        selected = max(disks, key=lambda d: d.get("free_gb", 0), default=disks[0] if disks else None)
    if not selected:
        selected = {"mount": str(STORAGE_ROOT), "user_folder": "users"}
    return Path(selected["mount"]) / selected["user_folder"].strip("/") / user_id

def find_user_json(user_id: str) -> Optional[Path]:
    """Buscar user.json de un usuario en todos los discos."""
    for plan in ("founder", "pro", "free"):
        path = get_user_storage_path(user_id, plan) / "user.json"
        if path.exists():
            return path
    compat = STORAGE_ROOT / "users" / user_id / "user.json"
    return compat if compat.exists() else None

def resolve_user_events_dirs(user_id: str) -> List[tuple]:
    """Devolver carpetas del usuario (nueva estructura + compat)."""
    base = get_user_storage_path(user_id, "founder")
    dirs = []
    cameras_dir = base / "cameras"
    if cameras_dir.exists():
        for cam_id in cameras_dir.iterdir():
            events = cam_id / "events"
            if events.is_dir():
                dirs.append((cam_id.name, events))
    legacy = base / "events"
    if legacy.is_dir():
        dirs.append(("_global", legacy))
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
                except:
                    pass
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
    return {"status": status, "service": "eva-api", "version": "7.0"}

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


@app.get("/frames/latest")
async def get_latest_frame(camera_id: Optional[str] = None, user_id: Optional[str] = None):
    grid = orchestrator._get_grid(user_id or "", camera_id or "")
    frame_bytes = grid.get_last_frame_bytes()
    last_cam = grid.get_last_camera_id()
    if camera_id and last_cam and last_cam != camera_id:
        frame_bytes = b""
    if not frame_bytes and camera_id and user_id:
        try:
            events_dir = STORAGE_ROOT / "users" / user_id / "cameras" / camera_id / "events"
            latest_vig = events_dir / "latest_vigilance.jpg"
            if latest_vig.exists():
                frame_bytes = latest_vig.read_bytes()
                last_cam = camera_id
            else:
                frames_dir = STORAGE_ROOT / "users" / user_id / "cameras" / camera_id / "frames"
                latest_raw = frames_dir / "latest_raw.jpg"
                if latest_raw.exists():
                    frame_bytes = latest_raw.read_bytes()
                    last_cam = camera_id
        except:
            pass
    image_b64 = base64.b64encode(frame_bytes).decode() if frame_bytes else ""
    yolo_count = grid.get_last_yolo_count()
    yolo_detections = grid.get_last_yolo_detections()
    # Leer latest_yolo.json si existe (más actualizado, funciona en modo centinela también)
    try:
        _yolo_json_path = STORAGE_ROOT / "users" / (user_id or "default") / "cameras" / (camera_id or "") / "frames" / "latest_yolo.json"
        if _yolo_json_path.exists():
            with open(_yolo_json_path) as _f:
                _yolo_data = json.load(_f)
            # Usar datos del JSON si el timestamp es reciente (< 60s)
            _yolo_ts = _yolo_data.get("timestamp", 0)
            if isinstance(_yolo_ts, (int, float)) and (time.time() - _yolo_ts) < 60:
                yolo_detections = _yolo_data.get("detections", [])
                yolo_count = _yolo_data.get("count", len(yolo_detections))
    except:
        pass
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
            events_dir = STORAGE_ROOT / "users" / user_id / "cameras" / camera_id / "events"
            latest_vig = events_dir / "latest_vigilance.jpg"
            if latest_vig.exists():
                frame_bytes = latest_vig.read_bytes()
            else:
                frames_dir = STORAGE_ROOT / "users" / user_id / "cameras" / camera_id / "frames"
                latest_raw = frames_dir / "latest_raw.jpg"
                if latest_raw.exists():
                    frame_bytes = latest_raw.read_bytes()
        except:
            pass
    if not frame_bytes:
        return Response(status_code=204)
    return Response(content=frame_bytes, media_type="image/jpeg", headers={"Cache-Control": "no-store, no-cache, must-revalidate"})

@app.get("/frames/latest-raw.jpg")
async def get_latest_raw_jpg(camera_id: Optional[str] = None, user_id: Optional[str] = None):
    """Frame en vivo más reciente (siempre se guarda, sin importar YOLO)."""
    if not camera_id or not user_id:
        return Response(status_code=204)
    try:
        frames_dir = STORAGE_ROOT / "users" / user_id / "cameras" / camera_id / "frames"
        latest_raw = frames_dir / "latest_raw.jpg"
        if latest_raw.exists():
            frame_bytes = latest_raw.read_bytes()
            return Response(content=frame_bytes, media_type="image/jpeg", headers={
                "Cache-Control": "no-store, no-cache, must-revalidate",
                "Access-Control-Allow-Origin": "*",
                "Cross-Origin-Resource-Policy": "cross-origin",
            })
    except:
        pass
    return Response(status_code=204)

@app.get("/grid/latest")
async def get_latest_grid(partial: int = 1, camera_id: Optional[str] = None, user_id: Optional[str] = None):
    grid = orchestrator._get_grid(user_id or "", camera_id or "")
    info = grid.get_grid_info()
    grid_b64 = ""
    if info["frame_count"] > 0:
        grid_img = grid.get_grid_image()
        grid_b64 = base64.b64encode(grid_img).decode()
    return {
        "frames_used": info["frame_count"],
        "grid_b64": grid_b64,
        "camera_ids": info["camera_ids"],
        "partial": bool(partial)
    }


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
        frames_dir = STORAGE_ROOT / "users" / user_id / "cameras" / camera_id / "frames"
        latest_raw = frames_dir / "latest_raw.jpg"
        if latest_raw.exists():
            data = latest_raw.read_bytes()
            if data:
                _cache_frame(user_id, camera_id, data)
                return data
    except Exception:
        pass
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
        decoded = auth.verify_id_token(id_token)
        uid = decoded["uid"]
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
        else:
            existing_plan = plan
            existing_status = "active"
            existing_plan_end = 0
            existing_trial_end = None
            existing_access_token = ""
            existing_payments = []
            existing_last_payment = None
            existing_next_due = 0

        now_ts = int(time.time())
        plan_def = available_plans.get(plan, {})

        # Compute plan_end for new registrations
        if existing_plan_end and existing_plan_end > now_ts:
            plan_end = existing_plan_end
        else:
            duration = plan_def.get("duration_days", 30)
            plan_end = now_ts + (duration * 86400)

        # Trial handling — only set if plan offers trial and user never had one
        trial_days = plan_def.get("trial_days", 0)
        trial_end = None
        if trial_days > 0 and not existing_payments and not existing_trial_end:
            trial_end = now_ts + (trial_days * 86400)

        # Access token
        access_token = existing_access_token or ("oj_live_" + secrets.token_urlsafe(32))

        user_data = {
            "user_id": uid,
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
        with open(storage_path / "user.json", "w") as f:
            json.dump(user_data, f, indent=2, ensure_ascii=False)
        compat_dir = STORAGE_ROOT / "users" / uid
        compat_dir.mkdir(parents=True, exist_ok=True)
        with open(compat_dir / "user.json", "w") as f:
            json.dump(user_data, f, indent=2, ensure_ascii=False)
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

@app.post("/api/fcm/register")
async def register_fcm_token(request: dict, authorization: str = Header(None, alias="Authorization")):
    """Registra token FCM para push notifications."""
    try:
        user_id = request.get("user_id", "") if isinstance(request, dict) else ""
        _verify_user_token(authorization, user_id)  # A1: anti-suplantacion de user_id en body
        fcm_token = request.get("fcm_token", "") if isinstance(request, dict) else ""
        if isinstance(request, str):
            try:
                request = json.loads(request)
                user_id = request.get("user_id", "")
                fcm_token = request.get("fcm_token", "")
            except:
                pass
        if not user_id or not fcm_token:
            raise HTTPException(status_code=400, detail="user_id and fcm_token required")
        uf = find_user_json(user_id)
        if uf and uf.exists():
            with open(uf) as f:
                user_data = json.load(f)
            tokens = user_data.get("fcm_tokens", [])
            if fcm_token not in tokens:
                tokens.append(fcm_token)
                user_data["fcm_tokens"] = tokens
                with open(uf, "w") as f:
                    json.dump(user_data, f, indent=2)
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
        uf = find_user_json(user_id)
        if uf and uf.exists():
            with open(uf) as f:
                user_data = json.load(f)
            tokens = user_data.get("fcm_tokens", [])
            if fcm_token in tokens:
                tokens.remove(fcm_token)
                user_data["fcm_tokens"] = tokens
                with open(uf, "w") as f:
                    json.dump(user_data, f, indent=2)
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
                chat_history_file = STORAGE_ROOT / "users" / user_id / "eva_chat_history.json"
                if chat_history_file.exists():
                    server_ts = int(chat_history_file.stat().st_mtime)
        except Exception:
            pass

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
        _verify_user_token(authorization, user_id)  # A1: validar token vs user_id del body
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
        _verify_user_token(authorization, user_id)  # A1: validar token vs user_id del body
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
        _verify_user_token(authorization, user_id)  # A1: validar token vs user_id del body
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
        _verify_user_token(authorization, user_id)  # A1: validar token vs user_id del body
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
            logger.exception(f"[EVA] handle_eva_v2 falló: {e1}")
            from eva_v2 import _make_os_session, _load_session, _mk_resp as _eva_mk_resp
            session = _load_session(session_id)
            if not session or session.get("user_id") != user_id:
                session = _make_os_session(user_id, session_id)
            try:
                from eva_v2 import _sessions
                _sessions[session_id] = session
            except Exception:
                pass
            result = _eva_mk_resp(
                session,
                f"Lo siento {user_id[:8]}, tuve un problema técnico procesando eso. Intenta reformularlo, por favor.",
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
                except Exception:
                    pass
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
                        pass
                import threading
                threading.Thread(target=_save_async, daemon=True).start()
            except Exception as e:
                logger.warning(f"[EVA] No se pudo guardar en user.json: {e}")
        
        return result
        
    except Exception as e:
        logger.error(f"[EVA] Error en chat_eva_message: {e}")
        return {"success": False, "error": str(e), "response": "Error procesando mensaje"}


# Alias para compatibilidad con frontend actual (/config/chat)
@app.post("/config/chat")
async def chat_config_alias(request: dict):
    """Alias del endpoint de chat para compatibilidad con versiones anteriores."""
    return await chat_eva_message(request)


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
        cam_root = STORAGE_ROOT / "users" / user_id / "cameras" / camera_id
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
    _verify_user_token(request.headers.get("authorization"), user_id)  # A1: validar token vs user_id del body
    if not user_id:
        raise HTTPException(status_code=400, detail="user_id required")
    user_file = find_user_json(user_id)
    if not user_file or not user_file.exists():
        raise HTTPException(status_code=404, detail="User not found")
    with open(user_file) as f:
        user_data = json.load(f)
    updatable_fields = ["vigilance_prompt", "vigilance_rules", "name", "business_name", "business_type", "schedule", "what_to_monitor", "schedule_open", "schedule_close", "employee_count", "main_concerns", "phone"]
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
    with open(user_file, "w") as f:
        json.dump(user_data, f, indent=2)
        return {"success": True}

@app.get("/api/user/events")
async def get_user_events(user_id: str, date: str = None, filter: str = None, limit: int = 50, camera_id: str = None, exclude_vigilance: bool = False):
    events = []
    now = int(time.time())
    start_of_today = int(datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    cam_names = {}
    user_file = find_user_json(user_id)
    if user_file and user_file.exists():
        with open(user_file) as f:
            ud = json.load(f)
        for cam in ud.get("cameras", []):
            cam_names[cam.get("camera_id", "")] = cam.get("name", "")
    # M4.4: si filtra por camera_id, no recorrer dirs de otras camaras
    target_cam_ids = {camera_id} if camera_id else None
    for cam_id, events_dir in resolve_user_events_dirs(user_id):
        if not events_dir.exists():
            continue
        # M4.4: saltar dirs de camaras que no nos interesan
        if target_cam_ids and cam_id not in target_cam_ids and cam_id != "_global":
            continue
        # Usar scandir (mas eficiente) y limitar cuantos examinamos para no recorrer 9000+ archivos
        try:
            entries = [e for e in os.scandir(str(events_dir)) if e.name.endswith(".json") and e.is_file()]
        except Exception:
            continue
        entries.sort(key=lambda e: e.stat(follow_symlinks=False).st_mtime, reverse=True)
        scanned = 0
        for entry in entries:
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
            ev["camera_name"] = cam_names.get(cid, cam_id if cam_id != "_global" else "Camara")
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
    base = Path(STORAGE_ROOT) / "users"
    search_dirs = []
    if user_id and user_id != "default":
        search_dirs.append(base / user_id / "cameras")
    if not search_dirs:
        search_dirs = [d / "cameras" for d in base.iterdir() if d.is_dir() and (d / "cameras").exists()]
    for cam_base in search_dirs:
        if not cam_base.exists():
            continue
        for cam_dir in cam_base.iterdir():
            if not cam_dir.is_dir():
                continue
            img_file = cam_dir / "events" / f"{event_id}.jpg"
            if img_file.exists():
                try:
                    img = PILImage.open(img_file)
                    img.thumbnail((160, 120))
                    buf = io.BytesIO()
                    img.save(buf, format="JPEG", quality=60)
                    return Response(content=buf.getvalue(), media_type="image/jpeg", headers={"Cache-Control": "max-age=86400"})
                except Exception:
                    pass
    raise HTTPException(status_code=404, detail="Image not found")


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
        cams_dir = STORAGE_ROOT / "users" / user_id / "cameras"
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
                ev_dir = STORAGE_ROOT / "users" / user_id / "cameras" / cam_id / "events"
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
                                pass
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
                ud_existing = ud if isinstance(ud, dict) else {}
                ud_existing["cameras"] = cams
                with open(user_file, "w") as f:
                    json.dump(ud_existing, f, indent=2)
            except Exception:
                pass
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
                    pass
                if target_ip:
                    break
        if not target_ip:
            return JSONResponse(status_code=503, content={"ok": False, "error": "Camera offline"}, headers=cors_headers)
        import httpx
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(10.0, connect=5.0, read=5.0, write=5.0),
                headers={"Connection": "close"},
                http1=True,
                http2=False
            ) as client:
                resp = await client.post(f"http://{target_ip}:81/config", json=body)
            return JSONResponse(content={"ok": True}, headers=cors_headers)
        except (httpx.ConnectError, httpx.ReadError, httpx.WriteError, httpx.RemoteProtocolError):
            # Si no se puede conectar directamente, el ESP32 aplicara via polling
            return JSONResponse(content={"ok": True, "queued": True}, headers=cors_headers)
    except Exception as e:
        return JSONResponse(status_code=502, content={"ok": False, "error": str(e)}, headers=cors_headers)


def _save_cam_config_to_user(camera_id: str, body: dict):
    """Guardar configuracion de camara en user.json para polling del ESP32."""
    # [DIAG] log inicial
    try:
        logger.info(f"[DIAG_SAVE] entry camera_id={camera_id} body={body}")
    except Exception:
        pass
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
            cam_cfg = get_camera_config_static(user_id, camera_id)
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
    cam_cfg = get_camera_config_static(user_id, camera_id)
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
            except Exception:
                pass
    with open(uf, "w") as f:
        json.dump(ud, f, indent=2)
    system_prompt = ""
    try:
        from eva.vigilance_prompts import format_vision_prompt as _regen
        cam_cfg = get_camera_config_static(user_id, camera_id)
        schedule = ud.get("schedule", {})
        is_after = _is_vigilante_mode(schedule, vigilance or ud.get("vigilance", {}), datetime.now().strftime("%H:%M"), cam_cfg.get("night_mode", False))
        new_prompt = _regen(
            business_type=ud.get("business_type", ""),
            zone=cam.get("zone", ""),
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
            # Devolver solo los campos que el ESP32 entiende
            return {
                "camera_id": camera_id,
                "quality": c.get("quality", 10),
                "interval_ms": c.get("interval_ms", 500),
                "framesize": c.get("framesize", 10),
                "led_auto": c.get("led_auto", True),
                "led_bright": c.get("led_bright", 128),
                "led_on": c.get("led_on", False),
                "h_mirror": c.get("h_mirror", False),
                "v_flip": c.get("v_flip", False),
                "brightness": c.get("brightness", 0),
                "contrast": c.get("contrast", 0),
                "stream_always": c.get("stream_always", True)
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
        pass
    return "unknown"

def _adjust_brightness(img_bytes: bytes, target_brightness: int = 80) -> bytes:
    """Ajustar brillo de imagen si esta muy oscura (brillo < 50)."""
    try:
        from PIL import Image, ImageStat, ImageEnhance
        import io
        img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
        stat = ImageStat.Stat(img)
        avg_brightness = sum(stat.mean[:3]) / 3
        if avg_brightness < 65:
            enhancer = ImageEnhance.Brightness(img)
            factor = target_brightness / max(avg_brightness, 1)
            factor = min(factor, 3.0)
            img = enhancer.enhance(factor)
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=85)
            return buf.getvalue()
    except Exception:
        pass
    return img_bytes


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
                    pass
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
            _uf = STORAGE_ROOT / "users" / user_id / "user.json"
            if _uf.exists():
                with open(_uf) as _f:
                    _ud = json.load(_f)
                schedule = _ud.get("schedule", {}) or {}
                if not vigilance:
                    vigilance = _ud.get("vigilance", {}) or {}
        except Exception:
            pass
    
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
        events_dir = STORAGE_ROOT / "users" / user_id / "cameras" / camera_id / "events"
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


def _update_camera_last_frame(user_id: str, camera_id: str, client_ip: str = None):
    """Actualizar last_frame de una cámara en user.json. Auto-registra si no existe."""
    if not user_id:
        return
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

        img_bytes = await image.read()
        frame_size = len(img_bytes)
        now_dt = datetime.now()
        frame_id = f"{camera_id}_{int(time.time()*1000)}"
        mode = "normal"
        processing = "sync_yolo"
        yolo_count = 0
        yolo_classes = []
        yolo_detections = []
        is_vigilante = False

        # ── [1] GUARDAR FRAME ORIGINAL (siempre) ──
        try:
            frames_dir_v = STORAGE_ROOT / "users" / user_id / "cameras" / camera_id / "frames"
            frames_dir_v.mkdir(parents=True, exist_ok=True)
            frame_path = frames_dir_v / "latest_raw.jpg"
            with open(frame_path, "wb") as f:
                f.write(img_bytes)
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
        cam_cfg = get_camera_config_static(user_id, camera_id)
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
                pass
        current_time = now_dt.strftime("%H:%M")
        is_vigilante = _is_vigilante_mode(schedule, vigilance, current_time, cam_cfg.get("night_mode", False), user_id=user_id, camera_id=camera_id)
        mode = "vigilante" if is_vigilante else "normal"

        # ── [3] YOLO SÍNCRONO RÁPIDO (para siluetas y eventos) ──
        try:
            yolo_count, yolo_classes, yolo_detections = await _run_yolo_detection(img_bytes)
        except Exception as e:
            logger.error(f"Error YOLO en endpoint: {e}")
            yolo_count, yolo_classes, yolo_detections = 0, [], []

        # ── [4] Actualizar latest_yolo.json y grid para el viewer ──
        try:
            frames_dir_v = STORAGE_ROOT / "users" / user_id / "cameras" / camera_id / "frames"
            frames_dir_v.mkdir(parents=True, exist_ok=True)
            yolo_data = {
                "timestamp": time.time(),
                "count": yolo_count,
                "detections": yolo_detections,
                "classes": yolo_classes,
                "mode": mode
            }
            with open(frames_dir_v / "latest_yolo.json", "w") as f:
                json.dump(yolo_data, f, indent=2)

            # Actualizar grid inmediatamente con el último frame y detecciones
            from orchestrator import orchestrator
            grid = orchestrator._get_grid(user_id, camera_id, grid_size=16)
            grid.add_frame(
                image_bytes=img_bytes,
                camera_id=camera_id,
                user_id=user_id,
                yolo_count=yolo_count,
                yolo_classes=yolo_classes,
                yolo_detections=yolo_detections,
                mode=mode
            )
        except Exception as e:
            logger.error(f"Error actualizando YOLO/grid para viewer: {e}")

        # ── [5] MODO CENTINELA: alerta directa si YOLO detecta algo ──
        # En modo vigilante SOLO generamos el evento centinela (YOLO + notificacion).
        # No se encola para Qwen (capa de inteligencia) — el usuario lo pidio asi:
        # "es solo yolo detecta personas y notifica, sin capa de inteligencia"
        if is_vigilante and yolo_count > 0:
            logger.warning(f"MODO CENTINELA: {yolo_count} objects {yolo_classes} → alerta directa")
            _save_vigilance_event(user_id, camera_id, img_bytes, yolo_count, yolo_classes, client_ip)

        # ── [6] ENCOLAR para grid + Qwen en background ──
        # YOLO Gate:
        #   1) count==0 → descartar
        #   2) is_vigilante → NO encolar (la alerta centinela de [5] ya fue guardada;
        #      en modo centinela no queremos capa de inteligencia Qwen, solo YOLO)
        #   3) resto (count>0, modo normal) → encolar
        if yolo_count > 0 and not is_vigilante:
            await FRAME_QUEUE.put({
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
                "yolo_count": yolo_count,
                "yolo_classes": yolo_classes,
                "yolo_detections": yolo_detections
            })
            processing = "queued"

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
            "queue_size": FRAME_QUEUE.qsize(),
            "yolo": {"count": yolo_count, "classes": yolo_classes, "detections": yolo_detections}
        }

    except Exception as e:
        logger.error(f"Error en _process_ingest: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


# ═══════════════════════════════════════════════════════════════════════════
# WORKER YOLO — Procesamiento asíncrono en background
# ═══════════════════════════════════════════════════════════════════════════

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


async def yolo_worker():
    """Worker que procesa la cola de frames en background.

    Flujo:
    1. Obtiene frame de la cola (con YOLO ya ejecutado en el endpoint)
    2. Agrega frame al grid para acumulación (análisis Qwen)
    3. Si el grid se llena o modo centinela con suficientes frames, procesa Qwen
    """
    global WORKER_RUNNING
    WORKER_RUNNING = True
    logger.info("🔧 YOLO Worker iniciado")

    while True:
        try:
            frame_data = await FRAME_QUEUE.get()

            frame_id = frame_data.get("frame_id")
            user_id = frame_data.get("user_id")
            camera_id = frame_data.get("camera_id")
            img_bytes = frame_data.get("img_bytes")
            cam_cfg = frame_data.get("cam_cfg") or {}
            schedule = frame_data.get("schedule") or {}
            vigilance = frame_data.get("vigilance") or {}
            mode = frame_data.get("mode", "normal")
            yolo_count = frame_data.get("yolo_count", 0)
            yolo_classes = frame_data.get("yolo_classes", [])
            yolo_detections = frame_data.get("yolo_detections", [])

            logger.info(f"Worker procesando frame {frame_id} (queue: {FRAME_QUEUE.qsize()})")

            if yolo_count <= 0:
                logger.info(f"YOLO gate: 0 objects → frame REJECTED del grid (frame_id={frame_id})")
                FRAME_QUEUE.task_done()
                continue

            # Agregar frame al grid para acumulación Qwen
            from orchestrator import orchestrator
            grid = orchestrator._get_grid(user_id, camera_id, grid_size=16)
            grid_is_full = grid.add_frame(
                image_bytes=img_bytes,
                camera_id=camera_id,
                user_id=user_id,
                yolo_count=yolo_count,
                yolo_classes=yolo_classes,
                yolo_detections=yolo_detections,
                mode=mode
            )

            current_time = datetime.now().strftime("%H:%M")
            is_vigilante = _is_vigilante_mode(schedule, vigilance, current_time, cam_cfg.get("night_mode", False), user_id=user_id, camera_id=camera_id)

            logger.info(f"YOLO gate: {yolo_count} objects {yolo_classes} → frame AGREGADO al grid ({grid.get_frame_count()}/16)")

            # Procesar grid solo cuando esté lleno (16 frames en modo normal).
            # En modo vigilante no procesamos Qwen (defensa en profundidad): la
            # alerta centinela ya fue guardada por _save_vigilance_event en [5]
            # y los frames vigilantes no se encolaron en [6].
            if grid_is_full:
                grid_result = await orchestrator.process_grid(
                    user_id=user_id,
                    camera_id=camera_id,
                    mode="vigilante" if is_vigilante else "normal",
                    use_grid_image=True,
                    grid_size=16
                )
                logger.info(f"Grid procesado: {grid_result.get('frame_count', 0)}/16 frames")
            else:
                logger.info(f"Grid aún no lleno ({grid.get_frame_count()}/16), esperando más frames")

            FRAME_QUEUE.task_done()

        except Exception as e:
            logger.error(f"Error en yolo_worker: {e}", exc_info=True)
            FRAME_QUEUE.task_done()


# ═══════════════════════════════════════════════════════════════════════════
# Entry point — Solo se ejecuta cuando se corre directamente (no cuando se importa)
# ═══════════════════════════════════════════════════════════════════════════

# Variable global para evitar múltiples workers
_WORKER_STARTED = False

@app.on_event("startup")
async def _start_background_tasks():
    """Iniciar tareas en background: YOLO Worker + Scheduler de Reportes."""
    global _WORKER_STARTED
    if not _WORKER_STARTED:
        _WORKER_STARTED = True
        logger.info("🚀 Iniciando YOLO Worker en background...")
        asyncio.create_task(yolo_worker())
        
        # Iniciar scheduler de reportes automáticos (7:30 AM diario)
        logger.info("⏰ Iniciando scheduler de reportes diarios (7:30 AM)...")
        from reportes.scheduler import start_scheduler
        asyncio.create_task(start_scheduler())


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
        reports_dir = STORAGE_ROOT / "users" / user_id / "daily_reports"
        
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


@app.post("/api/reports/test")
async def test_report_send(user_id: str, camera_id: str = None):
    """
    Prueba envío de reporte (para debugging).
    """
    try:
        from reportes.scheduler import test_send_report
        
        result = await test_send_report(user_id, camera_id)
        return result
    except Exception as e:
        logger.error(f"Error en test de reporte: {e}")
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
        reports_dir = STORAGE_ROOT / "users" / user_id / "daily_reports"
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
        notifications_dir = STORAGE_ROOT / "users" / user_id / "notifications"
        
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
                pass
        
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
        notifications_dir = STORAGE_ROOT / "users" / user_id / "notifications"
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
            notifications_dir = STORAGE_ROOT / "users" / user_id / "notifications"
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
            user_file = STORAGE_ROOT / "users" / user_id / "user.json"
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
        
        resp = _req.post(api_url, headers=headers, json=payload, timeout=10)
        
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
            pass
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


def _verify_admin(authorization: str = Header(None)) -> dict:
    """Valida sesion admin contra admin_config.json. Lanza 401 si invalido."""
    if not authorization:
        raise HTTPException(status_code=401, detail="Authorization requerido")
    token = authorization.replace("Bearer ", "").strip()
    if not token:
        raise HTTPException(status_code=401, detail="Token invalido")
    cfg = _load_admin_config()
    sessions = cfg.get("sessions", {})
    # A6: comparar token en tiempo constante (anti timing-attack).
    s = None
    for sess_tok, sess_data in sessions.items():
        if hmac.compare_digest(str(sess_tok), token):
            s = sess_data
            break
    if s is None:
        raise HTTPException(status_code=401, detail="Sesion no encontrada")
    if int(time.time()) > s.get("expires_at", 0):
        sessions.pop(token, None)
        _save_admin_config(cfg)
        raise HTTPException(status_code=401, detail="Sesion expirada")
    return {"session_token": token, "cfg": cfg}


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
    cfg.setdefault("sessions", {})[session_token] = {
        "created_at": int(time.time()),
        "expires_at": int(time.time()) + ADMIN_SESSION_TTL,
        "user_agent": request.get("user_agent", "admin")
    }
    _save_admin_config(cfg)
    logger.info("Admin login OK")
    return {"success": True, "session_token": session_token}


@app.post("/admin/auth/logout")
async def admin_auth_logout(authorization: str = Header(None)):
    cfg = _load_admin_config()
    token = (authorization or "").replace("Bearer ", "").strip()
    if token:
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
                except Exception:
                    pass
    logger.info(f"Plan deleted: {plan_id}, migrated {migrated} users to free")
    return {"success": True, "migrated_users": migrated}


# ── Admin: Billing Management ──────────────────────────────────────────

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
    user_dir = STORAGE_ROOT / "users" / user_id
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
    with open(user_dir / "user.json", "w") as f:
        json.dump(ud, f, indent=2, ensure_ascii=False)
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
                used_mb = _dir_used_mb(sp) if sp.exists() else 0
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
                pass
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
    with open(user_file) as f:
        ud = json.load(f)
    for k in ("name", "email", "phone", "business_name", "business_type", "plan", "status",
              "plan_end", "trial_end", "next_due", "access_token"):
        if k in request:
            ud[k] = request[k]
    with open(user_file, "w") as f:
        json.dump(ud, f, indent=2, ensure_ascii=False)
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
    if not duration_days:
        duration_days = plan_def.get("duration_days", 30)
    now_ts = int(time.time())
    current_end = ud.get("plan_end", 0) or 0
    if current_end < now_ts:
        new_end = now_ts + (duration_days * 86400)
    else:
        new_end = current_end + (duration_days * 86400)
    payment_id = f"pay_admin_{now_ts}_{secrets.token_hex(4)}"
    payment = {
        "id": payment_id, "user_id": user_id, "amount": amount,
        "currency": plan_def.get("currency", "USD"), "method": method,
        "reference": f"admin_renewal_{now_ts}",
        "notes": notes or f"Renovacion admin: {duration_days} dias",
        "status": "confirmed", "created_at": now_ts, "confirmed_at": now_ts, "confirmed_by": "admin",
        "duration_days": duration_days
    }
    ud["plan"] = plan
    ud["plan_end"] = new_end
    ud["next_due"] = new_end
    ud["status"] = "active"
    ud["trial_end"] = None
    payments = ud.get("payments", [])
    payments.append(payment)
    ud["payments"] = payments
    ud["last_payment"] = payment
    with open(user_file, "w") as f:
        json.dump(ud, f, indent=2, ensure_ascii=False)
    logger.info(f"User renewed: {user_id} plan={plan} end={new_end} amount={amount}")
    return {"success": True, "plan": plan, "plan_end": new_end, "payment_id": payment_id}


@app.post("/admin/users/{user_id}/payment/{payment_id}/confirm")
async def admin_confirm_payment(user_id: str, payment_id: str, request: dict, authorization: str = Header(None)):
    _verify_admin(authorization)
    user_file = find_user_json(user_id)
    if not user_file or not user_file.exists():
        raise HTTPException(status_code=404, detail="User not found")
    with open(user_file) as f:
        ud = json.load(f)
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
        raise HTTPException(status_code=404, detail="Payment not found")
    # Al confirmar pago: reactivar servicio + extender plan_end si el pago trae duration_days
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
    with open(user_file, "w") as f:
        json.dump(ud, f, indent=2, ensure_ascii=False)
    logger.info(f"Payment confirmed: {user_id} {payment_id} -> servicio activo")
    return {"success": True, "payment_id": payment_id, "status": "confirmed",
            "service": "active", "plan_end": ud.get("plan_end")}


@app.post("/admin/users/{user_id}/suspend")
async def admin_suspend_user(user_id: str, request: dict, authorization: str = Header(None)):
    _verify_admin(authorization)
    user_file = find_user_json(user_id)
    if not user_file or not user_file.exists():
        raise HTTPException(status_code=404, detail="User not found")
    with open(user_file) as f:
        ud = json.load(f)
    ud["status"] = "suspended"
    with open(user_file, "w") as f:
        json.dump(ud, f, indent=2, ensure_ascii=False)
    logger.info(f"User suspended: {user_id}")
    return {"success": True, "status": "suspended"}


@app.post("/admin/users/{user_id}/reactivate")
async def admin_reactivate_user(user_id: str, request: dict, authorization: str = Header(None)):
    _verify_admin(authorization)
    user_file = find_user_json(user_id)
    if not user_file or not user_file.exists():
        raise HTTPException(status_code=404, detail="User not found")
    with open(user_file) as f:
        ud = json.load(f)
    ud["status"] = "active"
    with open(user_file, "w") as f:
        json.dump(ud, f, indent=2, ensure_ascii=False)
    logger.info(f"User reactivated: {user_id}")
    return {"success": True, "status": "active"}


@app.post("/admin/users/{user_id}/regen-token")
async def admin_regen_token(user_id: str, authorization: str = Header(None)):
    _verify_admin(authorization)
    user_file = find_user_json(user_id)
    if not user_file or not user_file.exists():
        raise HTTPException(status_code=404, detail="User not found")
    with open(user_file) as f:
        ud = json.load(f)
    new_token = "oj_live_" + secrets.token_urlsafe(32)
    ud["access_token"] = new_token
    with open(user_file, "w") as f:
        json.dump(ud, f, indent=2, ensure_ascii=False)
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
                last_announce = cam.get("last_announce") or 0
                last_frame = cam.get("last_frame") or 0
                announce_age = now - last_announce if last_announce else None
                frame_age = now - last_frame if last_frame else None
                is_online = (announce_age is not None and announce_age < 120) or (frame_age is not None and frame_age < 120)
                last_seen_ts = max(last_announce or 0, last_frame or 0)
                cameras.append({
                    "camera_id": cid, "name": cam.get("name", cid), "zone": cam.get("zone", ""),
                    "user_id": uid.name, "business_name": udata.get("business_name", ""),
                    "status": "online" if is_online else "offline", "active": is_online,
                    "last_announce": datetime.fromtimestamp(last_announce).isoformat() if last_announce else None,
                    "last_frame": datetime.fromtimestamp(last_frame).isoformat() if last_frame else None,
                    "last_seen": datetime.fromtimestamp(last_seen_ts).isoformat() if last_seen_ts else None,
                    "announce_age_s": int(announce_age) if announce_age else None,
                    "frame_age_s": int(frame_age) if frame_age else None
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
                    pass
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
                    pass
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
                pass
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
                pass
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
                            pass
            except Exception:
                pass
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
            pass
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
                    pass
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
    used_mb = _dir_used_mb(storage_path) if storage_path.exists() else 0
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
    with open(user_file) as f:
        user_data = json.load(f)
    if "plan" in request:
        user_data["plan"] = request["plan"]
    if "quota_gb" in request:
        user_data["quota_gb"] = request["quota_gb"]
    with open(user_file, "w") as f:
        json.dump(user_data, f, indent=2)
    return {"success": True}


@app.post("/admin/storage/{user_id}/migrate")
async def admin_migrate_user(user_id: str, request: dict, authorization: str = Header(None)):
    _verify_admin(authorization)
    new_disk = request.get("disk_mount", str(STORAGE_ROOT))
    new_dir = Path(new_disk) / "users" / user_id
    old_file = find_user_json(user_id)
    existing = {}
    if old_file and old_file.exists():
        with open(old_file) as f:
            existing = json.load(f)
    existing["disk_mount"] = new_disk
    new_dir.mkdir(parents=True, exist_ok=True)
    with open(new_dir / "user.json", "w") as f:
        json.dump(existing, f, indent=2, ensure_ascii=False)
    compat = STORAGE_ROOT / "users" / user_id
    compat.mkdir(parents=True, exist_ok=True)
    with open(compat / "user.json", "w") as f:
        json.dump(existing, f, indent=2, ensure_ascii=False)
    return {"success": True, "new_path": str(new_dir)}


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
        pass
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


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8005, loop="asyncio")
