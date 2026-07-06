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
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Optional, Dict, List, Any

from fastapi import FastAPI, UploadFile, File, HTTPException, Form, Request, Header
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

# Configuracion Global
STORAGE_ROOT = Path("/home/sam/storage")
DISKS_CONFIG_FILE = STORAGE_ROOT / "disks_config.json"
EVA_CONFIG_FILE = STORAGE_ROOT / "eva_config.json"
FIREBASE_KEY_PATH = Path("/home/sam/Downloads/firebase-key.json")

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

# CORS middleware — permisivo para todos los orígenes
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"],
    max_age=86400,
)

# Static files for event images
app.mount("/events", StaticFiles(directory=str(STORAGE_ROOT / "users")), name="events-static")

# Middleware para no-cache y CORS seguro
@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    try:
        if request.method == "OPTIONS":
            return Response(
                status_code=200,
                headers={
                    "Access-Control-Allow-Origin": "*",
                    "Access-Control-Allow-Methods": "GET, POST, PUT, DELETE, OPTIONS",
                    "Access-Control-Allow-Headers": "*",
                    "Access-Control-Max-Age": "86400",
                }
            )
        response = await call_next(request)
        response.headers["Access-Control-Allow-Origin"] = "*"
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
            headers={"Access-Control-Allow-Origin": "*"}
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

    async def generate():
        last_frame_bytes = None
        first_frame_sent = False
        repeats = 0

        while True:
            try:
                frame_bytes = _read_latest_frame_bytes(user_id, camera_id)

                if frame_bytes:
                    if not first_frame_sent or frame_bytes != last_frame_bytes:
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
                    else:
                        repeats += 1
                        if repeats >= 3:
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

# Perfil y Eventos de Usuario
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
async def get_user_events(user_id: str, date: str = None, filter: str = None, limit: int = 50):
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
    for cam_id, events_dir in resolve_user_events_dirs(user_id):
        if not events_dir.exists():
            continue
        for fname in sorted(events_dir.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
            if not fname.name.endswith(".json"):
                continue
            if len(events) >= limit:
                break
            with open(fname) as f:
                ev = json.load(f)
            if date == "hoy" or filter == "today":
                if ev.get("timestamp", 0) < start_of_today:
                    continue
            if filter == "alerts" and ev.get("event_type") != "violation":
                continue
            cid = ev.get("camera_id", "")
            ev["camera_name"] = cam_names.get(cid, cam_id if cam_id != "_global" else "Camara")
            if ev.get("event_type") == "violation":
                rule_violated = ev.get("metadata", {}).get("rule_violated", "")
                qa = rule_violated if rule_violated else ev.get("metadata", {}).get("qwen_analysis", "")[:60]
            else:
                qa = ev.get("metadata", {}).get("qwen_analysis", "")[:100]
            is_violation = ev.get("event_type") in ("violation", "vigilance_alert", "night_alert")
            ev["qwen"] = {"violation": is_violation, "description": qa}
            ev["yolo"] = {"count": 1}
            if "metadata" in ev and "grid_b64" in ev["metadata"]:
                del ev["metadata"]["grid_b64"]
            img_path = events_dir / f"{ev.get('event_id', '')}.jpg"
            if img_path.exists():
                try:
                    from gateway_resize import resize_image
                    with open(img_path, "rb") as f:
                        img_bytes = f.read()
                    resized = resize_image(img_bytes, max_size=128)
                    ev["frame_b64"] = base64.b64encode(resized).decode()
                except Exception as e:
                    pass
            ev["thumb_url"] = f"/api/event-thumb/{ev.get('event_id', '')}?user_id={user_id}"
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

@app.get("/api/events/{event_id}")
async def get_event_detail(event_id: str, user_id: str):
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
    # Contar frames disponibles para el carrusel
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
        cams = ud.get("cameras", [])
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
    try:
        users_dir = STORAGE_ROOT / "users"
        if not users_dir.is_dir():
            return
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
                        with open(uf, "w") as f:
                            json.dump(ud, f, indent=2)
                        return
            except Exception:
                continue
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


async def _process_ingest(request: Request, camera_id: str, user_id: str, image: UploadFile):
    """Flujo completo: ESP32 -> YOLO gate -> Grid -> Qwen -> Evento"""
    try:
        client_ip = request.client.host if request.client else "unknown"
        user_id = resolve_user_id(camera_id, user_id, client_ip)
        if camera_id == "unknown":
            camera_id = await _resolve_unknown_camera(user_id, client_ip)

        img_bytes = await image.read()
        frame_size = len(img_bytes)
        now_dt = datetime.now()

        # Guardar frame ORIGINAL (sin watermark) para el viewer
        try:
            frames_dir_v = STORAGE_ROOT / "users" / user_id / "cameras" / camera_id / "frames"
            frames_dir_v.mkdir(parents=True, exist_ok=True)
            with open(frames_dir_v / "latest_raw.jpg", "wb") as f:
                f.write(img_bytes)
            # Cache en RAM para MJPEG stream (sin I/O de disco)
            _cache_frame(user_id, camera_id, img_bytes)
        except Exception:
            pass

        # ── WATERMARK: Agregar marca de agua SOLO para análisis Qwen ──
        ts_str = now_dt.strftime("%Y-%m-%dT%H:%M:%S")
        img_bytes = add_frame_watermark(img_bytes, camera_id, ts_str, business_name="")

        logger.info(f"Frame: IP={client_ip} Cam={camera_id} User={user_id} Size={frame_size}B")

        # 1. Leer config de la cámara
        cam_cfg = get_camera_config_static(user_id, camera_id)
        yolo_triggers = cam_cfg.get("yolo_triggers", ["person"])
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

        # 2. Determinar modo: normal o centinela
        current_time = now_dt.strftime("%H:%M")
        is_vigilante = _is_vigilante_mode(schedule, vigilance, current_time, cam_cfg.get("night_mode", False))
        mode = "vigilante" if is_vigilante else "normal"
        is_after_hours = is_vigilante
        logger.info(f"Mode: {'VIGILANTE' if is_vigilante else 'NORMAL'} | Time: {current_time}")

        # 3. Ajustar brillo para YOLO (siempre, para que detecte mejor)
        yolo_bytes = _adjust_brightness(img_bytes)

        # 4. YOLO detection - detectar CUALQUIER objeto con conf >= 0.25
        yolo_count = 0
        yolo_classes = []
        yolo_detections = []  # Lista completa para visualización
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                yolo_resp = await client.post(
                    "http://localhost:8002/detect",
                    files={"image": ("frame.jpg", yolo_bytes, "image/jpeg")},
                )
                if yolo_resp.status_code == 200:
                    yolo_data = yolo_resp.json()
                    # Aceptar TODOS los objetos con conf >= 0.25 (no solo "person")
                    for d in yolo_data.get("detections", []):
                        if d.get("confidence", 0) >= 0.25:
                            yolo_classes.append(d.get("class", ""))
                            yolo_detections.append(d)
                    # Conteo inteligente: usar track_id para personas únicas
                    unique_track_ids = set(d.get("track_id") for d in yolo_detections if d.get("track_id"))
                    yolo_count = len(unique_track_ids) if unique_track_ids else len(yolo_detections)
                    # Metadata de tracking en español
                    tracking_info = _build_tracking_info(yolo_detections)
                    logger.warning(f"YOLO_DEBUG: raw_count={yolo_data.get('count')} filtered_count={yolo_count} classes={yolo_classes} unique_tracks={len(unique_track_ids)} tracking={tracking_info}")
        except Exception as e:
            logger.warning(f"YOLO unavailable: {e}")

        # Guardar detecciones YOLO para el frontend (siempre, cualquier modo)
        try:
            _frames_dir = STORAGE_ROOT / "users" / user_id / "cameras" / camera_id / "frames"
            _frames_dir.mkdir(parents=True, exist_ok=True)
            yolo_json_path = _frames_dir / "latest_yolo.json"
            yolo_json_data = {
                "timestamp": time.time(),
                "count": yolo_count,
                "detections": yolo_detections,
                "mode": "vigilante" if is_vigilante else "normal"
            }
            with open(yolo_json_path, "w") as f:
                json.dump(yolo_json_data, f)
        except Exception:
            pass

        # 4. YOLO GATE: si no hay objetos, NO pasar al grid
        grid_result = {"frame_count": 0, "grid_full": False, "ready_for_analysis": False}
        
        if yolo_count == 0:
            # YOLO no detectó nada - frame NO va al grid
            logger.info(f"YOLO gate: 0 objects → frame REJECTED (not added to grid)")
            _update_camera_last_frame(user_id, camera_id, client_ip)
            return {
                "success": True, "camera_id": camera_id, "user_id": user_id,
                "client_ip": client_ip, "frame_size": frame_size,
                "mode": "vigilante" if is_vigilante else "normal", "timestamp": now_dt.isoformat(),
                "yolo": {"count": 0, "classes": [], "detections": []},
                **grid_result
            }

        # 4b. MODO CENTINELA: fuera de horario + detección = alerta directa sin grid
        if is_vigilante:
            logger.warning(f"MODO CENTINELA: {yolo_count} objects {yolo_classes} → alerta directa")
            # Guardar frame como latest para el viewer (actualización en vivo)
            try:
                events_dir = STORAGE_ROOT / "users" / user_id / "cameras" / camera_id / "events"
                events_dir.mkdir(parents=True, exist_ok=True)
                with open(events_dir / "latest_vigilance.jpg", "wb") as f:
                    f.write(img_bytes)
            except Exception:
                pass
            # Guardar evento de vigilancia y notificar FCM
            _save_vigilance_event(user_id, camera_id, img_bytes, yolo_count, yolo_classes, client_ip)
            _update_camera_last_frame(user_id, camera_id, client_ip)
            return {
                "success": True, "camera_id": camera_id, "user_id": user_id,
                "client_ip": client_ip, "frame_size": frame_size,
                "mode": "vigilante", "timestamp": now_dt.isoformat(),
                "yolo": {"count": yolo_count, "classes": yolo_classes, "detections": yolo_detections},
                **grid_result
            }

        logger.info(f"YOLO gate: {yolo_count} objects {yolo_classes} → frame ACCEPTED to grid")

        # 5. Usar la misma imagen ajustada para el grid (Qwen necesita imagen clara)
        grid_bytes = yolo_bytes

        # 6. Agregar frame al grid
        zone_priority = "normal"
        burst_mode = False
        if yolo_count > 0:
            zone_priority = _get_zone_priority(cam_cfg, yolo_detections)
            burst_mode = (zone_priority == "critical")
            if burst_mode:
                logger.info(f"BURST MODE: critical zone → fast grid fill")

        # 7. Preparar prompt para Qwen
        v_prompt, v_rules = "", ""
        uf = find_user_json(user_id)
        if uf and uf.exists():
            with open(uf) as f:
                ud = json.load(f)
            v_prompt = cam_cfg.get("system_prompt", ud.get("vigilance_prompt", ""))
            v_rules_raw = cam_cfg.get("rules", ud.get("vigilance_rules", []))
            v_rules = v_rules_raw if isinstance(v_rules_raw, str) else "\n".join(v_rules_raw) if v_rules_raw else ""

        grid_result = orchestrator.add_frame(
            grid_bytes, camera_id, user_id,
            yolo_count=yolo_count, yolo_detections=yolo_detections,
            vigilance_prompt=v_prompt,
            vigilance_rules=v_rules, burst_mode=burst_mode
        )
        logger.info(f"Grid: {grid_result['frame_count']}/16 | YOLO:{yolo_count} | zone:{zone_priority}")

        # 8. Si el grid está lleno, Qwen lo analiza
        if grid_result.get("grid_full"):
            logger.warning(f"GRID FULL! Triggering Qwen analysis for {camera_id}")
            try:
                import asyncio
                qwen_result = await asyncio.wait_for(
                    orchestrator.analyze_grid_and_save_event(
                        user_id=user_id,
                        camera_id=camera_id,
                        vigilance_prompt=v_prompt,
                        vigilance_rules=v_rules,
                        business_name=ud.get("business_name", ""),
                        business_type=ud.get("business_type", ""),
                        schedule_open=cam_cfg.get("schedule_open", ""),
                        schedule_close=cam_cfg.get("schedule_close", ""),
                        mode=mode,
                        is_after_hours=is_after_hours,
                    ),
                    timeout=60 # Timeout de 60 segundos para el análisis completo
                )
                logger.info(f"Qwen analysis done: violation={qwen_result.get('violation', False)} event_id={qwen_result.get('event_id','')}")
            except Exception as e:
                import traceback
                logger.error(f"Qwen analysis failed: {e}")
                logger.error(f"Qwen analysis traceback: {traceback.format_exc()}")
        else:
            pass  # Grid not full yet, keep filling

        # 9. Guardar frame para Eva
        try:
            from eva.eva_chat import ingest_frame_for_eva
            ingest_frame_for_eva(img_bytes)
        except Exception as e:
            logger.debug(f"eva frame buffer: {e}")

        # 10. Actualizar timestamp de la cámara
        _update_camera_last_frame(user_id, camera_id, client_ip)

        return {
            "success": True,
            "camera_id": camera_id,
            "user_id": user_id,
            "client_ip": client_ip,
            "frame_size": frame_size,
            "mode": "normal",
            "timestamp": now_dt.isoformat(),
            "yolo": {
                "count": yolo_count,
                "classes": yolo_classes,
                "detections": [{"class": d.get("class",""), "confidence": d.get("confidence",0), "bbox": d.get("bbox",[])} for d in yolo_detections]
            },
            **grid_result
        }
    except Exception as e:
        logger.error(f"Ingest error: {e}")
        return {"success": False, "error": str(e)}

# ── Delta Mode & Background Objects ──────────────────────────────────────

# In-memory background objects per camera: {camera_id: {class_name: {bbox, last_seen_frame}}}
_background_objects: dict = {}

# Cooldown tracking: {camera_id: last_alert_timestamp}
_alert_cooldowns: dict = {}


def _build_tracking_info(yolo_detections: list) -> dict:
    """Construir metadata de tracking en español para las detecciones.
    
    Returns:
        dict con información de tracking en español:
        - total_personas: número total de detecciones
        - personas_unicas: número de track_ids únicos
        - personas_estables: count de track_stable=True
        - alturas: lista de alturas estimadas
        - actividades: lista de actividades inferidas
    """
    if not yolo_detections:
        return {"total_personas": 0, "personas_unicas": 0}
    
    track_ids = set()
    estables = 0
    alturas = []
    actividades = []
    
    for d in yolo_detections:
        # Track ID
        tid = d.get("track_id")
        if tid:
            track_ids.add(tid)
        
        # Estabilidad
        if d.get("track_stable"):
            estables += 1
        
        # Altura estimada desde pose
        pose = d.get("pose", {})
        altura = _estimate_height_from_pose(pose, d.get("bbox", []))
        if altura:
            alturas.append(altura)
        
        # Actividad inferida
        actividad = _infer_activity_from_pose(pose)
        if actividad:
            actividades.append(actividad)
    
    return {
        "total_personas": len(yolo_detections),
        "personas_unicas": len(track_ids),
        "personas_estables": estables,
        "alturas_estimadas": alturas,
        "actividades": list(set(actividades))
    }


def _estimate_height_from_pose(pose: dict, bbox: list) -> str:
    """Estimar altura de persona desde keypoints de pose."""
    if not pose or not bbox or len(bbox) != 4:
        return "desconocida"
    
    keypoints = pose.get("keypoints", [])
    if not keypoints or len(keypoints) < 17:
        return "desconocida"
    
    try:
        # Usar vertical_span como proxy de altura
        vertical_span = pose.get("vertical_span", 0)
        
        if vertical_span > 0.6:
            return "alta"
        elif vertical_span > 0.4:
            return "media"
        else:
            return "baja"
    except Exception:
        return "desconocida"


def _infer_activity_from_pose(pose: dict) -> str:
    """Inferir actividad desde métricas de pose."""
    if not pose:
        return "desconocida"
    
    try:
        vertical_span = pose.get("vertical_span", 0)
        visible = pose.get("visible", 0)
        has_pose = pose.get("has_pose", False)
        
        if not has_pose:
            return "desconocida"
        
        if vertical_span > 0.5 and visible > 6:
            return "de_pie"
        elif vertical_span < 0.3 and visible < 6:
            return "sentado"
        elif visible > 6:
            return "moviendo_manos"
        else:
            return "quieto"
    except Exception:
        return "desconocida"


def _get_background_objects(camera_id: str) -> dict:
    """Get background objects for a camera."""
    return _background_objects.get(camera_id, {})


def _update_background_objects(camera_id: str, yolo_detections: list, frame_count: int):
    """Update background objects with current detections."""
    if camera_id not in _background_objects:
        _background_objects[camera_id] = {}
    bg = _background_objects[camera_id]
    for det in yolo_detections:
        cls = det.get("class", "")
        bbox = tuple(det.get("bbox", []))
        conf = det.get("confidence", 0)
        if conf > 0.35 and cls:
            bg[cls] = {"bbox": bbox, "last_seen": frame_count, "confidence": conf}


def _is_new_or_moved(camera_id: str, yolo_detections: list, frame_count: int, threshold_frames: int = 30) -> tuple:
    """
    Check if objects are new or moved compared to background.
    Returns: (is_delta, relevant_detections)
    """
    bg = _get_background_objects(camera_id)
    new_or_moved = []
    for det in yolo_detections:
        cls = det.get("class", "")
        bbox = det.get("bbox", [])
        conf = det.get("confidence", 0)
        if conf < 0.35 or not cls:
            continue
        if cls not in bg:
            new_or_moved.append(det)
            continue
        # Check if moved significantly (>20% bbox shift)
        old_bbox = bg[cls].get("bbox", [])
        if old_bbox and bbox and len(old_bbox) >= 4 and len(bbox) >= 4:
            dx = abs(bbox[0] - old_bbox[0]) / max(old_bbox[2] - old_bbox[0], 1)
            dy = abs(bbox[1] - old_bbox[1]) / max(old_bbox[3] - old_bbox[1], 1)
            if dx > 0.2 or dy > 0.2:
                new_or_moved.append(det)
                continue
        # Check if disappeared and reappeared
        last_seen = bg[cls].get("last_seen", 0)
        if frame_count - last_seen > threshold_frames:
            new_or_moved.append(det)
    return (len(new_or_moved) > 0, new_or_moved)


def _check_alert_cooldown(camera_id: str, cooldown_minutes: int = 5) -> bool:
    """Check if camera is in cooldown. Returns True if can alert."""
    import time as _time
    now = _time.time()
    last_alert = _alert_cooldowns.get(camera_id, 0)
    if now - last_alert < cooldown_minutes * 60:
        return False
    _alert_cooldowns[camera_id] = now
    return True


def _get_zone_priority(cam_cfg: dict, yolo_detections: list) -> str:
    """Check if detected objects are in high-priority zones. Returns: critical | medium | low"""
    zones = cam_cfg.get("zones", {})
    if not zones or not yolo_detections:
        return "low"
    for det in yolo_detections:
        bbox = det.get("bbox", [])
        if not bbox or len(bbox) < 4:
            continue
        obj_cx = (bbox[0] + bbox[2]) / 2
        obj_cy = (bbox[1] + bbox[3]) / 2
        for zone_name, zone_cfg in zones.items():
            coords = zone_cfg.get("coords", [])
            if len(coords) == 4:
                zx1, zy1, zx2, zy2 = coords
                if zx1 <= obj_cx <= zx2 and zy1 <= obj_cy <= zy2:
                    priority = zone_cfg.get("priority", "low")
                    if priority == "critical":
                        return "critical"
                    elif priority == "medium":
                        return "medium"
    return "low"


def _is_vigilante_mode(schedule: dict, vigilance: dict, current_time: str, night_mode: bool = False) -> bool:
    """Determinar si la cámara está en modo centinela (fuera de horario laboral + gracia)."""
    if not vigilance.get("enabled", False):
        if not night_mode:
            return False
    open_t = schedule.get("open", "07:00")
    close_t = schedule.get("close", "19:00")
    grace_min = vigilance.get("grace_minutes", 15)
    try:
        def to_min(t: str) -> int:
            parts = t.split(":")[:2]
            return int(parts[0]) * 60 + int(parts[1])
        cur = to_min(current_time)
        opn = to_min(open_t)
        cls_close = to_min(close_t)
        # Minuto en que inicia el modo centinela (cierre + gracia)
        centinela_start = cls_close + grace_min
        # Caso 1: NO cruza medianoche (ej: cierre 20:00 + 15min = 20:15)
        if centinela_start < 1440:
            if cur < opn or cur > centinela_start:
                return True
            return False
        # Caso 2: CRUZA medianoche (ej: cierre 23:59 + 15min = 1454 = 00:14 dia sig)
        else:
            real_centinel_start = centinela_start - 1440  # 14 = 00:14
            # Normal: dentro del horario laboral
            if cur >= opn and cur <= cls_close:
                return False
            # Gracia: después de medianoche pero Antes del inicio real de centinela
            # Usa <= para incluir el último minuto de gracia
            if cur <= real_centinel_start:
                return False
            # Centinela: antes de abrir o después de la gracia
            return True
    except:
        return False


_vigilance_cooldowns = {}  # {user_id_camera_id: last_alert_timestamp}

def _save_vigilance_event(user_id: str, camera_id: str, img_bytes: bytes, yolo_count: int, yolo_classes: list, client_ip: str):
    """Guardar evento de vigilancia (modo centinela) y notificar FCM. Usa cooldown_min de la config."""
    global _vigilance_cooldowns
    now = int(time.time())
    cam_key = f"{user_id}_{camera_id}"
    try:
        _cam_cfg = get_camera_config(user_id, camera_id)
        _cooldown_sec = int(_cam_cfg.get("cooldown_min", 60))
    except:
        _cooldown_sec = 60
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
        # Guardar metadata
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
        # Notificar FCM
        _send_vigilance_fcm(user_id, camera_id, event_id, yolo_count, yolo_classes)
    except Exception as e:
        logger.error(f"Error saving vigilance event: {e}")


def _send_vigilance_fcm(user_id: str, camera_id: str, event_id: str, yolo_count: int, yolo_classes: list):
    """Enviar notificación FCM de alerta de vigilancia."""
    try:
        from orchestrator import orchestrator
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
        for token in tokens[:3]:
            import asyncio
            asyncio.create_task(send_fcm_notification(
                title=title,
                body=body,
                token=token,
                user_id=user_id,
                link=f"https://ojoia.com.do/#events?event={event_id}"
            ))
        logger.info(f"Vigilance FCM queued to {len(tokens)} tokens")
    except Exception as e:
        logger.error(f"Error sending vigilance FCM: {e}")


def _update_camera_last_frame(user_id: str, camera_id: str, client_ip: str = None):
    """Actualizar last_frame de una cámara en user.json."""
    if not user_id:
        return
    try:
        uf = find_user_json(user_id)
        if uf and uf.exists():
            with open(uf) as f:
                ud = json.load(f)
            for c in ud.get("cameras", []):
                if c.get("camera_id") == camera_id:
                    c["last_frame"] = int(time.time())
                    # Actualizar IP si es diferente del servidor (10.0.0.44) y no es localhost
                    if client_ip and client_ip != "10.0.0.44" and client_ip != "127.0.0.1":
                        c["last_announce_ip"] = client_ip
                        c["last_announce"] = int(time.time())
                    break
            with open(uf, "w") as f:
                json.dump(ud, f, indent=2)
    except Exception as e:
        logger.error(f"Error updating camera last_frame: {e}")


def _resolve_user_id_from_camera(camera_id: str) -> str:
    """Buscar el user_id al que pertenece una cámara escaneando todos los user.json."""
    users_dir = STORAGE_ROOT / "users"
    if not users_dir.is_dir():
        return ""
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
                    return user_dir.name
        except:
            continue
    return ""


# Anuncio de Dispositivo
@app.post("/devices/announce")
async def device_announce(request: dict = None):
    cors_headers = {"Access-Control-Allow-Origin": "*"}
    if not request:
        request = {}
    camera_id = request.get("camera_id", "unknown")
    user_id = request.get("user_id", "")
    # IP real del ESP32: prioridad al campo "ip" del payload (el firmware lo envía)
    # Porque el announce viene por Cloudflare Tunnel y client_ip es el del servidor
    client_ip = request.get("ip", "") or request.get("client_ip", "") or ""
    if not camera_id:
        raise HTTPException(status_code=400, detail="camera_id required")
        logger.info(f"Device announce: Cam={camera_id} User={user_id} IP={client_ip}")

    # Si no viene user_id, resolver desde el camera_id (el firmware no lo envía)
    if not user_id or user_id == "":
        user_id = _resolve_user_id_from_camera(camera_id)

    # Update last_announce timestamp in user.json
    if user_id and user_id != "":
        uf = find_user_json(user_id)
        if uf and uf.exists():
            with open(uf) as f:
                ud = json.load(f)
            camera_found = False
            for c in ud.get("cameras", []):
                if c.get("camera_id") == camera_id:
                    now = int(time.time())
                    c["last_announce"] = now
                    c["last_announce_ip"] = client_ip
                    # Asegurar que first_seen exista (compatibilidad con cámaras antiguas)
                    if "first_seen" not in c:
                        c["first_seen"] = now
                    # Guardar interval_ms y quality si vienen en el announce
                    if request.get("interval_ms"):
                        c["interval_ms"] = request.get("interval_ms")
                    if request.get("quality"):
                        c["quality"] = request.get("quality")
                    camera_found = True
                    logger.info(f"Updated last_announce for {camera_id} IP={client_ip}")
                    break
            if not camera_found:
                # Camera not in list, add it
                logger.info(f"Adding new camera {camera_id} to user {user_id}")
                now = int(time.time())
                ud.setdefault("cameras", []).append({
                    "camera_id": camera_id,
                    "name": camera_id,
                    "zone": "",
                    "active": True,
                    "first_seen": now,
                    "last_announce": now,
                    "last_announce_ip": client_ip,
                    "last_frame": 0,
                    "interval_ms": request.get("interval_ms") or 0,
                    "quality": request.get("quality") or 0
                })
            uf.parent.mkdir(parents=True, exist_ok=True)
            with open(uf, "w") as f:
                json.dump(ud, f, indent=2)
        else:
            logger.warning(f"User file not found for {user_id}")
    
    image_b64 = ""
    grid = orchestrator._get_grid(user_id or "", camera_id)
    frame_bytes = grid.get_last_frame_bytes()
    last_cam = grid.get_last_camera_id()
    if frame_bytes and (last_cam == camera_id or not user_id):
        try:
            resized = resize_image(frame_bytes, max_size=256)
            image_b64 = base64.b64encode(resized).decode()
        except:
            image_b64 = base64.b64encode(frame_bytes).decode()
    if user_id and user_id in eva_sessions:
        s = eva_sessions[user_id]
        s["camera_id"] = camera_id
        s["camera_image_b64"] = image_b64
        s["camera_connected"] = True
        s["phase"] = "esperando_imagen"
        eva_sessions[user_id] = s
    return {
        "success": True,
        "message": f"Camera {camera_id} connected",
        "camera_id": camera_id,
        "image_b64": image_b64,
        "frame_available": bool(frame_bytes)
    }
    return {
        "success": True,
        "message": f"Camera {camera_id} connected",
        "camera_id": camera_id,
        "image_b64": image_b64,
        "frame_available": bool(frame_bytes)
    }


# ── Eva Chat v10 — Deterministic State Machine ────────────────────────────
from eva.eva_chat import handle_eva_chat, set_orchestrator

set_orchestrator(orchestrator)

@app.post("/config/chat")
async def config_chat(request: dict):
    try:
        logger = logging.getLogger(__name__)
        result = await handle_eva_chat(
        user_id=request.get("user_id", ""),
        message=request.get("message", "hola"),
        session_id=request.get("session_id", ""),
        cam_id=request.get("cam_id"),
        include_frame=request.get("include_frame", False),
        storage_root=STORAGE_ROOT,
    )
        return result
    except Exception as e:
        local_logger.error(f"[CHAT ERROR] {e}", exc_info=True)
        return {"success": False, "error": str(e)}



# ── NUEVO: Auto-config simple (sin chat) ─────────────────────────────────────

@app.post("/config/auto_config")
async def config_auto_config(request: dict):
    """
    Genera configuración de cámara automáticamente.
    Una sola llamada a Qwen. Sin chat.
    
    Body: { user_id, camera_id (opcional, para edición) }
    """
    from eva.auto_config import auto_generate_config
    from eva.eva_chat import ingest_frame_for_eva

    user_id = request.get("user_id", "")
    camera_id = request.get("camera_id", "")

    # Obtener el frame más reciente del usuario
    frame = None
    try:
        frame = orchestrator._get_grid(user_id, camera_id).get_last_frame_bytes()
    except Exception:
        pass

    if not frame:
        return {"success": False, "ready": False,
                "message": "Esperando imagen de la cámara..."}

    # Resize a 640px para el análisis
    try:
        from eva.eva_chat import _resize
        b64 = _resize(frame, 640)
    except Exception:
        import base64
        b64 = base64.b64encode(frame).decode()

    config = await auto_generate_config(user_id, b64, camera_id, STORAGE_ROOT)
    config["success"] = True
    config["ready"] = True
    config["image_b64"] = b64
    return config


@app.post("/config/camera_confirm")
async def config_camera_confirm(request: dict):
    """
    Usuario confirma (con o sin ediciones) la configuración.
    Guarda camera.json y activa vigilancia.
    
    Body: { user_id, camera_id, zone, rules_es, rules_en, 
            scanner_question, system_prompt, schedule, yolo_triggers, grid_size }
    """
    from eva.camera_builder import save_camera_config

    user_id = request.get("user_id", "")
    camera_id = request.get("camera_id", f"cam_{int(time.time())}")

    config = {
        "camera_id":         camera_id,
        "name":              f"Cámara {request.get('zone', 'Principal')}",
        "zone":              request.get("zone", "zona principal"),
        "scanner_question":  request.get("scanner_question", ""),
        "rules":             request.get("rules_en", []),
        "rules_es":          request.get("rules_es", []),
        "system_prompt":     request.get("system_prompt", ""),
        "yolo_triggers":     request.get("yolo_triggers", ["person"]),
        "schedule":          request.get("schedule", {"open": "07:00", "close": "19:00"}),
        "night_mode":        True,
        "grid_size":         request.get("grid_size", 12),
        "cooldown_min":      5,
        "active":            True,
    }

    saved = save_camera_config(user_id, config, STORAGE_ROOT)
    return {"success": saved, "camera_id": camera_id}


@app.post("/config/confirm")
async def config_confirm(request: dict):
    from eva.eva_chat import _sessions as _eva_s
    sess = _eva_s.get(request.get("session_id", ""), {})
    if sess.get("phase") == "done":
        return {"success": True, "message": "Cámara ya configurada"}
    if request.get("user_id", "") in eva_sessions:
        eva_sessions[request.get("user_id", "")]["configured"] = True
        eva_sessions[request.get("user_id", "")]["phase"] = "completado"
        return {"success": True}

@app.get("/config/camera_status")
async def config_camera_status(user_id: str):
    from eva.eva_chat import _sessions as _eva_s
    session = None
    for sid, sess in _eva_s.items():
        if sess.get("user_id") == user_id:
            session = sess
            break
    if not session:
        session = eva_sessions.get(user_id, {})
    camera_id = session.get("camera_id", "") if session else ""
    frame_bytes = orchestrator._get_grid(user_id, camera_id).get_last_frame_bytes()
    image_b64 = ""
    if frame_bytes:
        try:
            resized = resize_image(frame_bytes, max_size=512)
            image_b64 = base64.b64encode(resized).decode()
        except:
            image_b64 = base64.b64encode(frame_bytes).decode()
    return {
        "camera_connected": session.get("camera_connected", False) if session else False,
        "camera_id": session.get("camera_id", "") if session else "",
        "has_frame": bool(frame_bytes),
        "image_b64": image_b64,
        "zone": session.get("zone", "") if session else "",
        "rules_count": len(session.get("confirmed_rules", [])) if session else 0,
        "rules": session.get("confirmed_rules", []) if session else [],
        "phase": session.get("phase", "unknown") if session else "unknown",
        "configured": session.get("phase") == "done" if session else False,
        "has_image": bool(session.get("image_b64", "")) if session else False,
    }

@app.post("/config/reset")
async def config_reset(request: dict):
    user_id = request.get("user_id", "")
    session_id = request.get("session_id", "")
    eva_sessions.pop(user_id, None)
    from eva.eva_chat import _sessions as _eva_s, destroy_session
    if session_id:
        destroy_session(session_id)
        return {"success": True, "message": "Sesion reiniciada"}

@app.get("/config/session")
async def config_session(user_id: str):
    from eva.eva_chat import _sessions as _eva_s, get_user_sessions
    user_sessions = get_user_sessions(user_id)
    if user_sessions:
        s = max(user_sessions.values(), key=lambda x: x.get("last_activity", 0))
        return {
            "active": bool(s),
            "camera_id": s.get("camera_id", ""),
            "camera_image_b64": s.get("image_b64", ""),
            "zone": s.get("zone", ""),
            "rules": s.get("confirmed_rules", []),
            "phase": s.get("phase", "unknown"),
            "rules_count": len(s.get("confirmed_rules", [])),
            "has_image": bool(s.get("image_b64", "")),
            "session_id": s.get("session_id", ""),
        }
    s = eva_sessions.get(user_id, {})
    return {
        "active": bool(s),
        "camera_id": s.get("camera_id", ""),
        "camera_image_b64": s.get("camera_image_b64", ""),
        "zone": s.get("zone", ""),
        "rules": s.get("rules", []),
        "phase": s.get("phase", "unknown"),
        "rules_count": len(s.get("rules", [])),
        "has_image": bool(s.get("camera_image_b64", "")),
        "session_id": "",
    }

# ── FCM ──
@app.post("/api/fcm/register")
async def register_fcm_token(request: dict):
    user_id = request.get("user_id", "")
    fcm_token = request.get("fcm_token", "")
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
        return {"success": True}

# ── User Plan & Billing Endpoints ───────────────────────────────────────

@app.get("/api/user/status")
async def get_user_status(user_id: str):
    """Full plan status for the frontend."""
    user_file = find_user_json(user_id)
    if not user_file or not user_file.exists():
        raise HTTPException(status_code=404, detail="User not found")
    with open(user_file) as f:
        ud = json.load(f)
    check = _plan_check(ud)
    cfg = get_disk_config()
    plan_def = cfg.get("plans", {}).get(ud.get("plan", "free"), {})
    max_cams = plan_def.get("max_cameras", 1)
    max_storage = plan_def.get("max_storage_gb", 5)
    max_rules = plan_def.get("max_rules_per_camera", 2)
    features = plan_def.get("features", {})
    camera_count = len(ud.get("cameras", []))
    # Compute storage used
    storage_path = Path(ud.get("storage_path", str(user_file.parent)))
    used_mb = 0
    if storage_path.exists():
        for root, dirs, files in os.walk(str(storage_path)):
            for fi in files:
                fp = Path(root) / fi
                if fp.is_file():
                    used_mb += fp.stat().st_size
    used_mb = round(used_mb / (1024**2), 1)
    return {
        "user_id": user_id,
        "plan": ud.get("plan", "free"),
        "plan_name": plan_def.get("name", ud.get("plan", "free")),
        "status": check["status"],
        "days_left": check["days_left"],
        "plan_end": ud.get("plan_end", 0),
        "trial_end": ud.get("trial_end"),
        "trial_days_left": check.get("trial_days_left"),
        "grace_days_left": check.get("grace_days_left"),
        "expiring_soon": check.get("expiring_soon", False),
        "allowed": check["allowed"],
        "reason": check.get("reason"),
        "limits": {
            "max_cameras": max_cams,
            "camera_count": camera_count,
            "max_storage_gb": max_storage,
            "used_mb": used_mb,
            "max_rules_per_camera": max_rules
        },
        "features": features,
        "billing": {
            "cycle": ud.get("billing_cycle", "monthly"),
            "next_due": ud.get("next_due", 0),
            "payment_count": len(ud.get("payments", [])),
            "last_payment": ud.get("last_payment"),
            "currency": plan_def.get("currency", "USD"),
            "price_monthly": plan_def.get("price_monthly", 0),
            "price_yearly": plan_def.get("price_yearly", 0)
        },
        "access_token": ud.get("access_token", "")
    }


@app.post("/api/payment/upload")
async def upload_payment(
    user_id: str = Form(...),
    amount: str = Form(""),
    method: str = Form("transfer"),
    reference: str = Form(""),
    notes: str = Form(""),
    receipt: Optional[UploadFile] = File(None)
):
    """Upload payment receipt. Creates a pending payment entry."""
    user_file = find_user_json(user_id)
    if not user_file or not user_file.exists():
        raise HTTPException(status_code=404, detail="User not found")
    with open(user_file) as f:
        ud = json.load(f)

    payment_id = f"pay_{int(time.time())}_{secrets.token_hex(4)}"
    payment = {
        "id": payment_id,
        "user_id": user_id,
        "amount": float(amount) if amount else 0,
        "currency": "USD",
        "method": method,
        "reference": reference,
        "notes": notes,
        "status": "pending",
        "created_at": int(time.time()),
        "receipt_path": None
    }

    # Save receipt image if provided
    if receipt and receipt.filename:
        receipt_dir = user_file.parent / "receipts"
        receipt_dir.mkdir(exist_ok=True)
        ext = Path(receipt.filename).suffix or ".jpg"
        receipt_path = receipt_dir / f"{payment_id}{ext}"
        content = await receipt.read()
        with open(receipt_path, "wb") as f:
            f.write(content)
        payment["receipt_path"] = str(receipt_path)
        payment["receipt_filename"] = receipt.filename

    payments = ud.get("payments", [])
    payments.append(payment)
    ud["payments"] = payments
    with open(user_file, "w") as f:
        json.dump(ud, f, indent=2, ensure_ascii=False)

        logger.info(f"Payment uploaded: {payment_id} user={user_id} amount={payment['amount']}")
        return {"success": True, "payment_id": payment_id, "status": "pending"}


@app.get("/api/user/payments")
async def get_user_payments(user_id: str):
    """Get payment history for current user."""
    user_file = find_user_json(user_id)
    if not user_file or not user_file.exists():
        raise HTTPException(status_code=404, detail="User not found")
    with open(user_file) as f:
        ud = json.load(f)
    return {"payments": ud.get("payments", []), "user_id": user_id}


@app.delete("/api/cameras/{camera_id}")
async def delete_camera(camera_id: str, user_id: str):
    uf = find_user_json(user_id)
    if not uf or not uf.exists():
        raise HTTPException(status_code=404, detail="Usuario no encontrado")
    with open(uf) as f:
        ud = json.load(f)
    cams = ud.get("cameras", [])
    new_cams = [c for c in cams if c.get("camera_id") != camera_id]
    if len(new_cams) == len(cams):
        raise HTTPException(status_code=404, detail="Camara no encontrada")
    ud["cameras"] = new_cams
    with open(uf, "w") as f:
        json.dump(ud, f, indent=2, ensure_ascii=False)
    import shutil
    cam_dir = STORAGE_ROOT / "users" / user_id / "cameras" / camera_id
    if cam_dir.exists():
        shutil.rmtree(cam_dir, ignore_errors=True)
        return {"success": True, "message": f"Camara {camera_id} eliminada"}

@app.post("/fcm/test")
async def test_fcm_notification(request: dict):
    user_id = request.get("user_id", "default")
    title = request.get("title", "Prueba - OjoIA")
    body = request.get("body", "Notificacion de prueba")
    logger.info(f"FCM test: {title} -> {user_id}: {body}")
    return {"success": True, "message": "Notificacion enviada"}

# ── Grid analyze ──
@app.post("/grid/analyze")
async def analyze_grid(request: dict):
    user_id = request.get("user_id", "default")
    camera_id = request.get("camera_id", "")
    prompt = request.get("prompt")
    vigilance_prompt = request.get("vigilance_prompt", "")
    vigilance_rules = request.get("vigilance_rules", "")
    if not vigilance_prompt or not vigilance_rules:
        user_file = find_user_json(user_id)
        if user_file and user_file.exists():
            with open(user_file) as f:
                user_data = json.load(f)
            vigilance_prompt = vigilance_prompt or user_data.get("vigilance_prompt", "")
            v_rules_raw = vigilance_rules or user_data.get("vigilance_rules", [])
            vigilance_rules = v_rules_raw if isinstance(v_rules_raw, str) else "\n".join(v_rules_raw) if v_rules_raw else ""
    if not camera_id:
        return {"success": False, "error": "camera_id required"}
    result = await orchestrator.analyze_grid_and_save_event(
        user_id=user_id, camera_id=camera_id,
        vigilance_prompt=vigilance_prompt, vigilance_rules=vigilance_rules,
    )
    return result

# ── Admin Endpoints ──

# ── Admin: Plan CRUD ───────────────────────────────────────────────────

@app.get("/admin/plans")
async def admin_list_plans():
    """List all available plans with full details."""
    cfg = get_disk_config()
    return {"plans": cfg.get("plans", {})}


@app.post("/admin/plans")
async def admin_create_plan(request: dict):
    """Create a new plan. Expects full plan definition."""
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
            "ai_analysis": True,
            "alerts_push": True,
            "grid_detection": False,
            "multi_zone": False,
            "priority_disk": "",
            "support": "community"
        }),
        "public": bool(request.get("public", True))
    }
    plans[plan_id] = plan_def
    cfg["plans"] = plans
    save_disk_config(cfg)
    logger.info(f"Plan created: {plan_id}")
    return {"success": True, "plan_id": plan_id, "plan": plan_def}


@app.put("/admin/plans/{plan_id}")
async def admin_update_plan(plan_id: str, request: dict):
    """Update an existing plan."""
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
                # Merge features
                existing["features"].update(request[field])
            else:
                existing[field] = request[field]
    plans[plan_id] = existing
    cfg["plans"] = plans
    save_disk_config(cfg)
    logger.info(f"Plan updated: {plan_id}")
    return {"success": True, "plan": existing}


@app.delete("/admin/plans/{plan_id}")
async def admin_delete_plan(plan_id: str):
    """Delete a plan. Users on this plan will be moved to 'free'."""
    cfg = get_disk_config()
    plans = cfg.get("plans", {})
    if plan_id not in plans:
        raise HTTPException(status_code=404, detail=f"Plan '{plan_id}' not found")
    if plan_id == "free":
        raise HTTPException(status_code=400, detail="Cannot delete the 'free' plan")

    del plans[plan_id]
    cfg["plans"] = plans
    save_disk_config(cfg)

    # Migrate users on this plan to 'free'
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
                except:
                    pass

        logger.info(f"Plan deleted: {plan_id}, migrated {migrated} users to free")
        return {"success": True, "migrated_users": migrated}


# ── Admin: Billing Management ──────────────────────────────────────────

@app.get("/admin/billing")
async def admin_billing_overview():
    """Global billing overview — all users with plan status."""
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
            except:
                continue

            check = _plan_check(ud)
            access_status = _compute_access_status(ud)
            plan = ud.get("plan", "free")
            plan_def = cfg.get("plans", {}).get(plan, {})

            # Count confirmed payments
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
                "user_id": uid.name,
                "name": ud.get("name", ""),
                "email": ud.get("email", ""),
                "business_name": ud.get("business_name", ""),
                "plan": plan,
                "plan_name": plan_def.get("name", plan),
                "status": ud.get("status", "active"),
                "access_status": access_status,
                "plan_end": ud.get("plan_end", 0),
                "trial_end": ud.get("trial_end"),
                "days_left": check["days_left"],
                "grace_days_left": check.get("grace_days_left"),
                "trial_days_left": check.get("trial_days_left"),
                "camera_count": len(ud.get("cameras", [])),
                "payment_count": len(confirmed_payments),
                "total_paid": total_paid,
                "last_payment": ud.get("last_payment"),
                "next_due": ud.get("next_due", 0),
                "pending_payments": len([p for p in ud.get("payments", []) if p.get("status") == "pending"]),
                "created_at": ud.get("created_at", 0)
            })

    return {
        "users": users,
        "summary": {
            "total_users": len(users),
            "active": active_count,
            "warning": warning_count,
            "expired": expired_count,
            "trial": trial_count,
            "total_revenue": round(total_revenue, 2)
        }
    }


@app.post("/admin/users/{user_id}/renew")
async def admin_renew_user(user_id: str, request: dict):
    """Renew/extend a user's plan. Admin manually confirms payment."""
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

    # If plan already expired, start from now. Otherwise extend from current end.
    if current_end < now_ts:
        new_end = now_ts + (duration_days * 86400)
    else:
        new_end = current_end + (duration_days * 86400)

    # Record payment
    payment_id = f"pay_admin_{now_ts}_{secrets.token_hex(4)}"
    payment = {
        "id": payment_id,
        "user_id": user_id,
        "amount": amount,
        "currency": plan_def.get("currency", "USD"),
        "method": method,
        "reference": f"admin_renewal_{now_ts}",
        "notes": notes or f"Renovación admin: {duration_days} días",
        "status": "confirmed",
        "created_at": now_ts,
        "confirmed_at": now_ts,
        "confirmed_by": "admin"
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
async def admin_confirm_payment(user_id: str, payment_id: str, request: dict):
    """Confirm a pending payment."""
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

    ud["payments"] = payments
    ud["last_payment"] = next((p for p in payments if p["id"] == payment_id), None)
    with open(user_file, "w") as f:
        json.dump(ud, f, indent=2, ensure_ascii=False)

        return {"success": True, "payment_id": payment_id, "status": "confirmed"}


@app.post("/admin/users/{user_id}/suspend")
async def admin_suspend_user(user_id: str, request: dict):
    """Suspend a user."""
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
async def admin_reactivate_user(user_id: str, request: dict):
    """Reactivate a suspended user."""
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
async def admin_regen_token(user_id: str):
    """Regenerate access token for a user."""
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
@app.get("/admin/cameras")
async def admin_list_cameras():
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
            except:
                continue
            for cam in udata.get("cameras", []):
                cid = cam.get("camera_id", "") or ""
                # Determine online status from last_announce or last_frame (2 min threshold)
                last_announce = cam.get("last_announce") or 0
                last_frame = cam.get("last_frame") or 0
                announce_age = now - last_announce if last_announce else None
                frame_age = now - last_frame if last_frame else None
                is_online = False
                if announce_age is not None and announce_age < 120:
                    is_online = True
                if frame_age is not None and frame_age < 120:
                    is_online = True
                # Last seen = whichever is more recent
                last_seen_ts = max(last_announce or 0, last_frame or 0)
                cameras.append({
                    "camera_id": cid,
                    "name": cam.get("name", cid),
                    "zone": cam.get("zone", ""),
                    "user_id": uid.name,
                    "business_name": udata.get("business_name", ""),
                    "status": "online" if is_online else "offline",
                    "active": is_online,
                    "last_announce": datetime.fromtimestamp(last_announce).isoformat() if last_announce else None,
                    "last_frame": datetime.fromtimestamp(last_frame).isoformat() if last_frame else None,
                    "last_seen": datetime.fromtimestamp(last_seen_ts).isoformat() if last_seen_ts else None,
                    "announce_age_s": int(announce_age) if announce_age else None,
                    "frame_age_s": int(frame_age) if frame_age else None
                })
    return {"cameras": cameras}

@app.get("/admin/events")
async def admin_list_events(limit: int = 50, user_id: str = None, event_type: str = None):
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
                    except:
                        continue
                    if event_type and ev.get("event_type") != event_type:
                        continue
                    ev["_user_id"] = uid.name
                    events.append(ev)
    events.sort(key=lambda e: e.get("timestamp", 0), reverse=True)
    return {"events": events[:limit], "total": len(events)}

@app.get("/admin/stats")
async def admin_stats():
    cfg = get_disk_config()
    events_count = 0
    violations_count = 0
    for disk in cfg.get("disks", []):
        users_base = Path(disk.get("mount", "")) / disk.get("user_folder", "users")
        if not users_base.is_dir():
            continue
        for uid in users_base.iterdir():
            for _cam_id, events_dir in resolve_user_events_dirs(uid.name):
                if events_dir.exists():
                    for f in events_dir.iterdir():
                        if f.name.endswith(".json"):
                            events_count += 1
                            try:
                                with open(f) as fh:
                                    ev = json.load(fh)
                                if ev.get("event_type") == "violation":
                                    violations_count += 1
                            except:
                                pass
    total_cameras = set()
    for disk in cfg.get("disks", []):
        users_base = Path(disk.get("mount", "")) / disk.get("user_folder", "users")
        if not users_base.is_dir():
            continue
        for uid in users_base.iterdir():
            user_file = uid / "user.json"
            if user_file.is_file():
                try:
                    with open(user_file) as fh:
                        udata = json.load(fh)
                    for cam in udata.get("cameras", []):
                        total_cameras.add(cam.get("camera_id", ""))
                except:
                    pass
    storage_used_gb = sum(d.get("used_gb", 0) or 0 for d in cfg.get("disks", []))
    total_users = 0
    for disk in cfg.get("disks", []):
        users_base = Path(disk.get("mount", "")) / disk.get("user_folder", "users")
        if users_base.is_dir():
            total_users += len(list(users_base.iterdir()))
    return {
        "total_users": total_users,
        "total_cameras": len(total_cameras),
        "active_cameras": len(orchestrator._get_grid("", "").get_grid_info()["camera_ids"]),
        "storage_used_gb": round(storage_used_gb, 2),
        "total_events": events_count,
        "total_violations": violations_count
    }

# ── Admin Endpoints (v7 addition) ──
@app.get("/admin/server/status")
async def admin_server_status():
    return {
        "status": "ok",
        "backend": "https://api.ojoia.com.do",
        "ngrok_url": "https://api.ojoia.com.do",
        "hostname": "ojoia-server",
        "local_ip": "10.0.0.44",
        "backend_port": 8005,
        "uptime_seconds": 0
    }

@app.post("/admin/server/cloudflared/save")
async def admin_cloudflared_save(request: dict):
        return {"success": True}

@app.post("/admin/server/sync-firestore")
async def admin_sync_firestore():
        return {"success": True}

@app.get("/admin/users")
async def admin_users():
    users = []
    cfg = get_disk_config()
    seen = set()
    cfg_disk = get_disk_config()
    for disk in cfg_disk.get("disks", []):
        users_base = Path(disk.get("mount", "")) / disk.get("user_folder", "users")
        if not users_base.is_dir():
            continue
        for uid in users_base.iterdir():
            if uid.name in seen:
                continue
            user_file = uid / "user.json"
            if user_file.is_file():
                try:
                    with open(user_file) as f:
                        ud = json.load(f)
                    check = _plan_check(ud)
                    access_status = _compute_access_status(ud)
                    plan_def = cfg_disk.get("plans", {}).get(ud.get("plan", "free"), {})
                    # Compute storage used
                    sp = Path(ud.get("storage_path", str(user_file.parent)))
                    used_mb = 0
                    if sp.exists():
                        for root, dirs, files in os.walk(str(sp)):
                            for fi in files:
                                fp = Path(root) / fi
                                if fp.is_file():
                                    used_mb += fp.stat().st_size
                    used_mb = round(used_mb / (1024**2), 1)
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
                    seen.add(uid.name)
                except:
                    pass
    return {"users": users}

@app.get("/admin/users/{user_id}")
async def admin_get_user(user_id: str):
    user_file = find_user_json(user_id)
    if user_file and user_file.exists():
        with open(user_file) as f:
            user_data = json.load(f)
        check = _plan_check(user_data)
        return {
            "user_id": user_id,
            "name": user_data.get("name", "-"),
            "email": user_data.get("email", "-"),
            "business_name": user_data.get("business_name", ""),
            "business_type": user_data.get("business_type", ""),
            "plan": user_data.get("plan", "free"),
            "plan_status": check["status"],
            "access_status": _compute_access_status(user_data),
            "plan_end": user_data.get("plan_end", 0),
            "trial_end": user_data.get("trial_end"),
            "days_left": check["days_left"],
            "status": user_data.get("status", "active"),
            "grace_days_left": check.get("grace_days_left"),
            "trial_days_left": check.get("trial_days_left"),
            "access_token": user_data.get("access_token", ""),
            "camera_count": len(user_data.get("cameras", [])),
            "max_cameras": _get_plan_field(user_data.get("plan", "free"), "max_cameras", 1),
            "payments": user_data.get("payments", []),
            "last_payment": user_data.get("last_payment"),
            "created_at": user_data.get("created_at", "-"),
            "cameras": user_data.get("cameras", [])
        }
    return {
        "user_id": user_id, "name": "Test User", "email": "test@example.com",
        "business_name": "Test Business", "plan": "free", "status": "active",
        "plan_status": "active", "access_status": "active", "plan_end": 0,
        "cameras": [], "payments": [], "days_left": 0
    }

@app.get("/admin/disks")
async def admin_disks():
    return get_disk_config()

@app.get("/admin/disks/autodetect")
async def admin_disks_autodetect():
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
async def admin_disks_scan():
    cfg = get_disk_config()
    for disk in cfg.get("disks", []):
        mount = disk.get("mount", "")
        if mount and Path(mount).exists():
            try:
                st = os.statvfs(mount)
                total = st.f_blocks * st.f_frsize
                free = st.f_bavail * st.f_frsize
                disk["total_gb"] = round(total / (1024**3), 1)
                disk["free_gb"] = round(free / (1024**3), 1)
                disk["used_gb"] = round((total - free) / (1024**3), 1)
            except OSError:
                pass
    save_disk_config(cfg)
    return {"success": True, "disks": cfg.get("disks", [])}

@app.post("/admin/disks/save")
async def admin_disks_save(request: dict):
    if "disks" not in request or "plans" not in request:
        raise HTTPException(status_code=400, detail="Missing fields")
    save_disk_config(request)
    return {"success": True}

@app.post("/admin/disks/add")
async def admin_disks_add(request: dict):
    cfg = get_disk_config()
    mount = request.get("mount", "")
    if not mount or mount in [d.get("mount") for d in cfg["disks"]]:
        raise HTTPException(status_code=400, detail="Mount path empty or already exists")
    cfg["disks"].append({
        "mount": mount, "label": request.get("label", mount),
        "total_gb": 0, "used_gb": 0, "free_gb": 0,
        "user_folder": request.get("user_folder", "users")
    })
    save_disk_config(cfg)
    return {"success": True}

@app.post("/admin/disks/remove")
async def admin_disks_remove(request: dict):
    cfg = get_disk_config()
    mount = request.get("mount", "")
    cfg["disks"] = [d for d in cfg["disks"] if d.get("mount") != mount]
    save_disk_config(cfg)
    return {"success": True}

@app.get("/admin/storage")
async def admin_storage_list():
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
                        "user_id": uid.name,
                        "name": ud.get("name", ""),
                        "plan": ud.get("plan", "free"),
                        "business_name": ud.get("business_name", ""),
                        "access_status": _compute_access_status(ud),
                        "days_left": check["days_left"],
                        "camera_count": len(ud.get("cameras", []))
                    })
                except:
                    pass
    return {"users": result}

@app.get("/admin/storage/{user_id}")
async def admin_get_user_storage(user_id: str):
    cfg = get_disk_config()
    plan = "free"
    user_file = find_user_json(user_id)
    ud = {}
    if user_file and user_file.exists():
        with open(user_file) as f:
            ud = json.load(f)
        plan = ud.get("plan", "free")
    storage_path = get_user_storage_path(user_id, plan)
    used_mb = 0
    if storage_path.exists():
        for root, dirs, files in os.walk(str(storage_path)):
            for fi in files:
                fp = Path(root) / fi
                if fp.is_file():
                    used_mb += fp.stat().st_size
    used_mb = round(used_mb / (1024**2), 1)
    plan_data = cfg.get("plans", {}).get(plan, {})
    return {
        "user_id": user_id,
        "disk_mount": str(storage_path.parent),
        "quota_gb": plan_data.get("max_storage_gb", 500),
        "used_mb": used_mb,
        "usage_percent": round(used_mb / (plan_data.get("max_storage_gb", 500) * 1024) * 100, 1) if plan_data.get("max_storage_gb") else 0,
        "plan": plan,
        "plan_status": _compute_access_status(ud) if ud else "active",
        "days_left": _plan_check(ud)["days_left"] if ud else 0,
        "max_cameras": plan_data.get("max_cameras", 1),
        "camera_count": len(ud.get("cameras", [])) if ud else 0
    }

@app.post("/admin/storage/{user_id}/update")
async def admin_update_user_storage(user_id: str, request: dict):
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
async def admin_migrate_user(user_id: str, request: dict):
    new_disk = request.get("disk_mount", str(STORAGE_ROOT))
    cfg = get_disk_config()
    target = next((d for d in cfg.get("disks", []) if d.get("mount") == new_disk), None)
    if not target:
        target = {"mount": new_disk, "user_folder": "users"}
    new_dir = Path(new_disk) / target.get("user_folder", "users").strip("/") / user_id
    existing = {}
    old_file = find_user_json(user_id)
    if old_file:
        with open(old_file) as f:
            existing = json.load(f)
    existing["disk_mount"] = new_disk
    new_dir.mkdir(parents=True, exist_ok=True)
    with open(new_dir / "user.json", "w") as f:
        json.dump(existing, f, indent=2)
    compat = STORAGE_ROOT / "users" / user_id
    compat.mkdir(parents=True, exist_ok=True)
    with open(compat / "user.json", "w") as f:
        json.dump(existing, f, indent=2)
        return {"success": True, "new_path": str(new_dir)}

@app.get("/admin/queue")
async def admin_queue():
    return {
        "queue_length": 0,
        "processing": 0, "done": 0, "error": 0,
        "queue_size_mb": 0, "oldest_item": None,
        "last_processed": None, "worker_running": True,
        "pending_frames": [], "grid_frames": 0,
        "grid_ready": False,
        "disabled_reason": "admin/queue requires user_id/camera_id to select grid"
    }

@app.post("/admin/queue/clear")
async def admin_clear_queue():
        return {"success": True}

@app.post("/api/cameras/{camera_id}/cooldown")
async def save_camera_cooldown(camera_id: str, request: dict):
    try:
        user_id = request.get("user_id", "")
        cooldown_min = int(request.get("cooldown_min", 5))
        if not user_id:
            return JSONResponse(status_code=400, content={"ok": False, "error": "user_id required"})
        uf = STORAGE_ROOT / "users" / user_id / "user.json"
        if not uf.exists():
            return JSONResponse(status_code=404, content={"ok": False, "error": "User not found"})
        with open(uf) as f:
            ud = json.load(f)
        for c in ud.get("cameras", []):
            if c.get("camera_id") == camera_id:
                c["cooldown_min"] = cooldown_min
                break
        with open(uf, "w") as f:
            json.dump(ud, f, indent=2)
        return {"ok": True}
    except Exception as e:
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})

@app.get("/admin/eva-config")
async def admin_eva_config():
    try:
        with open(EVA_CONFIG_FILE) as f:
            cfg = json.load(f)
    except:
        cfg = {"prompt": ""}
    return {"prompt": cfg.get("prompt", ""), "docs": cfg.get("docs", []), "violation_cooldown_min": cfg.get("violation_cooldown_min", 5)}

@app.post("/admin/system/prompts")
async def admin_save_prompt(request: dict):
    try:
        with open(EVA_CONFIG_FILE) as f:
            cfg = json.load(f)
    except:
        cfg = {}
    cfg["prompt"] = request.get("prompt", cfg.get("prompt", ""))
    if "violation_cooldown_min" in request:
        cfg["violation_cooldown_min"] = request["violation_cooldown_min"]
    with open(EVA_CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2)
        return {"success": True}

@app.get("/admin/eva-docs")
async def admin_eva_docs():
    try:
        with open(EVA_CONFIG_FILE) as f:
            cfg = json.load(f)
    except:
        cfg = {}
    return {"documents": cfg.get("docs", [])}

@app.get("/admin/eva-docs/{doc_name}")
async def admin_get_eva_doc(doc_name: str):
    try:
        with open(EVA_CONFIG_FILE) as f:
            cfg = json.load(f)
    except:
        cfg = {}
    docs = cfg.get("docs_content", {})
    return {"name": doc_name, "content": docs.get(doc_name, "# " + doc_name)}

@app.post("/admin/eva-docs/save")
async def admin_save_eva_doc(request: dict):
    try:
        with open(EVA_CONFIG_FILE) as f:
            cfg = json.load(f)
    except:
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
async def admin_calc_tokens(request: dict):
    prompt = request.get("prompt", "")
    tokens = max(1, len(prompt) // 3)
    return {"tokens": tokens}

@app.get("/admin/events/stats")
async def admin_events_stats(days: int = 7):
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
                    except:
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
    return {
        "days": days, "total_events": total, "by_day": daily,
        "by_type": dict(per_type), "top_rules": top_rules.most_common(10)
    }

# ── Device announce alias ──
@app.post("/device/announce")
async def device_announce_alias(request: dict):
    return await device_announce(request)

# ── Debug endpoints ──
@app.get("/debug/frames")
async def debug_frames():
    grids = [g.get_grid_info() for g in orchestrator.grids.values()]
    merged = {"frame_count": sum(g.get("frame_count", 0) for g in grids), "camera_ids": list({c for g in grids for c in g.get("camera_ids", [])})}
    return {"grid_status": merged, "timestamp": datetime.now().isoformat()}

# ── Endpoints faltantes (compatibilidad frontend) ──
@app.post("/api/event/{event_id}/dismiss")
async def dismiss_event(event_id: str, request: dict = None):
    user_id = (request or {}).get("user_id", "")
    for _cam_id, events_dir in resolve_user_events_dirs(user_id):
        ef = events_dir / f"{event_id}.json"
        if ef.exists():
            with open(ef) as f:
                ev = json.load(f)
            ev["dismissed"] = True
            ev["dismissed_at"] = int(time.time())
            with open(ef, "w") as f:
                json.dump(ev, f, indent=2)
            return {"success": True}
    raise HTTPException(status_code=404, detail="Evento no encontrado")

@app.post("/api/event/{event_id}/confirm")
async def confirm_threat(event_id: str, request: dict = None):
    user_id = (request or {}).get("user_id", "")
    for _cam_id, events_dir in resolve_user_events_dirs(user_id):
        ef = events_dir / f"{event_id}.json"
        if ef.exists():
            with open(ef) as f:
                ev = json.load(f)
            ev["confirmed"] = True
            ev["confirmed_at"] = int(time.time())
            with open(ef, "w") as f:
                json.dump(ev, f, indent=2)
            return {"success": True}
    raise HTTPException(status_code=404, detail="Evento no encontrado")

@app.get("/api/user/latest_analysis")
async def get_latest_analysis(user_id: str):
    latest = None
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
            ts = ev.get("timestamp", 0)
            if latest is None or ts > latest["timestamp"]:
                latest = {
                    "event_id": ev.get("event_id", ""),
                    "event_type": ev.get("event_type", ""),
                    "timestamp": ts,
                    "qwen_violation": ev.get("event_type") == "violation",
                    "qwen_description": ev.get("metadata", {}).get("qwen_analysis", ""),
                    "camera_id": ev.get("camera_id", ""),
                }
    if latest is None:
        return {"found": False, "violation": False, "description": "", "timestamp": 0}
    latest["found"] = True
    return latest

@app.get("/api/violations")
async def api_get_violations(user_id: str, limit: int = 20):
    violations = []
    for _cam_id, events_dir in resolve_user_events_dirs(user_id):
        if not events_dir.exists():
            continue
        for fname in sorted(events_dir.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
            if not fname.name.endswith(".json"):
                continue
            if len(violations) >= limit:
                break
            with open(fname) as f:
                event = json.load(f)
            if event.get("event_type") == "violation":
                violations.append(event)
    return {"violations": violations}

@app.post("/admin/firebase/update-server-status")
async def update_server_status(request: dict):
        return {"success": True, "backend": "https://api.ojoia.com.do"}


# ── Firebase Storage Queue Processor ──────────────────────────────────────

@app.get("/admin/queue/firebase/status")
async def admin_firebase_queue_status():
    """Status of the Firebase Storage frame queue."""
    try:
        from google.cloud import storage as gcs
        from firebase_admin import storage as fb_storage

        bucket_name = "ojoia-67216.firebasestorage.app"
        bucket = fb_storage.bucket(bucket_name)
        blobs = list(bucket.list_blobs(prefix="frames/", max_results=500))

        total_size = 0
        frames = []
        for b in blobs:
            total_size += b.size or 0
            # Parse path: frames/{cameraID}/{sessionID}/{timestamp}.jpg
            parts = b.name.split("/")
            camera_id = parts[1] if len(parts) > 1 else "unknown"
            session_id = parts[2] if len(parts) > 2 else "unknown"
            filename = parts[3] if len(parts) > 3 else b.name
            frames.append({
                "path": b.name,
                "camera_id": camera_id,
                "session_id": session_id,
                "filename": filename,
                "size_kb": round((b.size or 0) / 1024, 1),
                "created": b.time_created.isoformat() if b.time_created else None,
                "updated": b.updated.isoformat() if b.updated else None
            })

        return {
            "success": True,
            "queue_length": len(frames),
            "total_size_kb": round(total_size / 1024, 1),
            "frames": frames[:100],  # Limit to 100 most recent
            "bucket": bucket_name
        }
    except Exception as e:
        logger.error(f"Firebase queue status error: {e}")
        return {"success": False, "error": str(e), "queue_length": 0, "frames": []}


@app.post("/admin/queue/firebase/process")
async def admin_process_firebase_queue():
    """Process pending frames from Firebase Storage."""
    try:
        from google.cloud import storage as gcs
        from firebase_admin import storage as fb_storage
        import tempfile, os

        bucket_name = "ojoia-67216.firebasestorage.app"
        bucket = fb_storage.bucket(bucket_name)
        blobs = list(bucket.list_blobs(prefix="frames/", max_results=50))

        if not blobs:
            return {"success": True, "processed": 0, "message": "No frames in queue"}

        results = []
        for blob in blobs:
            parts = blob.name.split("/")
            camera_id = parts[1] if len(parts) > 1 else "unknown"

            # Download frame to temp file
            with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
                tmp_path = tmp.name

            try:
                blob.download_to_filename(tmp_path)
                with open(tmp_path, "rb") as f:
                    img_bytes = f.read()

                # Resolve user from camera_id
                user_id = resolve_user_id(camera_id, "default", "127.0.0.1")

                # Process through normal pipeline
                yolo_count = 0
                try:
                    async with httpx.AsyncClient(timeout=10) as client:
                        yolo_resp = await client.post(
                            "http://localhost:8002/detect",
                            files={"image": ("frame.jpg", img_bytes, "image/jpeg")},
                        )
                        if yolo_resp.status_code == 200:
                            yolo_data = yolo_resp.json()
                            yolo_count = yolo_data.get("count", 0)
                except Exception as yolo_err:
                    logger.warning(f"YOLO error for {camera_id}: {yolo_err}")

                # Get user file for Qwen
                uf = find_user_json(user_id)
                user_data = {}
                if uf and uf.exists():
                    with open(uf) as f:
                        user_data = json.load(f)

                # Update user's camera last_frame
                if uf and uf.exists():
                    with open(uf) as f:
                        ud = json.load(f)
                    for c in ud.get("cameras", []):
                        if c.get("camera_id") == camera_id:
                            c["last_frame"] = int(time.time())
                            break
                    with open(uf, "w") as fw:
                        json.dump(ud, fw, indent=2, ensure_ascii=False)

                # Delete from Firebase Storage after processing
                blob.delete()

                results.append({
                    "camera_id": camera_id,
                    "path": blob.name,
                    "size_kb": round(len(img_bytes) / 1024, 1),
                    "yolo_count": yolo_count,
                    "status": "processed"
                })
                logger.info(f"Processed frame: {camera_id} from Firebase queue")

            except Exception as frame_err:
                logger.error(f"Error processing frame {blob.name}: {frame_err}")
                results.append({
                    "camera_id": camera_id,
                    "path": blob.name,
                    "status": "error",
                    "error": str(frame_err)
                })
            finally:
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)

        processed = len([r for r in results if r["status"] == "processed"])
        errors = len([r for r in results if r["status"] == "error"])

        return {
            "success": True,
            "processed": processed,
            "errors": errors,
            "results": results
        }
    except Exception as e:
        logger.error(f"Firebase queue process error: {e}")
        return {"success": False, "error": str(e), "processed": 0}


@app.post("/admin/queue/firebase/clear")
async def admin_clear_firebase_queue():
    """Delete all pending frames from Firebase Storage."""
    try:
        from firebase_admin import storage as fb_storage

        bucket_name = "ojoia-67216.firebasestorage.app"
        bucket = fb_storage.bucket(bucket_name)
        blobs = list(bucket.list_blobs(prefix="frames/"))

        deleted = 0
        for blob in blobs:
            blob.delete()
            deleted += 1

        return {"success": True, "deleted": deleted}
    except Exception as e:
        return {"success": False, "error": str(e)}


@app.get("/admin/queue/status")
async def admin_queue_status():
    """Full queue status: local + Firebase."""
    frame_count = sum(g.get_frame_count() for g in orchestrator.grids.values())

    # Check Firebase queue
    fb_queue_length = 0
    fb_total_kb = 0
    try:
        from firebase_admin import storage as fb_storage
        bucket = fb_storage.bucket("ojoia-67216.firebasestorage.app")
        blobs = list(bucket.list_blobs(prefix="frames/", max_results=500))
        fb_queue_length = len(blobs)
        fb_total_kb = round(sum(b.size or 0 for b in blobs) / 1024, 1)
    except:
        pass

    return {
        "queue_length": frame_count,
        "processing": 0,
        "done": 0,
        "error": 0,
        "queue_size_mb": 0,
        "oldest_item": None,
        "last_processed": None,
        "worker_running": True,
        "pending_frames": [],
        "grid_frames": frame_count,
        "grid_ready": frame_count >= 16,
        "firebase_queue": {
            "pending_frames": fb_queue_length,
            "total_kb": fb_total_kb,
            "bucket": "ojoia-67216.firebasestorage.app"
        }
    }

@app.get("/api/daily-summary/{user_id}")
async def get_daily_summary(user_id: str, date_str: str = None):
    from eva.daily_summary import load_summary, generate_daily_summary
    target_date = date_str or (date.today() - timedelta(days=1)).isoformat()
    summary = load_summary(user_id, target_date)
    if not summary or not summary.get("totals", {}).get("events"):
        summary = await generate_daily_summary(user_id, target_date)
        return {"success": True, "summary": summary}

# ═══════════════════════════════════════════════════════════════════════════
#  SDXL Switch — Cambiar entre Turbo y JuggernautXL
# ═══════════════════════════════════════════════════════════════════════════
from fastapi import APIRouter
import subprocess

sdxl_router = APIRouter()

@sdxl_router.get("/admin/sdxl/status")
async def sdxl_status():
    """Estado actual de SDXL"""
    import torch
    gpu1_used = 0
    try:
        result = subprocess.run(['nvidia-smi', '-i', '1', '--query-gpu=memory.used', '--format=noheader'], 
                              capture_output=True, text=True, timeout=5)
        gpu1_used = int(result.stdout.strip().replace(' MiB', ''))
    except:
        pass
    
    pid = subprocess.run(['pgrep', '-f', 'StableDiffusionXLPipeline'], 
                        capture_output=True, text=True, timeout=5)
    running = pid.returncode == 0
    
    # Detectar qué modelo está cargado
    model = "desconocido"
    if running:
        log_check = subprocess.run(['tail', '-5', '/home/sam/ai_system/logs/sdxl_boot.log'],
                                  capture_output=True, text=True, timeout=5)
        if 'Juggernaut' in log_check.stdout or 'juggernaut' in log_check.stdout:
            model = "JuggernautXL_v10"
        else:
            model = "SDXL Turbo"
    
    return {
        "running": running,
        "model": model,
        "gpu1_used_mb": gpu1_used,
        "switch_command": "GET /admin/sdxl/switch?model=turbo|juggernaut"
    }

@sdxl_router.get("/admin/sdxl/switch")
async def sdxl_switch(model: str = "turbo"):
    """Cambiar modelo SDXL: turbo o juggernaut"""
    if model not in ["turbo", "juggernaut"]:
        return {"error": "modelo debe ser 'turbo' o 'juggernaut'"}, 400
    
        result = subprocess.run(
        ['/home/sam/ai_system/scripts/switch_sdxl.sh', model],
        capture_output=True, text=True, timeout=120
    )
    
    return {
        "status": "switching" if result.returncode == 0 else "error",
        "model": model,
        "output": result.stdout[-500:] if result.stdout else result.stderr[-500:]
    }

# Registrar router
app.include_router(sdxl_router)


# ═══════════════════════════════════════════════════════════════════════════
# EVA CHAT OS — Endpoints del chat con Eva
# ═══════════════════════════════════════════════════════════════════════════

from eva.eva_chat_os import handle_eva_chat_os, load_business_json, save_business_json


class EvaChatRequest(BaseModel):
    message: str
    user_id: str
    session_id: Optional[str] = None
    history: Optional[list] = None


class EvaFeedbackRequest(BaseModel):
    user_id: str
    event_id: str
    is_real: bool
    notes: Optional[str] = None


@app.post("/api/chat/eva")
async def eva_chat_endpoint(request: EvaChatRequest):
    """Endpoint principal del chat con Eva."""
    try:
        result = await handle_eva_chat_os(
            user_id=request.user_id,
            message=request.message,
            session_id=request.session_id,
            history=request.history or []
        )
        return result
    except Exception as e:
        logger.error(f"Error en Eva chat: {e}")
        return {"success": False, "error": str(e), "response": "Disculpa, tuve un problema. Intenta de nuevo."}


@app.post("/api/chat/eva/feedback")
async def eva_feedback_endpoint(request: EvaFeedbackRequest):
    """Endpoint para dar feedback sobre alertas."""
    try:
        from eva.tools import tool_learn_from_feedback
        result = await tool_learn_from_feedback(
            user_id=request.user_id,
            event_id=request.event_id,
            is_real=request.is_real,
            notes=request.notes
        )
        return result
    except Exception as e:
        logger.error(f"Error en feedback: {e}")
        return {"success": False, "error": str(e)}


@app.get("/api/event-frame/{event_id}")
async def get_event_frame(event_id: str, user_id: str):
    """Sirve el frame (imagen) de un evento específico."""
    from PIL import Image as PILImage
    import io
    for cam_id, events_dir in resolve_user_events_dirs(user_id):
        if cam_id == "_global":
            continue
        img_file = events_dir / f"{event_id}.jpg"
        if img_file.exists():
            try:
                img = PILImage.open(img_file)
                img.thumbnail((640, 480))
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=75)
                return Response(content=buf.getvalue(), media_type="image/jpeg",
                                headers={"Cache-Control": "max-age=86400"})
            except Exception:
                pass
    raise HTTPException(status_code=404, detail="Frame no encontrado")


@app.get("/api/events/{event_id}/frame/{index}")
async def get_event_frame_by_index(event_id: str, index: int, user_id: str):
    """Sirve un frame específico del evento por índice (para carrusel/video)."""
    for cam_id, events_dir in resolve_user_events_dirs(user_id):
        if cam_id == "_global":
            continue
        frame_file = events_dir / event_id / "frames" / f"frame_{index:03d}.jpg"
        if frame_file.exists():
            try:
                from PIL import Image as PILImage
                import io
                img = PILImage.open(frame_file)
                img.thumbnail((640, 480))
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=75)
                return Response(content=buf.getvalue(), media_type="image/jpeg",
                                headers={"Cache-Control": "max-age=3600"})
            except Exception:
                pass
    raise HTTPException(status_code=404, detail="Frame no encontrado")


@app.get("/api/business/{user_id}")
async def get_business_data(user_id: str):
    """Obtiene el business.json del usuario."""
    business = load_business_json(user_id)
    if not business:
        raise HTTPException(status_code=404, detail="Business no encontrado")
    return business


@app.post("/api/business/{user_id}/migrate")
async def migrate_business_json(user_id: str):
    """Migra user.json a business.json."""
    try:
        from eva.tools import migrate_user_to_business
        user_file = STORAGE_ROOT / "users" / user_id / "user.json"
        if not user_file.exists():
            raise HTTPException(status_code=404, detail="user.json no encontrado")
        with open(user_file) as f:
            ud = json.load(f)
        business = migrate_user_to_business(ud)
        save_business_json(user_id, business)
        return {"success": True, "message": "Migración completada"}
    except Exception as e:
        return {"success": False, "error": str(e)}


def resolve_user_events_dirs(user_id: str):
    """Buscar carpetas de eventos del usuario."""
    base = STORAGE_ROOT / "users" / user_id
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


# ═══════════════════════════════════════════════════════════════════════════
# VIDEO EXPORT — Guardar últimos N minutos como video MP4
# ═══════════════════════════════════════════════════════════════════════════

@app.post("/api/cameras/{camera_id}/export-video")
async def export_camera_video(camera_id: str, user_id: str = None, minutes: int = 45):
    """
    Exporta los últimos N minutos de frames como video MP4.
    - minutes: 1-120 (default 45)
    - Retorna JSON con URL del video generado
    """
    if not user_id:
        raise HTTPException(status_code=400, detail="user_id required")
    minutes = max(1, min(minutes, 120))

    try:
        frames_dir = STORAGE_ROOT / "users" / user_id / "cameras" / camera_id / "frames"
        if not frames_dir.exists():
            raise HTTPException(status_code=404, detail="No hay frames para esta cámara")

        # Buscar frames en el directorio de eventos (frames individuales por evento)
        events_dir = STORAGE_ROOT / "users" / user_id / "cameras" / camera_id / "events"
        cutoff_ts = time.time() - (minutes * 60)

        # Recolectar frames de todos los eventos recientes
        frame_files = []
        if events_dir.exists():
            for event_dir in sorted(events_dir.iterdir(), key=lambda x: x.stat().st_mtime if x.exists() else 0, reverse=True):
                if not event_dir.is_dir():
                    continue
                # Verificar si el evento es reciente
                try:
                    event_ts = event_dir.stat().st_mtime
                    if event_ts < cutoff_ts:
                        continue
                except:
                    continue
                # Buscar frames dentro del evento
                frames_subdir = event_dir / "frames"
                if frames_subdir.exists():
                    for ff in sorted(frames_subdir.iterdir()):
                        if ff.suffix.lower() in ('.jpg', '.jpeg', '.png'):
                            frame_files.append(ff)
                # También buscar el frame principal del evento
                for ext in ('.jpg', '.jpeg', '.png'):
                    main_frame = event_dir / f"{event_dir.name}{ext}"
                    if main_frame.exists():
                        frame_files.append(main_frame)
                        break

        # Si no hay frames de eventos, usar latest_raw.jpg como fallback
        if not frame_files:
            latest_raw = frames_dir / "latest_raw.jpg"
            if latest_raw.exists():
                frame_files.append(latest_raw)

        if not frame_files:
            raise HTTPException(status_code=404, detail="No hay frames disponibles en el período solicitado")

        # Limitar a 500 frames para evitar videos enormes
        if len(frame_files) > 500:
            step = len(frame_files) // 500
            frame_files = frame_files[::step][:500]

        # Crear video con ffmpeg
        import subprocess
        import tempfile

        # Crear directorio de salida
        export_dir = STORAGE_ROOT / "users" / user_id / "cameras" / camera_id / "exports"
        export_dir.mkdir(parents=True, exist_ok=True)

        timestamp_str = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_file = export_dir / f"video_{camera_id}_{timestamp_str}.mp4"

        # Crear archivo de lista para ffmpeg
        with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as fflist:
            for ff in frame_files:
                fflist.write(f"file '{ff.absolute()}'\n")
                fflist.write(f"duration 0.5\n")  # 2 fps
            list_file = fflist.name

        # Ejecutar ffmpeg
        cmd = [
            'ffmpeg', '-y',
            '-f', 'concat',
            '-safe', '0',
            '-i', list_file,
            '-vf', 'scale=640:480:force_original_aspect_ratio=decrease,pad=640:480:(ow-iw)/2:(oh-ih)/2',
            '-c:v', 'libx264',
            '-preset', 'ultrafast',
            '-pix_fmt', 'yuv420p',
            '-r', '2',
            '-movflags', '+faststart',
            str(output_file)
        ]

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)

        # Limpiar archivo temporal
        try:
            os.unlink(list_file)
        except:
            pass

        if result.returncode != 0:
            logger.error(f"ffmpeg error: {result.stderr[-500:]}")
            raise HTTPException(status_code=500, detail=f"Error generando video: {result.stderr[-200:]}")

        if not output_file.exists():
            raise HTTPException(status_code=500, detail="No se pudo generar el video")

        file_size = output_file.stat().st_size
        logger.info(f"Video exportado: {output_file.name} ({file_size} bytes, {len(frame_files)} frames)")

        return {
            "success": True,
            "video_url": f"/api/cameras/{camera_id}/download-video?user_id={user_id}&file={output_file.name}",
            "frames_used": len(frame_files),
            "duration_seconds": len(frame_files) * 0.5,
            "file_size_bytes": file_size,
            "minutes_covered": minutes
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Export video error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/cameras/{camera_id}/download-video")
async def download_camera_video(camera_id: str, user_id: str = None, file: str = None):
    """Descarga un video exportado previamente."""
    if not user_id or not file:
        raise HTTPException(status_code=400, detail="user_id y file requeridos")

    # Sanitizar nombre de archivo
    safe_file = Path(file).name
    video_path = STORAGE_ROOT / "users" / user_id / "cameras" / camera_id / "exports" / safe_file

    if not video_path.exists():
        raise HTTPException(status_code=404, detail="Video no encontrado")

    return Response(
        content=video_path.read_bytes(),
        media_type="video/mp4",
        headers={
            "Content-Disposition": f'attachment; filename="{safe_file}"',
            "Cache-Control": "no-store"
        }
    )


# ═══════════════════════════════════════════════════════════════════════════
# EVA SETUP FLOW — Endpoint de configuración inicial
# ═══════════════════════════════════════════════════════════════════════════

class EvaSetupRequest(BaseModel):
    user_id: str
    message: str
    session_id: Optional[str] = None
    setup_phase: Optional[str] = None
    setup_session: Optional[dict] = None


@app.post("/api/eva/setup")
async def eva_setup_endpoint(request: EvaSetupRequest):
    """
    Endpoint para el flujo de configuración inicial con Eva.
    Maneja tanto el setup (GREETING → BUSINESS_NAME → CAMERA_CONNECT → ...)
    como el chat OS normal (CHAT_OS).
    """
    try:
        from eva.eva_chat_os import handle_eva_chat_os
        
        result = await handle_eva_chat_os(
            user_id=request.user_id,
            message=request.message,
            session_id=request.session_id,
            setup_phase=request.setup_phase,
            setup_session=request.setup_session,
        )
        return result
    except Exception as e:
        logger.error(f"Error en Eva setup: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return {
            "success": False,
            "error": str(e),
            "response": "Disculpa, tuve un problema. Intenta de nuevo.",
            "next_phase": request.setup_phase or "GREETING",
        }


@app.get("/api/eva/setup-session/{user_id}")
async def get_setup_session(user_id: str):
    """Obtiene la sesión de configuración de un usuario."""
    import json
    from pathlib import Path
    
    session_file = Path(f"/home/sam/storage/users/{user_id}/setup_session.json")
    if session_file.exists():
        with open(session_file) as f:
            return json.load(f)
    return {"phase": "GREETING", "session": {}}


@app.post("/api/eva/setup-session/{user_id}")
async def save_setup_session(user_id: str, data: dict):
    """Guarda la sesión de configuración de un usuario."""
    import json
    from pathlib import Path
    
    session_file = Path(f"/home/sam/storage/users/{user_id}/setup_session.json")
    session_file.parent.mkdir(parents=True, exist_ok=True)
    with open(session_file, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        return {"success": True}


# ═══════════════════════════════════════════════════════════════════════════
# Entry point — Solo se ejecuta cuando se corre directamente (no cuando se importa)
# ═══════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8005)
