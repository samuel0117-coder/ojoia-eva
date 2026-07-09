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
import camera_zones

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


# ═══════════════════════════════════════════════════════════════════════════
# FCM Token Registration
# ═══════════════════════════════════════════════════════════════════════════

@app.post("/api/fcm/register")
async def register_fcm_token(request: dict):
    """Registra token FCM para push notifications."""
    try:
        user_id = request.get("user_id", "") if isinstance(request, dict) else ""
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


@app.get("/api/chat/eva/history")
async def get_eva_chat_history(user_id: str, session_id: Optional[str] = None, limit: int = 50):
    """Historial de mensajes del chat con Eva."""
    try:
        if not user_id:
            raise HTTPException(status_code=400, detail="user_id required")
        # Sessions se guardan en user.json como eva_sessions_dict
        history = []
        uf = find_user_json(user_id)
        if uf and uf.exists():
            try:
                with open(uf) as f:
                    ud = json.load(f)
                sessions = ud.get("eva_sessions", {}) or {}
                if session_id and session_id in sessions:
                    msgs = sessions[session_id].get("messages", [])
                    history = msgs[-limit:]
                elif not session_id:
                    # Todas las sesiones, ordenado por ts
                    all_msgs = []
                    for sid, sdata in sessions.items():
                        for m in sdata.get("messages", []):
                            m2 = {**m, "session_id": sid}
                            all_msgs.append(m2)
                    all_msgs.sort(key=lambda x: x.get("timestamp", 0), reverse=True)
                    history = all_msgs[:limit]
            except Exception as e:
                logger.warning(f"Error leyendo eva_sessions from user.json: {e}")

        # Si no hay sesiones en user.json, intentar desde archivo dedicado
        if not history:
            try:
                chat_dir = STORAGE_ROOT / "users" / user_id / "eva_chat"
                if chat_dir.exists():
                    if session_id:
                        session_file = chat_dir / f"{session_id}.json"
                        if session_file.exists():
                            with open(session_file) as f:
                                session_data = json.load(f)
                            history = session_data.get("messages", [])[-limit:]
                    else:
                        # todas las sesiones
                        all_files = sorted(chat_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
                        for session_file in all_files[:5]:
                            try:
                                with open(session_file) as f:
                                    sd = json.load(f)
                                for m in sd.get("messages", []):
                                    m2 = {**m, "session_id": session_file.stem}
                                    history.append(m2)
                                if len(history) >= limit:
                                    break
                            except:
                                continue
                        history = history[-limit:]
            except Exception as e:
                logger.warning(f"Error leyendo archivos de chat: {e}")

        return {
            "success": True,
            "history": history,
            "count": len(history),
            "user_id": user_id,
            "session_id": session_id
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error get_eva_chat_history: {e}")
        return {"success": False, "error": str(e), "history": []}


@app.post("/api/chat/eva/save")
async def save_eva_chat_message(request: dict):
    """Guarda un mensaje del chat con Eva en user.json."""
    try:
        user_id = request.get("user_id", "")
        session_id = request.get("session_id", "")
        role = request.get("role", "user")
        content = request.get("content", "")
        timestamp = request.get("timestamp") or int(time.time())
        if not user_id or not session_id:
            raise HTTPException(status_code=400, detail="user_id and session_id required")
        uf = find_user_json(user_id)
        if not uf or not uf.exists():
            return {"success": False, "error": "user not found"}
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
        with open(uf, "w") as f:
            json.dump(ud, f, indent=2)
        return {"success": True, "saved": True}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error save_eva_chat_message: {e}")
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
                "Access-Control-Allow-Headers": "*",
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
        _send_vigilance_fcm(user_id, camera_id, event_id, yolo_count, yolo_classes)
    except Exception as e:
        logger.error(f"Error saving vigilance event: {e}")


def _send_vigilance_fcm(user_id: str, camera_id: str, event_id: str, yolo_count: int, yolo_classes: list):
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
        for token in tokens[:3]:
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
                    break
            with open(uf, "w") as f:
                json.dump(ud, f, indent=2)
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
        if is_vigilante and yolo_count > 0:
            logger.warning(f"MODO CENTINELA: {yolo_count} objects {yolo_classes} → alerta directa")
            _save_vigilance_event(user_id, camera_id, img_bytes, yolo_count, yolo_classes, client_ip)

        # ── [6] ENCOLAR para grid + Qwen en background ──
        # YOLO Gate: si count==0, no pasar al grid (ahorro recursos)
        if yolo_count > 0:
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

            # Procesar grid cuando esté lleno (16) o en centinela con ≥4 frames
            if grid_is_full or (is_vigilante and grid.get_frame_count() >= 4):
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
async def _start_yolo_worker():
    """Iniciar el YOLO Worker en el mismo event loop de uvicorn."""
    global _WORKER_STARTED
    if not _WORKER_STARTED:
        _WORKER_STARTED = True
        logger.info("🚀 Iniciando YOLO Worker en background...")
        asyncio.create_task(yolo_worker())


if __name__ == "__main__":
    import uvicorn
    # Arrancar servidor FastAPI (el worker se inicia en el startup event)
    uvicorn.run(app, host="0.0.0.0", port=8005, loop="asyncio")
