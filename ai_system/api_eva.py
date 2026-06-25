#!/usr/bin/env python3
"""
OjoIA API Eva v7.1 — "Cámara Primero, Reglas Después"
Flujo optimizado: conectar cámara en turno 3-5, reglas basadas en imagen real.
Mejoras v7.1:
- Análisis inteligente de imagen al primer frame (640px, no 512px)
- Eva da opinión experta sobre ubicación de cámara (¿es correcta? ¿enfocada? ¿obstáculos?)
- Sugiere ajustes específicos basados en tipo de negocio + imagen real
- describe_image mejorado: 60 palabras, análisis de nitidez, ángulo, obstáculos
"""
import logging
import os
import json
import re
import time
import base64
import hashlib
import hmac
import secrets
import shutil
import asyncio
import subprocess
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, List, Any

from fastapi import FastAPI, UploadFile, File, HTTPException, Form, Request, Header
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

_log_face = logging.getLogger("identity")
from fastapi.responses import JSONResponse, Response, FileResponse
from starlette.status import HTTP_200_OK
from pydantic import BaseModel
import httpx
import firebase_admin
from firebase_admin import auth, credentials

# Importar modulos locales
from gateway_resize import resize_image, image_to_base64
from orchestrator import orchestrator
from eva.camera_builder import normalize_camera_vigilance_config, build_vigilance_prompt

# Configuracion Global
STORAGE_ROOT = Path("/home/sam/storage")
DISKS_CONFIG_FILE = STORAGE_ROOT / "disks_config.json"
EVA_CONFIG_FILE = STORAGE_ROOT / "eva_config.json"
ADMIN_CONFIG_FILE = STORAGE_ROOT / "admin_config.json"
ADMIN_METRICS_FILE = STORAGE_ROOT / "admin_metrics.json"
PROMPT_AUDITOR_URL = "http://localhost:8004/v1/chat/completions"
ADMIN_LOG_DIR = STORAGE_ROOT / "admin_logs"
ADMIN_SESSION_TTL_SECONDS = 8 * 60 * 60
BUFFER_WINDOW_SECONDS = 45 * 60
BUFFER_TARGET_INTERVAL_SECONDS = 3
BUFFER_MAX_FRAMES = 1000
BUFFER_MAX_BYTES = 200 * 1024 * 1024
CAMERA_ONLINE_GRACE_SECONDS = 10 * 60
FIRMWARE_VERSION = "v6.8"
FIRMWARE_BIN = Path("/home/sam/esp32cam_project/.pio/build/esp32cam/firmware.bin")
FIREBASE_KEY_PATH = Path("/home/sam/Downloads/firebase-key.json")
FIREBASE_QUEUE_BUCKET = "ojoia-67216.firebasestorage.app"
FIREBASE_QUEUE_PREFIX = "frames/"
FIREBASE_QUEUE_AUTOPROCESS = True
FIREBASE_QUEUE_BATCH_SIZE = 20
FIREBASE_QUEUE_INTERVAL_SECONDS = 30
_firebase_queue_task_running = False

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
_last_yolo_by_camera: Dict[str, Dict[str, Any]] = {}
_last_yolo_ts_by_camera: Dict[str, float] = {}
_last_led_auto_ts_by_camera: Dict[str, float] = {}


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

ADMIN_AUTH_PUBLIC_PATHS = {"/admin/auth/login", "/admin/auth/me", "/admin/auth/logout"}

@app.middleware("http")
async def admin_auth_middleware(request: Request, call_next):
    path = request.url.path
    if request.method == "OPTIONS":
        return await call_next(request)
    if path.startswith("/admin/") and path not in ADMIN_AUTH_PUBLIC_PATHS:
        if not _is_admin_request_authorized(request):
            _audit_log("admin_unauthorized", actor="anonymous", target=path, data={"method": request.method, "ip": request.client.host if request.client else ""})
            return JSONResponse(
                status_code=401,
                content={"success": False, "error": "Admin no autorizado"},
                headers={
                    "WWW-Authenticate": "Bearer",
                    "Access-Control-Allow-Origin": "*",
                    "Access-Control-Allow-Methods": "GET, POST, PUT, DELETE, OPTIONS",
                    "Access-Control-Allow-Headers": "*",
                },
            )
    response = await call_next(request)
    if path.startswith("/admin/"):
        _audit_log("admin_request", actor=_admin_actor_from_request(request), target=path, data={"method": request.method, "status_code": response.status_code, "ip": request.client.host if request.client else ""})
    return response

@app.on_event("startup")
async def startup_event():
    global _firebase_queue_task_running
    if FIREBASE_QUEUE_AUTOPROCESS and not _firebase_queue_task_running:
        _firebase_queue_task_running = True
        asyncio.create_task(_firebase_queue_autoprocess_loop())

async def _firebase_queue_autoprocess_loop():
    while True:
        await asyncio.sleep(FIREBASE_QUEUE_INTERVAL_SECONDS)
        try:
            result = await admin_process_firestore_queue()
            logger.info(f"Firebase queue autoprocess result: {result}")
        except Exception as e:
            logger.error(f"Firebase queue autoprocess error: {e}")

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
    except HTTPException as e:
        # Dejar que FastAPI maneje HTTPException con sus headers
        return Response(
            status_code=e.status_code,
            content=json.dumps({"success": False, "error": e.detail}),
            media_type="application/json",
            headers={
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Methods": "GET, POST, PUT, DELETE, OPTIONS",
                "Access-Control-Allow-Headers": "*",
            }
        )
    except Exception as e:
        logger.error(f"Middleware error: {e}")
        return Response(
            status_code=500,
            content=json.dumps({"success": False, "error": "Internal server error"}),
            media_type="application/json",
            headers={"Access-Control-Allow-Origin": "*"})

# Helpers de Almacenamiento
def get_camera_config_static(user_id: str, camera_id: str) -> dict:
    """Lee camera.json de una camara."""
    cam_file = STORAGE_ROOT / "users" / user_id / "cameras" / camera_id / "camera.json"
    if cam_file.exists():
        try:
            with open(cam_file) as f:
                return normalize_camera_vigilance_config(json.load(f))
        except Exception:
            pass
    return {}


def _save_camera_config_static(user_id: str, camera_id: str, config: dict):
    cam_file = STORAGE_ROOT / "users" / user_id / "cameras" / camera_id / "camera.json"
    cam_file.parent.mkdir(parents=True, exist_ok=True)
    _save_json_safely(cam_file, normalize_camera_vigilance_config(config))


def _get_grid_size_for_camera(user_id: str, camera_id: str) -> int:
    cam_cfg = get_camera_config_static(user_id, camera_id)
    try:
        return max(1, min(int(cam_cfg.get("grid_size") or 16), 16))
    except Exception:
        return 16


def _get_current_mode(schedule: dict, vigilance: dict, current_time: str = None) -> str:
    if not isinstance(vigilance, dict) or not vigilance.get("enabled", True):
        return "normal"
    try:
        from datetime import datetime as dt, timedelta
        now_dt = dt.strptime(current_time or dt.now().strftime("%H:%M"), "%H:%M")
        open_t = dt.strptime((schedule or {}).get("open", "08:00"), "%H:%M")
        close_t = dt.strptime((schedule or {}).get("close", "22:00"), "%H:%M")
        grace = int(vigilance.get("grace_minutes", 15))
        sentinel_start = close_t + timedelta(minutes=grace)
        if now_dt < open_t or now_dt >= sentinel_start:
            return "sentinel" if (vigilance.get("sentinel_mode") or {}).get("enabled", True) else "normal"
        return "normal" if (vigilance.get("normal_mode") or {}).get("enabled", True) else "normal"
    except Exception:
        return "normal"

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
                            else:
                                any_match = uid
                except:
                    pass
        return founder_match or any_match or provided_user_id
    return provided_user_id

def _load_json_safely(path: Path) -> Optional[dict]:
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None

def _save_json_safely(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    tmp.replace(path)

_save_json = _save_json_safely
_load_json = _load_json_safely


def _camera_buffer_dir(user_id: str, camera_id: str) -> Path:
    return STORAGE_ROOT / "users" / user_id / "cameras" / camera_id / "recent_frames"


def _camera_events_dir(user_id: str, camera_id: str) -> Path:
    return STORAGE_ROOT / "users" / user_id / "cameras" / camera_id / "events"


def _camera_clips_dir(user_id: str, camera_id: str) -> Path:
    return STORAGE_ROOT / "users" / user_id / "cameras" / camera_id / "clips"


def _recent_manifest_path(user_id: str, camera_id: str) -> Path:
    return _camera_buffer_dir(user_id, camera_id) / "manifest.json"


def _load_recent_manifest(user_id: str, camera_id: str) -> dict:
    path = _recent_manifest_path(user_id, camera_id)
    data = _load_json_safely(path)
    if isinstance(data, dict):
        return data
    return {"camera_id": camera_id, "frames": []}


def _save_recent_manifest(user_id: str, camera_id: str, data: dict):
    _save_json_safely(_recent_manifest_path(user_id, camera_id), data)


def _cleanup_recent_frames(user_id: str, camera_id: str):
    buffer_dir = _camera_buffer_dir(user_id, camera_id)
    manifest = _load_recent_manifest(user_id, camera_id)
    frames = manifest.get("frames", [])
    if not isinstance(frames, list):
        frames = []
    cutoff = time.time() - BUFFER_WINDOW_SECONDS
    frames = [f for f in frames if isinstance(f, dict) and int(f.get("timestamp", 0)) >= cutoff]
    total_bytes = 0
    ordered = []
    for f in sorted(frames, key=lambda x: int(x.get("timestamp", 0))):
        fp = buffer_dir / f.get("file", "")
        size = fp.stat().st_size if fp.exists() and fp.is_file() else int(f.get("size", 0) or 0)
        if len(ordered) >= BUFFER_MAX_FRAMES or total_bytes + size > BUFFER_MAX_BYTES:
            try:
                fp.unlink()
            except Exception:
                pass
            continue
        ordered.append(f)
        total_bytes += size
    manifest.update({
        "camera_id": camera_id,
        "window_seconds": BUFFER_WINDOW_SECONDS,
        "target_interval_seconds": BUFFER_TARGET_INTERVAL_SECONDS,
        "max_frames": BUFFER_MAX_FRAMES,
        "max_bytes": BUFFER_MAX_BYTES,
        "frames": ordered,
        "total_bytes": total_bytes,
        "updated_at": time.time()
    })
    _save_recent_manifest(user_id, camera_id, manifest)
    return manifest


def _save_recent_frame(user_id: str, camera_id: str, img_bytes: bytes, timestamp: int = None) -> dict:
    if not user_id or not camera_id or not img_bytes:
        return {}
    try:
        buffer_dir = _camera_buffer_dir(user_id, camera_id)
        buffer_dir.mkdir(parents=True, exist_ok=True)
        ts = int(timestamp or time.time())
        manifest = _load_recent_manifest(user_id, camera_id)
        frames = manifest.get("frames", []) if isinstance(manifest.get("frames", []), list) else []
        if frames:
            last_ts = int(frames[-1].get("timestamp", 0)) if isinstance(frames[-1], dict) else 0
            if last_ts and ts - last_ts < BUFFER_TARGET_INTERVAL_SECONDS:
                return frames[-1]
        safe_ts = int(time.time() * 1000)
        digest = hashlib.sha1(img_bytes).hexdigest()[:8]
        filename = f"frame_{safe_ts}_{digest}.jpg"
        file_path = buffer_dir / filename
        file_path.write_bytes(img_bytes)
        frames.append({
            "timestamp": ts,
            "datetime": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(ts)),
            "file": filename,
            "size": len(img_bytes)
        })
        manifest.update({
            "camera_id": camera_id,
            "window_seconds": BUFFER_WINDOW_SECONDS,
            "target_interval_seconds": BUFFER_TARGET_INTERVAL_SECONDS,
            "max_frames": BUFFER_MAX_FRAMES,
            "max_bytes": BUFFER_MAX_BYTES,
            "frames": frames,
            "updated_at": time.time()
        })
        _save_recent_manifest(user_id, camera_id, manifest)
        return _cleanup_recent_frames(user_id, camera_id).get("frames", [])[-1]
    except Exception as e:
        logger.debug(f"recent frame buffer error: {e}")
        return {}


def _get_recent_frames(user_id: str, camera_id: str, limit: int = 60, minutes: int = None) -> list:
    manifest = _cleanup_recent_frames(user_id, camera_id)
    frames = manifest.get("frames", []) if isinstance(manifest.get("frames", []), list) else []
    cutoff = time.time() - ((minutes or (BUFFER_WINDOW_SECONDS // 60)) * 60)
    frames = [f for f in frames if isinstance(f, dict) and int(f.get("timestamp", 0)) >= cutoff]
    frames = frames[-int(limit):]
    for i, f in enumerate(frames):
        f = dict(f)
        f["index"] = i
        f["frame_url"] = f"/api/cameras/{camera_id}/recent-frame/{i}?user_id={user_id}&minutes={minutes or (BUFFER_WINDOW_SECONDS // 60)}"
        frames[i] = f
    return frames


def _generate_video_from_frames(frames_dir: Path, output_mp4: Path, fps: int = 2) -> bool:
    jpgs = sorted(frames_dir.glob("frame_*.jpg"))
    if not jpgs:
        return False
    try:
        subprocess.run([
            "ffmpeg", "-y", "-framerate", str(fps), "-i", "frame_%03d.jpg",
            "-vf", "scale=640:-2", "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-movflags", "+faststart", str(output_mp4)
        ], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=120, cwd=frames_dir)
        return output_mp4.exists() and output_mp4.stat().st_size > 0
    except Exception as e:
        logger.debug(f"ffmpeg video generation failed: {e}")
        return False


def _copy_frames_to_folder(source_frames: list, source_dir: Path, dest_frames_dir: Path, max_frames: int = 16) -> list:
    dest_frames_dir.mkdir(parents=True, exist_ok=True)
    copied = []
    for i, f in enumerate(source_frames[-max_frames:]):
        src = source_dir / f.get("file", "")
        if not src.exists():
            continue
        dst_name = f"frame_{i:03d}.jpg"
        dst = dest_frames_dir / dst_name
        shutil.copyfile(src, dst)
        item = dict(f)
        item.update({"index": i, "file": dst_name})
        copied.append(item)
    return copied


def _generate_clip_from_frames(user_id: str, camera_id: str, frames: list, folder_name: str, folder_type: str, event_id: str = None, summary: str = "") -> dict:
    base_dir = _camera_events_dir(user_id, camera_id) if folder_type == "events" else _camera_clips_dir(user_id, camera_id)
    folder = base_dir / folder_name
    frames_dir = folder / "frames"
    folder.mkdir(parents=True, exist_ok=True)
    copied = _copy_frames_to_folder(frames, _camera_buffer_dir(user_id, camera_id), frames_dir, max_frames=24)
    mp4 = folder / f"{folder_name}.mp4"
    video_ok = _generate_video_from_frames(frames_dir, mp4, fps=2)
    metadata = {
        "id": folder_name,
        "user_id": user_id,
        "camera_id": camera_id,
        "event_id": event_id,
        "type": folder_type,
        "summary": summary,
        "created_at": time.time(),
        "datetime": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
        "frames_count": len(copied),
        "video_file": f"{folder_name}.mp4" if video_ok else "",
        "frames": copied,
        "source": "recent_buffer"
    }
    _save_json_safely(folder / f"{folder_name}.json", metadata)
    return metadata


def _promote_recent_to_event(user_id: str, camera_id: str, event_id: str, summary: str = "", qwen_json: dict = None, frames_count: int = 16) -> dict:
    if not event_id:
        return {}
    event_file = _camera_events_dir(user_id, camera_id) / event_id / f"{event_id}.json"
    existing_event = _load_json_safely(event_file) or _load_json_safely(_camera_events_dir(user_id, camera_id) / f"{event_id}.json") or {}
    frames = _get_recent_frames(user_id, camera_id, limit=frames_count, minutes=BUFFER_WINDOW_SECONDS // 60)
    if not frames:
        return {}
    metadata = _generate_clip_from_frames(user_id, camera_id, frames, event_id, "events", event_id=event_id, summary=summary or "")
    merged = {**metadata, **existing_event}
    merged.update({
        "frames": metadata.get("frames", []),
        "video_file": metadata.get("video_file", ""),
        "clip_type": "event_package"
    })
    if qwen_json:
        merged["qwen_json"] = qwen_json
    try:
        _save_json_safely(event_file, merged)
    except Exception as e:
        logger.debug(f"event package update failed: {e}")
    return metadata


def _save_recent_clip(user_id: str, camera_id: str, minutes: int = 45) -> dict:
    frames = _get_recent_frames(user_id, camera_id, limit=1000, minutes=minutes)
    if not frames:
        return {"success": False, "error": "No recent frames"}
    clip_id = f"clip_{int(time.time())}_{camera_id}"
    metadata = _generate_clip_from_frames(user_id, camera_id, frames, clip_id, "clips", event_id=None, summary=f"Últimos {minutes} minutos")
    return {"success": True, "clip_id": clip_id, "clip": metadata}


def _camera_aliases(ud: dict) -> dict:
    aliases = ud.get("camera_aliases", {})
    return aliases if isinstance(aliases, dict) else {}


def _camera_alias_candidates(user_id: str, camera_id: str) -> list:
    if not user_id or user_id == "default":
        return []
    aliases = _camera_aliases(_load_json_safely(find_user_json(user_id)) or {})
    candidates = []
    if camera_id and camera_id not in candidates:
        candidates.append(camera_id)
    alias_value = aliases.get(camera_id)
    if alias_value and alias_value not in candidates:
        candidates.append(alias_value)
    for k, v in aliases.items():
        if v == camera_id and k not in candidates:
            candidates.append(k)
    return candidates


def _resolve_camera_alias(user_id: str, camera_id: str, client_ip: str = "unknown") -> tuple:
    """Resolve physical camera IDs like OJO-E17604 to configured canonical IDs."""
    if not user_id or user_id == "default":
        if camera_id.startswith("OJO-"):
            users_dir = STORAGE_ROOT / "users"
            candidates = []
            if users_dir.is_dir():
                for user_folder in users_dir.iterdir():
                    uid = user_folder.name
                    if not uid or uid == "default" or uid.startswith("test_") or uid.startswith("debug_"):
                        continue
                    uf = user_folder / "user.json"
                    ud = _load_json_safely(uf)
                    if not ud:
                        continue
                    cams = ud.get("cameras", []) if isinstance(ud.get("cameras"), list) else []
                    if not cams:
                        continue
                    aliases = _camera_aliases(ud)
                    if camera_id in aliases.values() or camera_id in [c.get("camera_id") for c in cams]:
                        return uid, camera_id
                    owner = ud.get("owner", {}).get("name") if isinstance(ud.get("owner"), dict) else ud.get("name")
                    if ud.get("business_name") and owner != "Default":
                        candidates.append((uf.stat().st_mtime, uid, ud, cams))
            if candidates:
                candidates.sort(reverse=True)
                _, uid, ud, cams = candidates[0]
                canonical = cams[0].get("camera_id")
                aliases = _camera_aliases(ud)
                aliases[camera_id] = canonical
                ud["camera_aliases"] = aliases
                cams[0]["physical_camera_id"] = camera_id
                cams[0]["last_announce_ip"] = client_ip
                _save_json_safely(uf, ud)
                return uid, canonical
        return user_id, camera_id
    uf = find_user_json(user_id)
    if not uf or not uf.exists():
        return user_id, camera_id
    ud = _load_json_safely(uf)
    if not ud:
        return user_id, camera_id
    aliases = _camera_aliases(ud)
    if camera_id in aliases:
        return user_id, aliases[camera_id]
    cams = ud.get("cameras", []) if isinstance(ud.get("cameras"), list) else []
    if len(cams) == 1 and camera_id.startswith("OJO-"):
        canonical = cams[0].get("camera_id")
        aliases[camera_id] = canonical
        ud["camera_aliases"] = aliases
        cams[0]["physical_camera_id"] = camera_id
        cams[0]["last_announce_ip"] = client_ip
        _save_json_safely(uf, ud)
        return user_id, canonical
    return user_id, camera_id

def find_camera_owner(camera_id: str) -> tuple:
    users_dir = STORAGE_ROOT / "users"
    if users_dir.is_dir():
        for user_dir in users_dir.iterdir():
            if not user_dir.is_dir():
                continue
            uf = user_dir / "user.json"
            if not uf.exists():
                continue
            try:
                ud = json.load(open(uf))
                for c in ud.get("cameras", []):
                    if c.get("camera_id") == camera_id or c.get("physical_camera_id") == camera_id:
                        return ud.get("user_id", user_dir.name), c.get("camera_id") or camera_id
            except Exception:
                continue
    return "", ""


def pop_pending_camera_command(user_id: str, camera_id: str, max_age: int = 600) -> Optional[dict]:
    """Obtener y consumir un comando pendiente para una cámara física o alias."""
    uf = find_user_json(user_id)
    if not uf or not uf.exists():
        return None
    try:
        with open(uf) as f:
            ud = json.load(f)
        for c in ud.get("cameras", []):
            if c.get("camera_id") == camera_id or c.get("physical_camera_id") == camera_id:
                key = c.get("camera_id") or camera_id
                pending = ud.setdefault("pending_commands", {})
                cmd = pending.pop(key, None) or pending.pop(camera_id, None)
                if cmd:
                    created = int(cmd.get("created") or 0)
                    if int(time.time()) - created > max_age:
                        with open(uf, "w") as f:
                            json.dump(ud, f, indent=2)
                        return None
                    with open(uf, "w") as f:
                        json.dump(ud, f, indent=2)
                    return cmd.get("body")
                if pending:
                    with open(uf, "w") as f:
                        json.dump(ud, f, indent=2)
                return None
    except Exception:
        return None
    return None


def _normalize_system_prompt(user_id: str, camera_id: str, raw_prompt: str = "") -> str:
    uf = find_user_json(user_id)
    if not uf or not uf.exists():
        return raw_prompt or ""
    ud = _load_json_safely(uf)
    if not ud:
        return raw_prompt or ""
    clean = raw_prompt.strip() if raw_prompt else ""
    if clean.startswith("```"):
        clean = re.sub(r"^```(?:json)?\s*", "", clean)
        clean = re.sub(r"\s*```$", "", clean).strip()
    if clean and not clean.lstrip().startswith("{"):
        return raw_prompt
    ctx = ud.get("vigilance_context", {}) if isinstance(ud.get("vigilance_context"), dict) else {}
    schedule = ud.get("schedule", {"open": "08:00", "close": "22:00"})
    owner = ud.get("name") or (ud.get("owner", {}).get("name") if isinstance(ud.get("owner"), dict) else "") or "el dueño"
    biz = ud.get("business_name", "negocio")
    btype = ud.get("business_type", "negocio")
    zone = ctx.get("zone") or "la zona"
    concern = ctx.get("concern") or "seguridad general"
    forbidden = ctx.get("forbidden_events") or "actividad sospechosa o no autorizada"
    normal = ctx.get("normal_state") or "actividad normal de la zona"
    authorized = ctx.get("authorized_people") or "empleados autorizados"
    objects = ctx.get("important_objects") or "dinero, productos, puerta, caja registradora"
    severity = ctx.get("severity_rules") or "robo, incendio y persona no autorizada son críticos"
    return (f"Eres un vigilante de seguridad para {biz} ({btype}) en República Dominicana.\n"
            f"Dueño: {owner}. Zona de la cámara: {zone}. Horario: {schedule.get('open','08:00')} a {schedule.get('close','22:00')}.\n"
            f"Preocupación principal: {concern}. Nunca debe pasar: {forbidden}.\n"
            f"Estado normal esperado: {normal}. Personas autorizadas: {authorized}.\n"
            f"Objetos importantes: {objects}. Reglas de severidad: {severity}.\n"
            f"No analices cada frame. Reporta solo anomalías observables o resúmenes. Responde SOLO JSON con summary, details, anomalias e importancia.")

def _camera_alias_candidates(user_id: str, camera_id: str) -> list:
    candidates = [camera_id]
    uf = find_user_json(user_id)
    if not uf or not uf.exists():
        return candidates
    ud = _load_json_safely(uf)
    if not ud:
        return candidates
    aliases = _camera_aliases(ud)
    for phys, canonical in aliases.items():
        if canonical == camera_id and phys not in candidates:
            candidates.append(phys)
    for cam in ud.get("cameras", []) if isinstance(ud.get("cameras"), list) else []:
        if cam.get("physical_camera_id") and cam.get("camera_id") == camera_id:
            if cam["physical_camera_id"] not in candidates:
                candidates.append(cam["physical_camera_id"])
    return candidates

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
    uid = user_id or ""
    cid = camera_id or ""
    frame_bytes = b""
    last_cam = ""
    for alias_id in _camera_alias_candidates(uid, cid):
        grid = orchestrator._get_grid(uid, alias_id)
        frame_bytes = grid.get_last_frame_bytes()
        last_cam = grid.get_last_camera_id()
        if frame_bytes and (not cid or last_cam == alias_id):
            break
        frame_bytes = b""
    if not frame_bytes and cid and user_id:
        try:
            events_dir = STORAGE_ROOT / "users" / user_id / "cameras" / cid / "events"
            latest_vig = events_dir / "latest_vigilance.jpg"
            if latest_vig.exists():
                frame_bytes = latest_vig.read_bytes()
                last_cam = cid
        except:
            pass
    image_b64 = base64.b64encode(frame_bytes).decode() if frame_bytes else ""
    cached_yolo = _last_yolo_by_camera.get(last_cam or cid, {}) if (last_cam or cid) else {}
    cached_classes = cached_yolo.get("classes", [])
    cached_detections = cached_yolo.get("detections", [])
    cached_count = cached_yolo.get("count") or 0
    if cached_count == 0 and cached_detections:
        cached_count = len(cached_detections)
        cached_classes = list(dict.fromkeys([d.get("class", "obj") for d in cached_detections if d.get("class")]))
    grid_yolo_count = grid.get_last_yolo_count() if 'grid' in locals() else cached_count
    yolo_count = cached_count if grid_yolo_count == 0 and cached_count > 0 else grid_yolo_count
    yolo_classes = cached_classes if 'grid' not in locals() or grid_yolo_count == 0 else cached_yolo.get("classes", [])
    yolo_detections = cached_detections if 'grid' not in locals() or grid_yolo_count == 0 else cached_yolo.get("detections", [])
    return {
        "success": bool(frame_bytes),
        "image_b64": image_b64,
        "camera_id": last_cam or cid,
        "yolo": {
            "count": yolo_count,
            "classes": yolo_classes,
            "detections": yolo_detections,
            "timestamp": cached_yolo.get("timestamp")
        },
        "metadata": {"timestamp": int(time.time())}
    }

@app.get("/frames/latest.jpg")
async def get_latest_frame_jpg(camera_id: Optional[str] = None, user_id: Optional[str] = None):
    uid = user_id or ""
    cid = camera_id or ""
    frame_bytes = b""
    for alias_id in _camera_alias_candidates(uid, cid):
        grid = orchestrator._get_grid(uid, alias_id)
        fb = grid.get_last_frame_bytes()
        last_cam = grid.get_last_camera_id()
        if fb and (not cid or last_cam == alias_id):
            frame_bytes = fb
            break
    if not frame_bytes and cid and user_id:
        try:
            events_dir = STORAGE_ROOT / "users" / user_id / "cameras" / cid / "events"
            latest_vig = events_dir / "latest_vigilance.jpg"
            if latest_vig.exists():
                frame_bytes = latest_vig.read_bytes()
        except:
            pass
    if not frame_bytes:
        return Response(status_code=204)
    return Response(content=frame_bytes, media_type="image/jpeg", headers={"Cache-Control": "no-store, no-cache, must-revalidate"})


@app.get("/frames/latest-raw.jpg")
async def get_latest_raw_frame_jpg(camera_id: Optional[str] = None, user_id: Optional[str] = None):
    uid = user_id or ""
    cid = camera_id or ""
    for alias_id in _camera_alias_candidates(uid, cid):
        frames_dir = STORAGE_ROOT / "users" / uid / "cameras" / alias_id / "frames"
        latest_raw = frames_dir / "latest_raw.jpg"
        if latest_raw.exists():
            return Response(
                content=latest_raw.read_bytes(),
                media_type="image/jpeg",
                headers={"Cache-Control": "no-store, no-cache, must-revalidate"}
            )
    if uid and cid:
        events_dir = STORAGE_ROOT / "users" / uid / "cameras" / cid / "events"
        latest_vig = events_dir / "latest_vigilance.jpg"
        if latest_vig.exists():
            return Response(
                content=latest_vig.read_bytes(),
                media_type="image/jpeg",
                headers={"Cache-Control": "no-store, no-cache, must-revalidate"}
            )
    return Response(status_code=204)


@app.get("/api/cameras/{camera_id}/recent-frames")
async def get_camera_recent_frames(camera_id: str, user_id: str, limit: int = 60, minutes: int = 45):
    frames = _get_recent_frames(user_id, camera_id, limit=limit, minutes=minutes)
    return {
        "success": True,
        "camera_id": camera_id,
        "window_minutes": minutes,
        "frames_count": len(frames),
        "frames": frames
    }


@app.get("/api/cameras/{camera_id}/recent-frame/{index}")
async def get_camera_recent_frame(camera_id: str, index: int, user_id: str, minutes: int = 45):
    frames = _get_recent_frames(user_id, camera_id, limit=1000, minutes=minutes)
    if index < 0 or index >= len(frames):
        return Response(status_code=204)
    frame = frames[index]
    file_path = _camera_buffer_dir(user_id, camera_id) / frame.get("file", "")
    if not file_path.exists():
        return Response(status_code=204)
    return Response(content=file_path.read_bytes(), media_type="image/jpeg", headers={"Cache-Control": "no-store, no-cache, must-revalidate"})


@app.post("/api/cameras/{camera_id}/save-recent-clip")
async def save_camera_recent_clip(camera_id: str, user_id: str, minutes: int = 45):
    result = _save_recent_clip(user_id, camera_id, minutes=minutes)
    if not result.get("success"):
        raise HTTPException(status_code=404, detail=result.get("error", "No recent frames"))
    return result


@app.get("/api/cameras/{camera_id}/clips/{clip_id}")
async def get_camera_clip(camera_id: str, clip_id: str, user_id: str):
    clip_file = _camera_clips_dir(user_id, camera_id) / clip_id / f"{clip_id}.json"
    clip = _load_json_safely(clip_file)
    if not clip:
        raise HTTPException(status_code=404, detail="Clip not found")
    return clip


@app.get("/api/cameras/{camera_id}/clips/{clip_id}/video.mp4")
async def get_camera_clip_video(camera_id: str, clip_id: str, user_id: str):
    clip_file = _camera_clips_dir(user_id, camera_id) / clip_id / f"{clip_id}.json"
    clip = _load_json_safely(clip_file)
    if not clip:
        raise HTTPException(status_code=404, detail="Clip not found")
    video_file = _camera_clips_dir(user_id, camera_id) / clip_id / clip.get("video_file", "")
    if not video_file.exists():
        raise HTTPException(status_code=404, detail="Video not found")
    return FileResponse(video_file, media_type="video/mp4", filename=video_file.name)


@app.get("/api/cameras/{camera_id}/clips/{clip_id}/frame/{index}")
async def get_camera_clip_frame(camera_id: str, clip_id: str, index: int, user_id: str):
    clip_file = _camera_clips_dir(user_id, camera_id) / clip_id / f"{clip_id}.json"
    clip = _load_json_safely(clip_file)
    if not clip:
        raise HTTPException(status_code=404, detail="Clip not found")
    frames = clip.get("frames", [])
    if index < 0 or index >= len(frames):
        return Response(status_code=204)
    frame_file = _camera_clips_dir(user_id, camera_id) / clip_id / "frames" / frames[index].get("file", "")
    if not frame_file.exists():
        return Response(status_code=204)
    return Response(content=frame_file.read_bytes(), media_type="image/jpeg", headers={"Cache-Control": "no-store, no-cache, must-revalidate"})


@app.get("/api/events/{event_id}/video.mp4")
async def get_event_video(event_id: str, user_id: str):
    for cam_dir in (STORAGE_ROOT / "users" / user_id / "cameras").glob("*"):
        events_dir = cam_dir / "events"
        event_file = events_dir / event_id / f"{event_id}.json"
        event = _load_json_safely(event_file)
        event_path = events_dir / event_id
        if not event:
            event_file = events_dir / f"{event_id}.json"
            event = _load_json_safely(event_file)
            event_path = events_dir / event_id
        if not event:
            continue
        event["_events_dir"] = str(event_path)
        event = _attach_event_package(event)
        video_file = event_path / event.get("video_file", "")
        if not video_file.exists():
            video_file = events_dir / event.get("video_file", "")
        if video_file.exists():
            return FileResponse(video_file, media_type="video/mp4", filename=video_file.name)
    raise HTTPException(status_code=404, detail="Event video not found")


@app.get("/api/events/{event_id}/frame/{index}")
async def get_event_frame(event_id: str, index: int, user_id: str):
    for cam_dir in (STORAGE_ROOT / "users" / user_id / "cameras").glob("*"):
        events_dir = cam_dir / "events"
        event_file = events_dir / event_id / f"{event_id}.json"
        event = _load_json_safely(event_file)
        event_path = events_dir / event_id
        if not event:
            event_file = events_dir / f"{event_id}.json"
            event = _load_json_safely(event_file)
            event_path = events_dir / event_id
        if not event:
            continue
        event["_events_dir"] = str(event_path)
        event = _attach_event_package(event)
        frames = event.get("frames", [])
        if index < 0 or index >= len(frames):
            return Response(status_code=204)
        frame_file = event_path / "frames" / frames[index].get("file", "")
        if not frame_file.exists():
            frame_file = events_dir / "frames" / frames[index].get("file", "")
        if frame_file.exists():
            return Response(content=frame_file.read_bytes(), media_type="image/jpeg", headers={"Cache-Control": "no-store, no-cache, must-revalidate"})
    return Response(status_code=204)


@app.get("/api/events/{event_id}")
async def get_event(event_id: str, user_id: str):
    for cam_dir in (STORAGE_ROOT / "users" / user_id / "cameras").glob("*"):
        event_file = cam_dir / "events" / event_id / f"{event_id}.json"
        event = _load_json_safely(event_file)
        if event:
            event["camera_id"] = cam_dir.name
            event["_events_dir"] = str(cam_dir / "events" / event_id)
            event = _attach_event_package(event)
            cam_names = {}
            user_file = find_user_json(user_id)
            if user_file and user_file.exists():
                with open(user_file) as f:
                    ud = json.load(f)
                for cam in ud.get("cameras", []):
                    cam_names[cam.get("camera_id", "")] = cam.get("name", "")
            event = _enrich_event(event, user_id, cam_names, cam_dir.name)
            event.pop("_events_dir", None)
            return event
    for _cam_id, events_dir in resolve_user_events_dirs(user_id):
        ef = events_dir / f"{event_id}.json"
        if ef.exists():
            event = _load_json_safely(ef) or {}
            img_file = events_dir / f"{event_id}.jpg"
            event["_events_dir"] = str(events_dir)
            event = _attach_event_package(event)
            cam_names = {}
            user_file = find_user_json(user_id)
            if user_file and user_file.exists():
                with open(user_file) as f:
                    ud = json.load(f)
                for cam in ud.get("cameras", []):
                    cam_names[cam.get("camera_id", "")] = cam.get("name", "")
            event = _enrich_event(event, user_id, cam_names, _cam_id)
            event.pop("_events_dir", None)
            if img_file and img_file.exists():
                with open(img_file, "rb") as f:
                    event["frame_b64"] = base64.b64encode(f.read()).decode()
            grid_file = events_dir / f"{event_id}" / "grid.jpg"
            if grid_file.exists():
                with open(grid_file, "rb") as f:
                    event["grid_b64"] = base64.b64encode(f.read()).decode()
            return event
    raise HTTPException(status_code=404, detail="Event not found")


@app.get("/grid/latest")
async def get_latest_grid(partial: int = 1, camera_id: Optional[str] = None, user_id: Optional[str] = None):
    uid = user_id or ""
    cid = camera_id or ""
    info = {"frame_count": 0, "camera_ids": [], "grid_size": _get_grid_size_for_camera(uid, cid) if cid else 16}
    grid_img = b""
    for alias_id in _camera_alias_candidates(uid, cid):
        grid_size = _get_grid_size_for_camera(uid, alias_id)
        grid = orchestrator._get_grid(uid, alias_id, grid_size=grid_size)
        info = grid.get_grid_info()
        info["grid_size"] = grid.max_frames
        last_cam = grid.get_last_camera_id()
        if info["frame_count"] > 0 and (not cid or last_cam == alias_id):
            grid_img = grid.get_grid_image()
            break
    grid_b64 = base64.b64encode(grid_img).decode() if grid_img else ""
    return {
        "frames_used": info["frame_count"],
        "grid_size": info.get("grid_size", 16),
        "grid_b64": grid_b64,
        "camera_ids": info["camera_ids"],
        "partial": bool(partial)
    }

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
        name = data.get("name", "") or decoded.get("name", "")
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
            existing_plan = existing.get("plan", plan)
            existing_status = existing.get("status", "active")
            existing_plan_end = existing.get("plan_end", 0)
            existing_trial_end = existing.get("trial_end", None)
            existing_access_token = existing.get("access_token", "")
            existing_payments = existing.get("payments", [])
            existing_last_payment = existing.get("last_payment", None)
            existing_next_due = existing.get("next_due", 0)
        else:
            # No hay user.json — solo crear si vienen datos de registro completos
            if not name or not business_name:
                return JSONResponse(
                    status_code=403,
                    content={"success": False, "error": "not_registered", "message": "Usuario no registrado. Completa el registro."},
                    headers={"Access-Control-Allow-Origin": "*"}
                )
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

@app.delete("/admin/user/{uid}")
async def delete_user(uid: str):
    """Eliminar usuario de Firebase Auth y del storage."""
    try:
        auth.delete_user(uid)
    except Exception as e:
        return {"success": False, "error": f"Firebase delete error: {str(e)}"}
    try:
        user_path = STORAGE_ROOT / "users" / uid
        if user_path.exists():
            import shutil
            shutil.rmtree(user_path)
    except Exception as e:
        return {"success": False, "error": f"Storage delete error: {str(e)}"}
    return {"success": True, "message": f"Usuario {uid} eliminado"}

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
            "vigilance": data.get("vigilance_context", data.get("vigilance", {})),
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

def _parse_json_text(value):
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return {}
    text = value.strip()
    if not text:
        return {}
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text).strip()
    text = re.sub(r"\{[\s]*\.\.\.[\s]*\}", "{}", text)
    text = re.sub(r"\[[\s]*\.\.\.[\s]*\]", "[]", text)
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        pass
    match = re.search(r"\{[\s\S]*\}", text)
    if match:
        try:
            parsed = json.loads(match.group())
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            pass
    for key in ("summary", "description"):
        key_match = re.search(rf'"{key}"\s*:\s*"((?:\\.|[^"\\])*)"', text)
        if key_match:
            try:
                return {key: json.loads('"' + key_match.group(1) + '"')}
            except Exception:
                return {key: key_match.group(1)}
    return {}


def _event_qwen_dict(ev: dict) -> dict:
    meta = ev.get("metadata", {}) if isinstance(ev.get("metadata"), dict) else {}
    q = ev.get("qwen", {}) if isinstance(ev.get("qwen"), dict) else {}
    qj = ev.get("qwen_json", {}) if isinstance(ev.get("qwen_json"), dict) else {}
    parsed_desc = _parse_json_text(ev.get("description"))
    parsed_summary = _parse_json_text(ev.get("summary"))
    parsed_meta_desc = _parse_json_text(meta.get("qwen_analysis"))
    merged = {}
    for source in (parsed_desc, parsed_meta_desc, q, qj, parsed_summary):
        if isinstance(source, dict):
            merged.update(source)
    for key in ("summary", "description"):
        if isinstance(merged.get(key), str):
            nested = _parse_json_text(merged[key])
            if nested.get("summary"):
                merged[key] = nested["summary"]
            elif nested.get("description"):
                merged[key] = nested["description"]
    if "importance" in merged and "importancia" not in merged:
        merged["importancia"] = merged["importance"]
    return merged


def _enrich_description_from_metadata(ev: dict, desc: str, qj: dict) -> str:
    """Enriquecer descripción SOLO si Qwen no dio datos útiles.
    Si Qwen ya describió la escena, usar SOLO esa descripción.
    """
    # Si ya tenemos una descripción rica de Qwen (no genérica), usarla tal cual
    generic_markers = ["escena tranquila", "escena repetitiva", "sin personas", "ninguna persona",
                       "personas adicionales", "sin actividad sospechosa", "con ninguna actividad",
                       "brief scene description", "qwen no distinguió"]
    is_generic = any(m in desc.lower() for m in generic_markers)

    # Si la descripción es genérica/vacia, intentar enriquecer desde metadata
    if not desc or is_generic:
        metadata = ev.get("metadata", {}) if isinstance(ev.get("metadata"), dict) else {}
        yolo_classes = metadata.get("yolo_classes") or ev.get("yolo_classes") or []
        if isinstance(yolo_classes, str):
            yolo_classes = [c.strip() for c in yolo_classes.split(",") if c.strip()]
        total_yolo = int(metadata.get("total_yolo_objects") or ev.get("total_yolo_objects") or 0 or 0)
        person_count = sum(1 for c in yolo_classes if str(c).lower() == "person") if isinstance(yolo_classes, list) else 0
        if not person_count and total_yolo > 0:
            person_count = max(1, total_yolo)
        qj_details = qj.get("details") if isinstance(qj.get("details"), dict) else {}
        fallback_details = metadata.get("qwen_details") if isinstance(metadata.get("qwen_details"), dict) else {}
        if not qj_details and isinstance(fallback_details, dict):
            qj_details = fallback_details
        parts = []
        if person_count > 0 and not qj_details.get("persons_description"):
            parts.append(f"Se detectaron {person_count} persona(s).")
        if qj_details.get("persons_description"):
            parts.append(str(qj_details["persons_description"]))
        if qj_details.get("scene_context"):
            parts.append(str(qj_details["scene_context"]))
        for key in ("actions_visible", "objects_visible", "clothing_visible"):
            v = qj_details.get(key)
            if isinstance(v, list) and v:
                parts.append(", ".join(str(x) for x in v))
            elif v:
                parts.append(str(v))
        tags = qj.get("search_tags") or metadata.get("qwen_search_tags") or []
        if tags:
            parts.append(", ".join(str(t) for t in tags))
        detail_text = " ".join(parts).strip()
        if detail_text:
            return f"{detail_text} {desc}".strip() if desc and not is_generic else detail_text
        return "Sin descripción"

    # Si Qwen dio datos reales, usar SOLO esos datos — sin agregar basura técnica
    return desc.strip() or "Sin descripción"


def _event_description_simple(ev: dict) -> str:
    qj = _event_qwen_dict(ev)
    desc = (qj.get("summary") or qj.get("description") or ev.get("description") or
            ev.get("summary") or "")
    if isinstance(desc, dict):
        desc = json.dumps(desc, ensure_ascii=False)
    desc = str(desc).strip()
    return _enrich_description_from_metadata(ev, desc, qj)


def _event_description(ev: dict) -> str:
    qj = _event_qwen_dict(ev)
    desc = (qj.get("summary") or qj.get("description") or ev.get("description") or
            ev.get("summary") or "")
    if isinstance(desc, dict):
        desc = json.dumps(desc, ensure_ascii=False)
    desc = str(desc).strip()
    try:
        from orchestrator import _description_detail_parts, _is_generic_qwen_summary, _convert_qwen_vision_response
    except Exception:
        return _enrich_description_from_metadata(ev, desc, qj)
    # Convertir formato viejo (cajero/clientes/caja) a nuevo (persons/scene/objects)
    if "persons" not in qj:
        vision_legacy = qj.get("vision", {})
        if isinstance(vision_legacy, dict) and "cajero" in vision_legacy:
            converted = _convert_qwen_vision_response(vision_legacy)
            qj.update(converted)  # Añade scene, persons, objects a raíz de qj
    # Obtener v_summary: preferir scene convertido (más útil) sobre summary viejo
    v_summary = str(qj.get("summary", "") or "")
    v_scene = str(qj.get("scene", "") or "")
    if len(v_scene) > len(v_summary) and not _is_generic_qwen_summary(v_scene):
        v_summary = v_scene
    if not v_summary or len(v_summary) < 30:
        v_vision = qj.get("vision", {})
        if isinstance(v_vision, dict) and v_vision.get("summary"):
            v_summary = str(v_vision["summary"])
    vision_data = qj.get("vision", {})
    has_new_format = (
        isinstance(vision_data, dict) and 
        (vision_data.get("cliente", {}).get("presente") or vision_data.get("empleado", {}).get("presente"))
    )
    v_txn = vision_data.get("transaction", {}) if isinstance(vision_data, dict) else {}
    has_rich_data = (
        has_new_format or 
        (isinstance(v_txn, dict) and v_txn.get("active")) or
        (len(v_summary) > 60 and not _is_generic_qwen_summary(v_summary))
    )
    if has_rich_data and len(v_summary) > 20:
        return v_summary
    if len(v_summary) > 50 and not _is_generic_qwen_summary(v_summary):
        desc = v_summary
    else:
        desc = (qj.get("summary") or qj.get("description") or ev.get("description") or
                ev.get("summary") or "")
    detail_parts = _description_detail_parts(qj, ev)
    detail_text = " ".join(detail_parts).strip()
    if _is_generic_qwen_summary(desc) and detail_text:
        enriched = detail_text
    else:
        enriched = " ".join([x for x in [desc, detail_text] if x]).strip()
    if not enriched:
        return "Sin descripción"
    return enriched


def _event_violation(ev: dict) -> bool:
    meta = ev.get("metadata", {}) if isinstance(ev.get("metadata"), dict) else {}
    q = ev.get("qwen", {}) if isinstance(ev.get("qwen"), dict) else {}
    qj = _event_qwen_dict(ev)
    ev_type = ev.get("event_type", "")
    importancia = str(qj.get("importancia", "")).lower()
    anomalias = qj.get("anomalias", []) if isinstance(qj.get("anomalias"), list) else []
    high_severity = any(
        str(a.get("severidad") if isinstance(a, dict) else a).lower() in ("alta", "critica", "crítica")
        for a in anomalias
    )
    return ev_type in ("violation", "vigilance_alert", "night_alert") or importancia in ("alta", "critica") or high_severity or bool(q.get("violation") or meta.get("violation"))


def _event_yolo(ev: dict) -> dict:
    meta = ev.get("metadata", {}) if isinstance(ev.get("metadata"), dict) else {}
    y = ev.get("yolo", {}) if isinstance(ev.get("yolo"), dict) else {}
    classes = (ev.get("yolo_classes") or meta.get("yolo_classes") or y.get("classes") or [])
    if isinstance(classes, str):
        classes = [c.strip() for c in classes.split(",") if c.strip()]
    detections = y.get("detections", []) if isinstance(y.get("detections"), list) else []
    count = ev.get("total_yolo_objects") or meta.get("total_yolo_objects") or ev.get("yolo_count") or meta.get("yolo_count") or y.get("count") or (len(detections) if detections else 0)
    return {"count": count, "classes": classes, "detections": detections}


def _event_persons(ev: dict) -> int:
    qj = _event_qwen_dict(ev)
    details = qj.get("details", {}) if isinstance(qj.get("details"), dict) else {}
    for key in ("persons", "person_count", "persons_visible", "personas", "personas_contadas", "people_count"):
        try:
            value = details.get(key)
            if value is None:
                value = qj.get(key)
            if value is not None and int(value) > 0:
                return int(value)
        except Exception:
            pass
    metadata = ev.get("metadata", {}) if isinstance(ev.get("metadata"), dict) else {}
    yolo_classes = metadata.get("yolo_classes") or ev.get("yolo_classes") or []
    if isinstance(yolo_classes, str):
        yolo_classes = [c.strip() for c in yolo_classes.split(",") if c.strip()]
    if isinstance(yolo_classes, list):
        person_count = sum(1 for c in yolo_classes if str(c).lower() == "person")
        total_yolo = int(metadata.get("total_yolo_objects") or ev.get("total_yolo_objects") or ev.get("yolo_count") or 0 or 0)
        return max(person_count, 1) if person_count == 0 and total_yolo > 0 else person_count
    y = ev.get("yolo", {}) if isinstance(ev.get("yolo"), dict) else {}
    classes = (ev.get("yolo_classes") or (ev.get("metadata") or {}).get("yolo_classes") or y.get("classes") or [])
    if isinstance(classes, str):
        classes = [c.strip() for c in classes.split(",") if c.strip()]
    return classes.count("person")


def _attach_event_package(ev: dict) -> dict:
    events_dir = Path(ev.get("_events_dir", ""))
    event_id = ev.get("event_id", "")
    if not events_dir or not event_id:
        return ev
    package_dir = events_dir / event_id
    if package_dir.is_dir():
        mp4_file = package_dir / f"{event_id}.mp4"
        if mp4_file.exists():
            ev["video_file"] = f"{event_id}.mp4"
            ev["clip_type"] = "event_package"
        frames_dir = package_dir / "frames"
        if frames_dir.is_dir() and not ev.get("frames"):
            frames = []
            for fp in sorted(frames_dir.glob("frame_*.jpg")):
                try:
                    frames.append({
                        "index": len(frames),
                        "file": fp.name,
                        "size": fp.stat().st_size,
                        "timestamp": ev.get("timestamp", 0),
                        "datetime": ev.get("datetime", ""),
                    })
                except Exception:
                    pass
            if frames:
                ev["frames"] = frames
                ev["frames_count"] = len(frames)
                ev["clip_type"] = "event_package"
    else:
        legacy_frames_dir = events_dir / "frames"
        if legacy_frames_dir.is_dir() and not ev.get("frames"):
            frames = []
            for fp in sorted(legacy_frames_dir.glob("frame_*.jpg")):
                try:
                    frames.append({
                        "index": len(frames),
                        "file": fp.name,
                        "size": fp.stat().st_size,
                        "timestamp": ev.get("timestamp", 0),
                        "datetime": ev.get("datetime", ""),
                    })
                except Exception:
                    pass
            if frames:
                ev["frames"] = frames
                ev["frames_count"] = len(frames)
                ev["clip_type"] = "event_package"
    return ev


def _enrich_event(ev: dict, user_id: str, cam_names: dict, cam_id: str) -> dict:
    from orchestrator import _convert_qwen_vision_response
    ev = _attach_event_package(ev)
    cid = ev.get("camera_id", "")
    ev["camera_name"] = cam_names.get(cid, cam_id if cam_id != "_global" else "Camara")
    desc = _event_description(ev)
    is_violation = _event_violation(ev)
    yolo = _event_yolo(ev)
    persons = _event_persons(ev)
    
    # Filtrar objetos irrelevantes del vision
    qj_merged = _event_qwen_dict(ev)
    # Convertir formato viejo si es necesario
    if "persons" not in qj_merged:
        vision_in_qj = qj_merged.get("vision", {})
        if isinstance(vision_in_qj, dict) and "cajero" in vision_in_qj:
            converted = _convert_qwen_vision_response(vision_in_qj)
            qj_merged.update(converted)
    vision_raw = qj_merged.get("vision", {})
    if not isinstance(vision_raw, dict):
        vision_raw = {}
    # Buscar persons/scene/objects en vision o en raíz (nuevo formato Qwen)
    raw_persons = vision_raw.get("persons", []) or qj_merged.get("persons", [])
    raw_objects = vision_raw.get("objects", []) or qj_merged.get("objects", [])
    raw_scene = vision_raw.get("scene", "") or qj_merged.get("scene", "")
    raw_cliente = vision_raw.get("cliente") or qj_merged.get("cliente")
    raw_empleado = vision_raw.get("empleado") or qj_merged.get("empleado")
    raw_resumen = vision_raw.get("resumen") or qj_merged.get("resumen") or ""
    irrelevant_keywords = {"silla", "mesa", "cable", "lámpara", "ventilador", "pared", "paredes", "techo", "cielo", "ventana", "puerta", "piso", "alfombra", "cortina", "planta", "florero", "adorno", "cuadro", "espejo", "monitor", "pantalla", "teclado", "computadora"}
    vision = {
        "persons": raw_persons if isinstance(raw_persons, list) else [],
        "objects": [o for o in (raw_objects if isinstance(raw_objects, list) else []) if o and not any(ik in str(o).lower() for ik in irrelevant_keywords)],
        "scene": raw_scene if isinstance(raw_scene, str) else "",
        "cliente": raw_cliente if isinstance(raw_cliente, dict) else None,
        "empleado": raw_empleado if isinstance(raw_empleado, dict) else None,
        "resumen": raw_resumen if isinstance(raw_resumen, str) else "",
    }
    
    ev["description"] = desc
    # Filtrar search_tags
    raw_tags = _event_qwen_dict(ev).get("search_tags", []) or []
    search_tags = [t for t in raw_tags if t and not any(ik in str(t).lower() for ik in irrelevant_keywords)]
    ev["qwen_analysis"] = {
        "description": desc,
        "summary": desc,
        "details": {},
        "anomalias": _event_qwen_dict(ev).get("anomalias", []),
        "importancia": _event_qwen_dict(ev).get("importancia", "baja"),
        "persons": persons,
        "yolo": yolo,
        "vision": vision,
        "resumen": vision.get("resumen", ""),
        "rule_checks": _event_qwen_dict(ev).get("rule_checks", {}),
        "search_tags": search_tags,
        "violation": is_violation,
    }
    ev["qwen"] = {"violation": is_violation, "description": desc}
    ev["yolo"] = yolo
    ev["persons"] = persons
    if "metadata" in ev and isinstance(ev["metadata"], dict) and "grid_b64" in ev["metadata"]:
        del ev["metadata"]["grid_b64"]
    events_dir = Path(ev.get("_events_dir", ""))
    img_path = events_dir / f"{ev.get('event_id', '')}.jpg" if events_dir else Path()
    if img_path.exists():
        try:
            from gateway_resize import resize_image
            with open(img_path, "rb") as f:
                img_bytes = f.read()
            resized = resize_image(img_bytes, max_size=128)
            ev["frame_b64"] = base64.b64encode(resized).decode()
        except Exception:
            pass
    if not ev.get("frame_url") and ev.get("frames_count", 0) > 0:
        ev["frame_url"] = f"/api/events/{ev.get('event_id', '')}/frame/0?user_id={user_id}"
    ev["thumb_url"] = f"https://api.ojoia.com.do/api/event-thumb/{ev.get('event_id', '')}?user_id={user_id}"
    grid_path = events_dir / f"{ev.get('event_id', '')}" / "grid.jpg"
    if grid_path.exists():
        ev["grid_url"] = f"/api/events/{ev.get('event_id', '')}/grid?user_id={user_id}"
    return ev


@app.get("/api/user/events")
async def get_user_events(user_id: str, date: str = None, filter: str = None, limit: int = 50, offset: int = 0):
    raw_events = []
    start_of_today = int(datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    recent_start = int(time.time()) - (24 * 60 * 60)
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
        for fname in sorted(events_dir.iterdir(), key=lambda x: (_load_json_safely(x).get("timestamp", 0) if x.name.endswith(".json") else 0), reverse=True):
            if not fname.name.endswith(".json"):
                continue
            try:
                with open(fname) as f:
                    ev = json.load(f)
            except Exception:
                continue
            ev["_events_dir"] = str(events_dir)
            if date == "hoy" or filter in ("today", "recent"):
                min_ts = recent_start if filter in ("today", "recent") else start_of_today
                if ev.get("timestamp", 0) < min_ts:
                    continue
            if filter == "alerts" and not _event_violation(ev):
                continue
            raw_events.append(ev)
    raw_events.sort(key=lambda e: e.get("timestamp", 0), reverse=True)
    total = len(raw_events)
    events = []
    for ev in raw_events[offset:offset + limit]:
        events.append(_enrich_event(ev, user_id, cam_names, ev.get("_events_dir", "").split("/")[-2]))
    for ev in events:
        ev.pop("_events_dir", None)
    return {"events": events, "limit": limit, "offset": offset, "total": total}


@app.get("/api/event-thumb/{event_id}")
@app.get("/api/thumb/{event_id}")
async def get_event_thumb(event_id: str, user_id: str = None):
    """Servir miniatura de un evento"""
    from PIL import Image as PILImage
    import io
    base = Path(STORAGE_ROOT) / "users"
    search_dirs = [base / user_id / "cameras"] if user_id else [d / "cameras" for d in base.iterdir() if d.is_dir()]
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


@app.get("/api/event-frame/{event_id}")
async def get_event_frame(event_id: str, user_id: str = None):
    """Servir frame completo de un evento para Eva OS."""
    base = Path(STORAGE_ROOT) / "users"
    search_dirs = [base / user_id / "cameras"] if user_id else [d / "cameras" for d in base.iterdir() if d.is_dir()]
    for cam_base in search_dirs:
        if not cam_base.exists():
            continue
        for cam_dir in cam_base.iterdir():
            if not cam_dir.is_dir():
                continue
            img_file = cam_dir / "events" / f"{event_id}.jpg"
            if img_file.exists():
                return FileResponse(str(img_file), media_type="image/jpeg", headers={"Cache-Control": "max-age=3600"})
    raise HTTPException(status_code=404, detail="Image not found")

@app.get("/api/events/{event_id}")
async def get_event_detail(event_id: str, user_id: str):
    event_file = None
    img_file = None
    events_dir_name = ""
    for _cam_id, events_dir in resolve_user_events_dirs(user_id):
        ef = events_dir / f"{event_id}.json"
        if ef.exists():
            event_file = ef
            img_file = events_dir / f"{event_id}.jpg"
            events_dir_name = _cam_id
            break
    if not event_file or not event_file.exists():
        raise HTTPException(status_code=404, detail="Evento no encontrado")
    with open(event_file) as f:
        event = json.load(f)
    grid_b64 = event.get("metadata", {}).get("grid_b64", "") if isinstance(event.get("metadata"), dict) else ""
    event["_events_dir"] = str(event_file.parent)
    cam_names = {}
    user_file = find_user_json(user_id)
    if user_file and user_file.exists():
        with open(user_file) as f:
            ud = json.load(f)
        for cam in ud.get("cameras", []):
            cam_names[cam.get("camera_id", "")] = cam.get("name", "")
    event = _enrich_event(event, user_id, cam_names, events_dir_name)
    event.pop("_events_dir", None)
    if img_file and img_file.exists():
        with open(img_file, "rb") as f:
            event["frame_b64"] = base64.b64encode(f.read()).decode()
    event["grid_b64"] = grid_b64
    return event

def _camera_metrics(user_id: str, camera_id: str) -> dict:
    now = int(time.time())
    start_of_today = int(datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    events_dirs = [STORAGE_ROOT / "users" / user_id / "cameras" / camera_id / "events"]
    for _cam_id, events_dir in resolve_user_events_dirs(user_id):
        if _cam_id == camera_id:
            events_dirs.append(events_dir)
    total = 0
    today = 0
    alerts = 0
    seen = set()
    for events_dir in events_dirs:
        key = str(events_dir.resolve()) if events_dir.exists() else str(events_dir)
        if key in seen:
            continue
        seen.add(key)
        if not events_dir.exists():
            continue
        for fname in events_dir.glob("*.json"):
            try:
                ev = _load_json_safely(fname) or {}
            except Exception:
                continue
            total += 1
            ts = int(ev.get("timestamp", 0) or 0)
            if ts >= start_of_today:
                today += 1
            if _event_violation(ev):
                alerts += 1
    return {"total_events": total, "total_alerts": alerts, "today_events": today, "today_alerts": alerts, "rules_count": 0}


def _event_is_alert(ev: dict) -> bool:
    return _event_violation(ev)


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
        for fname in sorted(events_dir.iterdir(), key=lambda x: (_load_json_safely(x).get("timestamp", 0) if x.name.endswith(".json") else 0), reverse=True):
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
            cam_cfg = get_camera_config_static(user_id, cam.get("camera_id", ""))
            if cam_cfg:
                cam_cfg = normalize_camera_vigilance_config(cam_cfg)
                cam_copy["schedule"] = cam_cfg.get("schedule", {})
                cam_copy["vigilance"] = cam_cfg.get("vigilance", {})
                cam_copy["current_mode"] = _get_current_mode(cam_cfg.get("schedule", {}), cam_cfg.get("vigilance", {}))
            # Determine online status dynamically
            # Camera is online ONLY if it has announced OR sent a frame within last 2 min
            last_announce = cam.get("last_announce") or 0
            last_frame = cam.get("last_frame") or 0
            is_online = False
            if last_announce and (now - last_announce) < CAMERA_ONLINE_GRACE_SECONDS:
                is_online = True
            if last_frame and (now - last_frame) < CAMERA_ONLINE_GRACE_SECONDS:
                is_online = True
            cam_copy["active"] = is_online
            # Age info for frontend
            cam_copy["announce_age"] = int(now - last_announce) if last_announce else None
            cam_copy["frame_age"] = int(now - last_frame) if last_frame else None
            cam_copy["metrics"] = _camera_metrics(user_id, cam.get("camera_id", ""))
            result.append(cam_copy)
        return {"cameras": result}
    return {"cameras": []}

# ── Proxy ESP32 (LED, calidad, rotación) ─────────────────────────────────────

from typing import Optional as _Optional

@app.post("/cameras/{camera_id}/cmd", include_in_schema=False)
async def cam_cmd(camera_id: str, request: dict = None, user_id: str = None):
    """Proxy de comandos al ESP32 local."""
    cors_headers = {"Access-Control-Allow-Origin": "*"}
    try:
        body = request or {}
        target_ip = None
        user_file = None
        users_dir = STORAGE_ROOT / "users"
        if user_id and user_id != "default":
            uf = find_user_json(user_id)
            candidate_users = [uf] if uf and uf.exists() else []
        else:
            candidate_users = [d / "user.json" for d in users_dir.iterdir() if d.is_dir()] if users_dir.is_dir() else []
        for uf in candidate_users:
            if not uf or not uf.exists():
                continue
            try:
                with open(uf) as f:
                    ud = json.load(f)
                for c in ud.get("cameras", []):
                    if c.get("camera_id") == camera_id or c.get("physical_camera_id") == camera_id:
                        target_ip = c.get("last_announce_ip") or ""
                        user_file = uf
                        break
            except Exception:
                pass
            if target_ip:
                break
        if not target_ip and user_file and user_file.exists():
            try:
                with open(user_file) as f:
                    ud = json.load(f)
                ud.setdefault("pending_commands", {})[camera_id] = {
                    "body": body,
                    "created": int(time.time())
                }
                with open(user_file, "w") as f:
                    json.dump(ud, f, indent=2)
                return JSONResponse(content={"ok": True, "queued": True, "detail": "Comando en cola: no encontré IP directa de la cámara"}, headers=cors_headers)
            except Exception as qe:
                return JSONResponse(content={"ok": False, "error": "No encontré IP de la cámara y falló la cola", "detail": str(qe)}, headers=cors_headers)
        import httpx
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(10.0, connect=5.0, read=5.0, write=5.0),
                headers={"Connection": "close"},
                http1=True,
                http2=False
            ) as client:
                resp = await client.post(f"http://{target_ip}:81/config", json=body)
            data = {}
            try:
                data = resp.json()
                if not isinstance(data, dict):
                    data = {"ok": True}
            except Exception:
                data = {"ok": resp.status_code < 400}
            if resp.status_code >= 400:
                return JSONResponse(status_code=resp.status_code, content={"ok": False, "detail": data.get("error") or data.get("detail") or f"Error cámara {resp.status_code}"}, headers=cors_headers)
            return JSONResponse(status_code=resp.status_code, content={"ok": True, **data}, headers=cors_headers)
        except (httpx.ConnectError, httpx.ReadError, httpx.WriteError, httpx.RemoteProtocolError, httpx.TimeoutException, httpx.InvalidURL) as e:
            if user_file and user_file.exists():
                try:
                    with open(user_file) as f:
                        ud = json.load(f)
                    ud.setdefault("pending_commands", {})[camera_id] = {
                        "body": body,
                        "created": int(time.time())
                    }
                    with open(user_file, "w") as f:
                        json.dump(ud, f, indent=2)
                    return JSONResponse(content={"ok": True, "queued": True, "detail": "Comando en cola para la cámara"}, headers=cors_headers)
                except Exception as qe:
                    return JSONResponse(content={"ok": False, "error": "No se pudo conectar con la cámara local y falló la cola", "detail": str(qe)}, headers=cors_headers)
            return JSONResponse(content={"ok": False, "error": "No se pudo conectar con la cámara local", "detail": str(e)}, headers=cors_headers)
    except Exception as e:
        return JSONResponse(status_code=502, content={"ok": False, "error": str(e)}, headers=cors_headers)


@app.get("/camera/config/{camera_id}", include_in_schema=False)
async def camera_poll_config(camera_id: str):
    """Comandos en cola para el ESP32. El firmware llama esto periódicamente."""
    cors_headers = {"Access-Control-Allow-Origin": "*"}
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
                    if c.get("camera_id") == camera_id or c.get("physical_camera_id") == camera_id:
                        pending_key = c.get("camera_id") or camera_id
                        pending = ud.get("pending_commands", {})
                        cmd = pending.pop(pending_key, None) or pending.pop(camera_id, None)
                        if cmd:
                            created = cmd.get("created") or 0
                            if int(time.time()) - int(created) > 600:
                                with open(uf, "w") as f:
                                    json.dump(ud, f, indent=2)
                                return JSONResponse(content={}, headers=cors_headers)
                            with open(uf, "w") as f:
                                json.dump(ud, f, indent=2)
                            return JSONResponse(content=cmd.get("body", {}), headers=cors_headers)
            except Exception:
                continue
    return JSONResponse(content={}, headers=cors_headers)


@app.get("/ota/check/{camera_id}", include_in_schema=False)
async def ota_check(camera_id: str, request: Request):
    uid, canonical = find_camera_owner(camera_id)
    if not uid:
        return {"update_available": False, "firmware_version": FIRMWARE_VERSION}
    base = f"{request.url.scheme}://{request.url.netloc}"
    size = FIRMWARE_BIN.stat().st_size if FIRMWARE_BIN.exists() else 0
    return {
        "update_available": True,
        "firmware_version": FIRMWARE_VERSION,
        "firmware_url": f"{base}/ota/firmware.bin",
        "firmware_size": size,
        "camera_id": canonical,
        "note": "Actualización LED automática disponible."
    }


@app.get("/ota/firmware.bin", include_in_schema=False)
async def ota_firmware():
    if not FIRMWARE_BIN.exists():
        raise HTTPException(status_code=404, detail="Firmware no encontrado")
    digest = hashlib.sha256(FIRMWARE_BIN.read_bytes()).hexdigest()
    headers = {"X-Firmware-Version": FIRMWARE_VERSION, "X-Firmware-SHA256": digest}
    return FileResponse(FIRMWARE_BIN, media_type="application/octet-stream", filename="firmware.bin", headers=headers)


# ── Face ID ─────────────────────────────────────────────────────────
from face_pipeline import (
    identify_from_frame, identify_face, register_face as _fp_register_face, list_employees,
    extract_face_from_frame, crop_face_from_frame, FACES_DIR,
)


@app.post("/api/identity/register")
async def identity_register(request: dict):
    user_id = request.get("user_id", "")
    person_name = request.get("person_name", "")
    image_b64 = request.get("image_b64", "")
    if not user_id or not person_name or not image_b64:
        raise HTTPException(400, "user_id, person_name and image_b64 required")
    import base64, tempfile
    img_bytes = base64.b64decode(image_b64)
    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
        tmp.write(img_bytes)
        tmp_path = tmp.name
    result = _fp_register_face(user_id, person_name, tmp_path)
    if result is None:
        raise HTTPException(422, "No face detected in image")
    return {"success": True, "person": result}


@app.post("/api/identity/identify")
async def identity_identify(request: dict):
    user_id = request.get("user_id", "")
    image_b64 = request.get("image_b64", "")
    threshold = float(request.get("threshold", 0.45))
    if not user_id or not image_b64:
        raise HTTPException(400, "user_id and image_b64 required")
    import base64
    img_bytes = base64.b64decode(image_b64)
    results = identify_from_frame(img_bytes, user_id, threshold)
    return {"identified": results, "count": len(results)}


@app.get("/api/identity/employees")
async def identity_employees(user_id: str = ""):
    if not user_id:
        raise HTTPException(400, "user_id required")
    employees = list_employees(user_id)
    return {"employees": employees, "count": len(employees)}


@app.post("/api/identity/identify-frame")
async def identity_identify_frame(request: dict):
    user_id = request.get("user_id", "")
    frame_b64 = request.get("frame_b64", "")
    threshold = float(request.get("threshold", 0.45))
    if not user_id or not frame_b64:
        raise HTTPException(400, "user_id and frame_b64 required")
    import base64
    frame_bytes = base64.b64decode(frame_b64)
    results = identify_from_frame(frame_bytes, user_id, threshold)
    return {"identified": results, "count": len(results)}


@app.get("/api/identity/faces/{user_id}/{person_id}.jpg")
async def identity_face_image(user_id: str, person_id: str):
    img_path = FACES_DIR / user_id / person_id / "face_registered.jpg"
    if not img_path.exists():
        img_path = FACES_DIR / person_id / "face_registered.jpg"
    if not img_path.exists():
        raise HTTPException(404, "Face image not found")
    return FileResponse(str(img_path), media_type="image/jpeg")


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
            cam_cfg = get_camera_config_static(user_id, camera_id)
            if not cam_cfg:
                raise HTTPException(status_code=404, detail="Camara no encontrada")
            result = dict(c)
            raw_prompt = cam_cfg.get("system_prompt", ud.get("vigilance_prompt", ""))
            result["system_prompt"] = _normalize_system_prompt(user_id, camera_id, raw_prompt)
            result["rules"] = cam_cfg.get("rules", [])
            result["rules_es"] = cam_cfg.get("rules_es", [])
            result["yolo_triggers"] = cam_cfg.get("yolo_triggers", ["person"])
            result["vigilance"] = cam_cfg.get("vigilance", {})
            result["current_mode"] = _get_current_mode(cam_cfg.get("schedule", {}), cam_cfg.get("vigilance", {}))
            now = time.time()
            last_announce = result.get("last_announce", 0) or 0
            last_frame = result.get("last_frame", 0) or 0
            announce_age = now - last_announce if last_announce else None
            frame_age = now - last_frame if last_frame else None
            is_online = False
            if announce_age is not None and announce_age < CAMERA_ONLINE_GRACE_SECONDS:
                is_online = True
            if frame_age is not None and frame_age < CAMERA_ONLINE_GRACE_SECONDS:
                is_online = True
            result["active"] = is_online
            result["metrics"] = _camera_metrics(user_id, camera_id)
            return result
    raise HTTPException(status_code=404, detail="Camara no encontrada")


@app.get("/api/cameras/{camera_id}/vigilance")
async def get_camera_vigilance(camera_id: str, user_id: str):
    cam_cfg = get_camera_config_static(user_id, camera_id)
    if not cam_cfg:
        raise HTTPException(status_code=404, detail="Camara no encontrada")
    mode = _get_current_mode(cam_cfg.get("schedule", {}), cam_cfg.get("vigilance", {}))
    return {
        "success": True,
        "camera_id": camera_id,
        "mode": mode,
        "system_prompt": cam_cfg.get("system_prompt", ""),
        "vigilance": cam_cfg.get("vigilance", {}),
        "schedule": cam_cfg.get("schedule", {}),
    }


@app.put("/api/cameras/{camera_id}/grid-size")
async def update_camera_grid_size(camera_id: str, user_id: str, request: Request):
    data = await request.json()
    raw_size = data.get("grid_size") or data.get("size") or 16
    try:
        size = int(raw_size)
    except Exception:
        raise HTTPException(status_code=400, detail="grid_size debe ser un número")
    size = 16 if size >= 16 else 12 if size >= 12 else 8

    cam_cfg = get_camera_config_static(user_id, camera_id)
    if not cam_cfg:
        raise HTTPException(status_code=404, detail="Camara no encontrada")

    cam_cfg["grid_size"] = size
    _save_camera_config_static(user_id, camera_id, cam_cfg)
    orchestrator._cleanup_grid(user_id, camera_id)
    return {"success": True, "camera_id": camera_id, "grid_size": size}


@app.post("/api/cameras/{camera_id}/vigilance/test")
async def test_camera_vigilance(camera_id: str, user_id: str, request: Request):
    try:
        cam_cfg = normalize_camera_vigilance_config(get_camera_config_static(user_id, camera_id))
        if not cam_cfg:
            raise HTTPException(status_code=404, detail="Camara no encontrada")
        mode = _get_current_mode(cam_cfg.get("schedule", {}), cam_cfg.get("vigilance", {}))
        prompt = build_vigilance_prompt(cam_cfg, mode)
        frame = await get_latest_frame(camera_id=camera_id, user_id=user_id)
        if not frame.get("success") or not frame.get("image_b64"):
            return {
                "success": False,
                "error": "No hay último frame disponible para esta cámara",
                "mode": mode,
                "prompt": prompt,
            }
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                "http://localhost:8004/v1/chat/completions",
                json={
                    "model": "qwen",
                    "messages": [{"role": "user", "content": [
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{frame['image_b64']}"}},
                        {"type": "text", "text": f"{prompt}\n\nAnaliza esta imagen y responde SOLO JSON con violation, mode, summary, details, anomalias, importance, evidence."}
                    ]}],
                    "max_tokens": 500,
                },
            )
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"]
        parsed = orchestrator._parse_qwen_json(content)
        importance = parsed.get("importance") or parsed.get("importancia") or "normal"
        violation = bool(parsed.get("violation")) or importance in ("alta", "critica")
        return {
            "success": True,
            "mode": mode,
            "prompt": prompt,
            "raw": content,
            "parsed": parsed,
            "summary": parsed.get("summary", ""),
            "importance": importance,
            "violation": violation,
        }
    except httpx.HTTPStatusError as e:
        return {"success": False, "error": f"Qwen respondió con error: {e.response.status_code}", "mode": mode if 'mode' in locals() else None}
    except httpx.ConnectError:
        return {"success": False, "error": "No se pudo conectar con Qwen en localhost:8004", "mode": mode if 'mode' in locals() else None}
    except Exception as e:
        logger.error(f"Vigilance test error: {e}")
        return {"success": False, "error": str(e), "mode": mode if 'mode' in locals() else None}


@app.get("/api/cameras/{camera_id}/grid")
async def get_camera_grid(camera_id: str, user_id: Optional[str] = None):
    uid = user_id or ""
    frame_bytes = b""
    yolo_count = 0
    info = {"frame_count": 0, "camera_ids": [], "grid_size": _get_grid_size_for_camera(uid, camera_id)}
    for alias_id in _camera_alias_candidates(uid, camera_id):
        grid_size = _get_grid_size_for_camera(uid, alias_id)
        grid = orchestrator._get_grid(uid, alias_id, grid_size=grid_size)
        info = grid.get_grid_info()
        info["grid_size"] = grid.max_frames
        last_cam = grid.get_last_camera_id()
        fb = grid.get_last_frame_bytes() if last_cam == alias_id else b""
        if fb:
            frame_bytes = fb
            yolo_count = grid.get_last_yolo_count()
            break
    image_b64 = base64.b64encode(frame_bytes).decode() if frame_bytes else ""
    return {
        "success": True,
        "camera_id": camera_id,
        "image_b64": image_b64,
        "yolo": {"count": yolo_count},
        "frames_used": info["frame_count"],
        "grid_size": info.get("grid_size", 16),
        "camera_ids": info["camera_ids"]
    }

# Ingesta de Frames (ESP32-CAM)
@app.post("/ingest/test")
async def ingest_test(camera_id: str = Form("test"), user_id: str = Form("test")):
    """Endpoint de prueba para verificar que el routing funciona."""
    return {"success": True, "camera_id": camera_id, "user_id": user_id, "message": "OK"}

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

async def _pending_setup_camera_id(user_id: str) -> str:
    try:
        from eva.eva_v2 import _sessions
        for s in _sessions.values():
            if s.get("user_id") != user_id:
                continue
            if s.get("phase") in {"done", "os"}:
                continue
            if s.get("camera_id"):
                return s.get("camera_id")
    except Exception:
        pass
    try:
        best = None
        best_time = 0.0
        for sf in STORAGE_ROOT.glob("users/*/cameras/*/eva_session_v2.json"):
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
            if not d.get("camera_id"):
                continue
            t = float(d.get("created_at", 0) or sf.stat().st_mtime or 0)
            if t >= best_time:
                best_time = t
                best = d
        if best and best.get("camera_id"):
            return best.get("camera_id")
    except Exception:
        pass
    return ""

async def _has_pending_new_camera_setup(user_id: str) -> bool:
    return bool(await _pending_setup_camera_id(user_id))

def _pending_camera_id_for_ip(client_ip: str) -> str:
    safe = (client_ip or "unknown").replace(".", "_").replace(":", "_")
    return f"pending_{safe}"

def _resolve_unknown_camera(user_id: str, client_ip: str) -> str:
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


def _image_avg_brightness(img_bytes: bytes) -> float:
    try:
        from PIL import Image, ImageStat
        import io
        img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
        stat = ImageStat.Stat(img)
        return float(sum(stat.mean[:3]) / 3)
    except Exception:
        return 255.0


async def _queue_led_auto_if_dark(user_id: str, camera_id: str, img_bytes: bytes, led_header: str = ""):
    """Si el frame llega oscuro y el LED está apagado, poner la cámara en LED automático."""
    led_text = str(led_header or "").lower()
    if led_text in ("1", "true", "on"):
        return
    avg = _image_avg_brightness(img_bytes)
    if avg >= 55:
        return
    now = time.time()
    last = _last_led_auto_ts_by_camera.get(camera_id, 0)
    if now - last < 30:
        return
    _last_led_auto_ts_by_camera[camera_id] = now
    uf = find_user_json(user_id)
    if not uf or not uf.exists():
        logger.warning(f"LED auto skipped: no user.json for {user_id}")
        return
    try:
        with open(uf) as f:
            ud = json.load(f)
        pending = ud.setdefault("pending_commands", {})
        pending[camera_id] = {
            "body": {"led_auto": True, "led_bright": 255, "led_force_on": True},
            "created": int(now),
            "source": "backend_dark_frame"
        }
        with open(uf, "w") as f:
            json.dump(ud, f, indent=2)
        logger.warning(f"LED auto queued for {camera_id}: avg_brightness={avg:.1f}")
    except Exception as e:
        logger.warning(f"LED auto queue failed for {camera_id}: {e}")


async def _process_ingest(request: Request, camera_id: str, user_id: str, image: UploadFile):
    """Flujo completo: ESP32 -> YOLO gate -> Grid -> Qwen -> Evento"""
    try:
        client_ip = request.client.host if request.client else "unknown"
        user_id = resolve_user_id(camera_id, user_id, client_ip)
        pending_setup = await _has_pending_new_camera_setup(user_id)
        pending_camera_id = await _pending_setup_camera_id(user_id) if pending_setup else ""
        if pending_setup:
            if camera_id == "unknown":
                camera_id = pending_camera_id or _pending_camera_id_for_ip(client_ip)
            elif camera_id.startswith("OJO-"):
                camera_id = pending_camera_id or camera_id
        else:
            user_id, camera_id = _resolve_camera_alias(user_id, camera_id, client_ip)
        if not pending_setup and camera_id == "unknown":
            camera_id = await _resolve_unknown_camera(user_id, client_ip)
        if not user_id or user_id == "default":
            owner_uid, owner_cam = find_camera_owner(camera_id)
            if owner_uid:
                user_id = owner_uid
                camera_id = owner_cam
        pending_command = pop_pending_camera_command(user_id, camera_id)
        command_response = {"command": pending_command} if pending_command else {}

        img_bytes = await image.read()
        frame_size = len(img_bytes)
        logger.info(f"ESP32: quality={request.headers.get('x-quality')} framesize={request.headers.get('x-framesize')} led={request.headers.get('x-led')} mirror={request.headers.get('x-hmirror')} flip={request.headers.get('x-vflip')}")
        logger.info(f"Frame: IP={client_ip} Cam={camera_id} User={user_id} Size={frame_size}B")
        await _queue_led_auto_if_dark(user_id, camera_id, img_bytes, request.headers.get('x-led', ''))

        try:
            frames_dir = STORAGE_ROOT / "users" / user_id / "cameras" / camera_id / "frames"
            frames_dir.mkdir(parents=True, exist_ok=True)
            with open(frames_dir / "latest_raw.jpg", "wb") as f:
                f.write(img_bytes)
            _save_recent_frame(user_id, camera_id, img_bytes)
        except Exception as e:
            logger.debug(f"raw frame cache error: {e}")

        # 1. Leer config de la cámara
        cam_cfg = normalize_camera_vigilance_config(get_camera_config_static(user_id, camera_id))
        _save_camera_config_static(user_id, camera_id, cam_cfg)
        yolo_triggers = cam_cfg.get("yolo_triggers", ["person"])
        vigilance = cam_cfg.get("vigilance", {})
        schedule = cam_cfg.get("schedule", {})

        # 2. Determinar modo: normal o centinela
        now_dt = datetime.now()
        current_time = now_dt.strftime("%H:%M")
        current_mode = _get_current_mode(schedule, vigilance, current_time)
        is_vigilante = current_mode == "sentinel"
        logger.info(f"Mode: {'CENTINELA' if is_vigilante else 'NORMAL'} | Time: {current_time}")

        grid_result = {"frame_count": 0, "grid_full": False, "ready_for_analysis": False}
        yolo_interval = max(1, int(cam_cfg.get("yolo_interval_sec", 3) or 3))
        yolo_key = f"{user_id}:{camera_id}"
        now_ts = time.time()
        yolo_skipped = False
        if now_ts - _last_yolo_ts_by_camera.get(yolo_key, 0) < yolo_interval:
            yolo_skipped = True
            _update_camera_last_frame(user_id, camera_id)
            try:
                from eva.eva_v2 import ingest_frame_for_eva
                ingest_frame_for_eva(img_bytes, camera_id)
            except Exception as e:
                logger.debug(f"eva frame buffer skipped-yolo: {e}")
            return {
                "success": True, "camera_id": camera_id, "user_id": user_id,
                "client_ip": client_ip, "frame_size": frame_size,
                "mode": "centinela" if is_vigilante else "normal", "timestamp": now_dt.isoformat(),
                "yolo": _last_yolo_by_camera.get(camera_id, {"count": 0, "classes": [], "detections": []}),
                "yolo_skipped": True, "grid": grid_result,
                **command_response
            }

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
                    params={"camera_id": camera_id},
                    files={"image": ("frame.jpg", yolo_bytes, "image/jpeg")},
                )
                if yolo_resp.status_code == 200:
                    yolo_data = yolo_resp.json()
                    raw_detections = yolo_data.get("detections", [])
                    yolo_detections = raw_detections
                    relevant_detections = _filter_yolo_detections(
                        raw_detections,
                        yolo_triggers=yolo_triggers,
                        sentinel=is_vigilante
                    )
                    for d in relevant_detections:
                        if d.get("confidence", 0) >= 0.25:
                            yolo_classes.append(d.get("class", ""))
                    yolo_count = len(yolo_classes)
                    _last_yolo_ts_by_camera[yolo_key] = now_ts
                    _last_yolo_by_camera[camera_id] = {
                        "count": yolo_count,
                        "classes": yolo_classes,
                        "detections": relevant_detections,
                        "raw_count": yolo_data.get("count", len(raw_detections)),
                        "raw_classes": [d.get("class", "") for d in raw_detections],
                        "raw_detections": raw_detections,
                        "timestamp": int(now_ts)
                    }
                    logger.warning(f"YOLO_DEBUG: raw_count={yolo_data.get('count')} filtered_count={yolo_count} classes={yolo_classes} all_dets={[(d.get('class'),d.get('confidence')) for d in raw_detections]}")
        except Exception as e:
            logger.warning(f"YOLO unavailable: {e}")
            _last_yolo_ts_by_camera[yolo_key] = now_ts
            _last_yolo_by_camera[camera_id] = {
                "count": 0,
                "classes": [],
                "detections": [],
                "timestamp": int(now_ts),
                "error": str(e)
            }

        # 4. YOLO GATE: si no hay objetos relevantes, NO pasar al grid
        if yolo_count == 0:
            # YOLO no detectó nada relevante - frame NO va al grid, pero guardar para Eva
            if is_vigilante:
                cached = _last_yolo_by_camera.get(camera_id, {})
                if cached.get("raw_count", 0) > 0:
                    logger.info(f"CENTINELA ignored non-person YOLO classes: {cached.get('raw_classes', [])}")
            logger.info(f"YOLO gate: 0 relevant objects → frame REJECTED (not added to grid)")
            _update_camera_last_frame(user_id, camera_id)
            # Guardar frame crudo para que Eva pueda verlo durante setup
            try:
                frames_dir = STORAGE_ROOT / "users" / user_id / "cameras" / camera_id / "frames"
                frames_dir.mkdir(parents=True, exist_ok=True)
                with open(frames_dir / "latest_raw.jpg", "wb") as f:
                    f.write(img_bytes)
                _save_recent_frame(user_id, camera_id, img_bytes)
                # También guardar en buffer de Eva
                from eva.eva_v2 import ingest_frame_for_eva
                ingest_frame_for_eva(img_bytes, camera_id)
            except Exception:
                pass
            _last_yolo_ts_by_camera[yolo_key] = now_ts
            _last_yolo_by_camera[camera_id] = {
                "count": 0,
                "classes": [],
                "detections": [],
                "timestamp": int(now_ts)
            }
            return {
                "success": True, "camera_id": camera_id, "user_id": user_id,
                "client_ip": client_ip, "frame_size": frame_size,
                "mode": "centinela" if is_vigilante else "normal", "timestamp": now_dt.isoformat(),
                "yolo": {"count": 0, "classes": [], "detections": []},
                **grid_result,
                **command_response
            }

        # 4b. MODO CENTINELA: fuera de horario + persona relevante = alerta directa sin grid
        if is_vigilante:
            logger.warning(f"MODO CENTINELA: {yolo_count} persona(s) relevante(s) {yolo_classes} → alerta directa")
            # Guardar frame como latest para el viewer (actualización en vivo)
            try:
                events_dir = STORAGE_ROOT / "users" / user_id / "cameras" / camera_id / "events"
                events_dir.mkdir(parents=True, exist_ok=True)
                with open(events_dir / "latest_vigilance.jpg", "wb") as f:
                    f.write(img_bytes)
                _save_recent_frame(user_id, camera_id, img_bytes)
            except Exception:
                pass
            # Guardar evento de vigilancia y notificar FCM
            _save_vigilance_event(user_id, camera_id, img_bytes, yolo_count, yolo_classes, client_ip)
            _update_camera_last_frame(user_id, camera_id)
            return {
                "success": True, "camera_id": camera_id, "user_id": user_id,
                "client_ip": client_ip, "frame_size": frame_size,
                "mode": "centinela", "timestamp": now_dt.isoformat(),
                "yolo": {"count": yolo_count, "classes": yolo_classes, "detections": yolo_detections},
                **grid_result,
                **command_response
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
            v_prompt = _normalize_system_prompt(user_id, camera_id, v_prompt)
            v_rules_raw = cam_cfg.get("rules", ud.get("vigilance_rules", []))
            v_rules = v_rules_raw if isinstance(v_rules_raw, str) else "\n".join(v_rules_raw) if v_rules_raw else ""

        grid_size = _get_grid_size_for_camera(user_id, camera_id)
        grid_result = orchestrator.add_frame(
            grid_bytes, camera_id, user_id,
            yolo_count=yolo_count, yolo_classes=yolo_classes, yolo_detections=yolo_detections,
            vigilance_prompt=v_prompt, vigilance_rules=v_rules, burst_mode=burst_mode,
            mode=current_mode, grid_size=grid_size
        )
        logger.info(f"Grid: {grid_result['frame_count']}/{grid_result['grid_size']} | YOLO:{yolo_count} | zone:{zone_priority}")

        # 8. Si el grid está lleno, Qwen lo analiza
        if grid_result.get("grid_full"):
            logger.warning(f"GRID FULL! Triggering Qwen analysis for {camera_id}")
            try:
                import asyncio
                qwen_result = await asyncio.wait_for(
                    orchestrator.process_grid(
                        user_id=user_id,
                        camera_id=camera_id,
                        vigilance_prompt=v_prompt,
                        vigilance_rules=v_rules,
                        mode=current_mode,
                        grid_size=grid_size
                    ),
                    timeout=45
                )
                logger.info(f"Qwen analysis done: violation={qwen_result.get('violation', False)} event_id={qwen_result.get('event_id','')}")
                event_id = qwen_result.get("event_id")
                if event_id:
                    try:
                        _promote_recent_to_event(
                            user_id=user_id,
                            camera_id=camera_id,
                            event_id=event_id,
                            summary=qwen_result.get("qwen_json", {}).get("summary") or "Análisis de cámara",
                            qwen_json=qwen_result.get("qwen_json") or {}
                        )
                    except Exception as event_package_err:
                        logger.debug(f"Event package failed: {event_package_err}")
                if not qwen_result.get("event_id"):
                    try:
                        logger.warning(f"Qwen returned empty event_id for {camera_id}; saving fallback event")
                        from orchestrator import save_event_to_disk_v2, update_camera_metrics
                        qwen_json = qwen_result.get("qwen_json") or {}
                        summary = qwen_json.get("summary") or "Sin actividad sospechosa"
                        grid = orchestrator._get_grid(user_id, camera_id, grid_size=grid_size)
                        fallback_event_id = save_event_to_disk_v2(
                            user_id=user_id,
                            camera_id=camera_id,
                            event_type="normal",
                            frame_bytes=grid.get_last_frame_bytes() or b"",
                            summary=summary,
                            qwen_json={**qwen_json, "violation": False, "mode": current_mode},
                            metadata={
                                "frames_count": qwen_result.get("frames_processed", 0),
                                "total_yolo_objects": yolo_count,
                                "yolo_classes": yolo_classes,
                                "grid_frames": [f.get("image_bytes", b"") for f in grid.frames],
                                "business_name": ud.get("business_name", ""),
                                "schedule": f"{_schedule_open}-{_schedule_close}",
                                "after_hours": False,
                                "mode": current_mode,
                                "fallback": "empty_grid_event_id",
                            }
                        )
                        update_camera_metrics(user_id, camera_id, event_type="normal")
                        qwen_result["event_id"] = fallback_event_id
                        qwen_result["action_taken"] = "event_saved_fallback"
                    except Exception as fallback_err:
                        logger.error(f"Fallback event save failed: {fallback_err}")
            except Exception as e:
                logger.error(f"Qwen analysis failed: {e}")
        else:
            pass  # Grid not full yet, keep filling

        # 9. Guardar frame para Eva
        try:
            from eva.eva_v2 import ingest_frame_for_eva
            ingest_frame_for_eva(img_bytes, camera_id)
            _save_recent_frame(user_id, camera_id, img_bytes)
        except Exception as e:
            logger.debug(f"eva frame buffer: {e}")

        # 10. Actualizar timestamp de la cámara
        _update_camera_last_frame(user_id, camera_id)

        return {
            "success": True,
            "camera_id": camera_id,
            "user_id": user_id,
            "client_ip": client_ip,
            "frame_size": frame_size,
            "mode": "centinela" if is_vigilante else "normal",
            "timestamp": now_dt.isoformat(),
            "yolo": {
                "count": yolo_count,
                "classes": yolo_classes,
                "detections": [{"class": d.get("class",""), "confidence": d.get("confidence",0), "bbox": d.get("bbox",[])} for d in yolo_detections]
            },
            **grid_result,
            **command_response
        }
    except Exception as e:
        import traceback as _tb
        print(f"[INGEST ERROR] {e}", flush=True)
        _tb.print_exc()
        logger.error(f"Ingest error: {e}")
        return {"success": False, "error": str(e)}

# ── Delta Mode & Background Objects ──────────────────────────────────────

# In-memory background objects per camera: {camera_id: {class_name: {bbox, last_seen_frame}}}
_background_objects: dict = {}

# Cooldown tracking: {camera_id: last_alert_timestamp}
_alert_cooldowns: dict = {}


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


def _filter_yolo_detections(detections: list, yolo_triggers: list = None,
                            sentinel: bool = False) -> list:
    """Filtrar detecciones YOLO por triggers relevantes."""
    if not isinstance(detections, list):
        return []
    if sentinel:
        triggers = {"person"}
    else:
        if isinstance(yolo_triggers, str):
            yolo_triggers = [x.strip() for x in re.split(r"[,;\n]", yolo_triggers) if x.strip()]
        triggers = set(yolo_triggers or ["person"])
        triggers.add("person")
    relevant = []
    for det in detections:
        cls = str(det.get("class", "")).lower().strip()
        if not cls:
            continue
        if cls in triggers:
            relevant.append(det)
    return relevant


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
        qwen_payload = {
            "violation": True,
            "mode": "centinela",
            "summary": f"Modo centinela: {yolo_count} persona(s) detectada(s) fuera de horario: {', '.join(yolo_classes)}",
            "details": {"persons": yolo_count, "actions_visible": ["persona fuera de horario"]},
            "anomalias": [{"tipo": "persona_en_centinela", "descripcion": f"Modo centinela: {yolo_count} persona(s) detectada(s) fuera de horario", "severidad": "alta"}],
            "importance": "alta",
            "importancia": "alta"
        }
        event_data = {
            "event_id": event_id,
            "camera_id": camera_id,
            "user_id": user_id,
            "event_type": "vigilance_alert",
            "timestamp": ts,
            "datetime": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(ts)),
            "yolo_count": yolo_count,
            "yolo_classes": yolo_classes,
            "source_ip": "test" if client_ip == "test" else client_ip,
            "description": qwen_payload["summary"],
            "summary": qwen_payload["summary"],
            "qwen_json": qwen_payload
        }
        package_metadata = {}
        try:
            package_metadata = _promote_recent_to_event(
                user_id=user_id,
                camera_id=camera_id,
                event_id=event_id,
                summary=qwen_payload["summary"],
                qwen_json=qwen_payload,
                frames_count=16
            )
        except Exception as package_err:
            logger.debug(f"Vigilance event package failed: {package_err}")
        event_data.update({
            "frames": package_metadata.get("frames", []),
            "frames_count": package_metadata.get("frames_count", 0),
            "video_file": package_metadata.get("video_file", ""),
            "clip_type": "event_package" if package_metadata else "",
            "source": "recent_buffer" if package_metadata else ""
        })
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
        body = f"Modo centinela activo. {yolo_count} persona(s) detectada(s) fuera de horario."
        from orchestrator import send_fcm_notification
        for token in tokens[:3]:
            import asyncio
            asyncio.create_task(send_fcm_notification(
                title=title,
                body=body,
                token=token,
                user_id=user_id,
                link=f"https://ojoia.com.do/#eva?alert={event_id}&camera={camera_id}"
            ))
        logger.info(f"Vigilance FCM queued to {len(tokens)} tokens")
    except Exception as e:
        logger.error(f"Error sending vigilance FCM: {e}")


def _update_camera_last_frame(user_id: str, camera_id: str):
    """Actualizar last_frame de una cámara en user.json."""
    if not user_id or user_id == "default":
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

# Anuncio de Dispositivo
@app.post("/devices/announce")
async def device_announce(request: dict = None):
    if not request:
        request = {}
    camera_id = request.get("camera_id", "unknown")
    user_id = request.get("user_id", "")
    client_ip = request.get("client_ip", "")
    if not camera_id:
        raise HTTPException(status_code=400, detail="camera_id required")
    logger.info(f"Device announce: Cam={camera_id} User={user_id} IP={client_ip}")
    
    # Update last_announce timestamp in user.json
    if user_id and user_id != "":
        uf = find_user_json(user_id)
        if uf and uf.exists():
            with open(uf) as f:
                ud = json.load(f)
            camera_found = False
            for c in ud.get("cameras", []):
                if c.get("camera_id") == camera_id:
                    c["last_announce"] = int(time.time())
                    c["last_announce_ip"] = client_ip
                    camera_found = True
                    logger.info(f"Updated last_announce for {camera_id}")
                    break
            if not camera_found:
                # Camera not in list, add it
                logger.info(f"Adding new camera {camera_id} to user {user_id}")
                ud.setdefault("cameras", []).append({
                    "camera_id": camera_id,
                    "name": camera_id,
                    "zone": "",
                    "active": True,
                    "first_seen": int(time.time()),
                    "last_announce": int(time.time()),
                    "last_announce_ip": client_ip,
                    "last_frame": 0
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


# ── Eva v2 — Motor unificado (setup + OS) ──────────────────────────────────
from eva.eva_v2 import handle_eva_v2 as handle_eva_chat, ingest_frame_for_eva

from fastapi.responses import FileResponse
import tempfile as _tmp

@app.get("/eva-frame/{user_id}/{camera_id}")
async def eva_frame(user_id: str, camera_id: str):
    """Servir frame almacenado de la cámara."""
    base = STORAGE_ROOT / "users" / user_id / "cameras" / camera_id / "frames"
    for name in ["eva_frame.jpg", "first.jpg", "latest_raw.jpg"]:
        p = base / name
        if p.exists():
            return FileResponse(str(p), media_type="image/jpeg")
    raise HTTPException(status_code=404, detail="Frame not found")


@app.get("/frames/latest-raw")
async def get_latest_raw_frame(camera_id: Optional[str] = None):
    """Devolver el frame mas reciente sin pasar por el grid (para setup de Eva)."""
    from eva.eva_v2 import _latest_frame, _latest_frame_time
    import time as _time
    best_frame = None
    best_time = 0
    for cid, frame in _latest_frame.items():
        t = _latest_frame_time.get(cid, 0)
        if t > best_time and (_time.time() - t < 120):
            best_time = t
            best_frame = frame
    if best_frame:
        return Response(content=best_frame, media_type="image/jpeg", headers={"Cache-Control": "no-store"})
    return Response(status_code=204)


@app.get("/eva-image/{filename}")
async def eva_image(filename: str):
    """Servir imagen temporal de Eva."""
    tmp_dir = Path(_tmp.gettempdir())
    file_path = tmp_dir / filename
    if file_path.exists():
        return FileResponse(str(file_path), media_type="image/jpeg")
    # Fallback: buscar en storage persistente (eva_frame.jpg)
    import glob
    for f in glob.glob(str(STORAGE_ROOT / "users" / "*" / "cameras" / "*" / "frames" / "eva_frame.jpg")):
        return FileResponse(f, media_type="image/jpeg")
    raise HTTPException(status_code=404, detail="Image not found")


@app.post("/config/chat")
async def config_chat(request: dict):
    import logging
    logger = logging.getLogger(__name__)
    logger.info(f"[CHAT] user_id={request.get('user_id')}, message={request.get('message')}, session_id={request.get('session_id')}")
    try:
        result = await handle_eva_chat(
            user_id=request.get("user_id", ""),
            message=request.get("message", "hola"),
            session_id=request.get("session_id", ""),
            cam_id=request.get("cam_id"),
            include_frame=bool(request.get("include_frame", False)),
            storage_root=STORAGE_ROOT,
        )
        return result
    except Exception as e:
        logger.error(f"[CHAT ERROR] {e}", exc_info=True)
        raise


# ── NUEVO: Auto-config simple (sin chat) ─────────────────────────────────────

@app.post("/config/auto_config")
async def config_auto_config(request: dict):
    """
    Genera configuración de cámara automáticamente.
    Una sola llamada a Qwen. Sin chat.
    
    Body: { user_id, camera_id (opcional, para edición) }
    """
    from eva.auto_config import auto_generate_config
    from eva.eva_v2 import ingest_frame_for_eva

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
    result = await orchestrator.process_grid(prompt, vigilance_prompt, vigilance_rules)
    if "violation_frame" in result and result["violation_frame"]:
        result["violation_frame"] = "[Frame data excluded from response]"
    return result

# ── Admin Audit Logs ───────────────────────────────────────────────────────

def _admin_actor_from_request(request: Request) -> str:
    token = _extract_bearer_token(request) if "_extract_bearer_token" in globals() else ""
    cfg = _load_admin_config() if "_load_admin_config" in globals() else {}
    sessions = cfg.get("sessions", {}) or {}
    for session_token, meta in sessions.items():
        if token and hmac.compare_digest(token, session_token):
            return meta.get("email") or meta.get("actor") or "admin"
    return "admin"


def _audit_log(action: str, actor: str = "admin", target: str = None, data: dict = None):
    try:
        ADMIN_LOG_DIR.mkdir(parents=True, exist_ok=True)
        now = datetime.utcnow()
        path = ADMIN_LOG_DIR / f"admin_{now.strftime('%Y%m%d')}.jsonl"
        entry = {
            "ts": now.isoformat() + "Z",
            "action": action,
            "actor": actor,
            "target": target,
            "data": data or {},
        }
        with open(path, "a") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.error(f"Admin audit log error: {e}")


def _load_admin_logs(limit: int = 200, level: str = None):
    ADMIN_LOG_DIR.mkdir(parents=True, exist_ok=True)
    rows = []
    for path in sorted(ADMIN_LOG_DIR.glob("admin_*.jsonl"), reverse=True):
        try:
            with open(path) as f:
                for line in f:
                    if not line.strip():
                        continue
                    try:
                        row = json.loads(line)
                    except Exception:
                        continue
                    if level and row.get("action") != level:
                        continue
                    rows.append(row)
                    if len(rows) >= limit:
                        return rows
        except Exception:
            continue
    return rows

# ── Admin Authentication ─────────────────────────────────────────────────

def _load_admin_config() -> dict:
    try:
        with open(ADMIN_CONFIG_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_admin_config(cfg: dict):
    ADMIN_CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(ADMIN_CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
    try:
        os.chmod(ADMIN_CONFIG_FILE, 0o600)
    except Exception:
        pass


def _get_admin_token() -> str:
    env_token = os.getenv("OJOIA_ADMIN_TOKEN")
    if env_token:
        return env_token.strip()
    cfg = _load_admin_config()
    token = cfg.get("admin_token")
    if token:
        return token
    token = "oj_admin_" + secrets.token_urlsafe(32)
    cfg["admin_token"] = token
    cfg["admin_email"] = cfg.get("admin_email", "admin@ojoia.com.do")
    cfg["created_at"] = time.time()
    _save_admin_config(cfg)
    return token


def _extract_bearer_token(request: Request) -> str:
    auth = request.headers.get("authorization") or request.headers.get("Authorization") or ""
    if auth.lower().startswith("bearer "):
        return auth.split(" ", 1)[1].strip()
    return auth.strip()


def _valid_admin_session_token(token: str) -> bool:
    if not token:
        return False
    expected = _get_admin_token()
    if hmac.compare_digest(token, expected):
        return True
    cfg = _load_admin_config()
    sessions = cfg.get("sessions", {}) or {}
    now = time.time()
    valid_sessions = {}
    ok = False
    for session_token, meta in sessions.items():
        expires_at = float(meta.get("expires_at", 0) or 0)
        if expires_at <= now:
            continue
        valid_sessions[session_token] = meta
        if hmac.compare_digest(session_token, token):
            ok = True
    if len(valid_sessions) != len(sessions):
        cfg["sessions"] = valid_sessions
        _save_admin_config(cfg)
    return ok


def _is_admin_request_authorized(request: Request) -> bool:
    return _valid_admin_session_token(_extract_bearer_token(request))


@app.post("/admin/auth/login")
async def admin_auth_login(request: Request):
    try:
        data = await request.json()
    except Exception:
        data = {}
    supplied = str(data.get("token") or data.get("access_token") or data.get("password") or "")
    expected = _get_admin_token()
    if not supplied or not hmac.compare_digest(supplied, expected):
        _audit_log("admin_login_failed", actor="anonymous", target="admin", data={"ip": request.client.host if request.client else ""})
        return JSONResponse(status_code=401, content={"success": False, "error": "Credencial inválida"})
    session_token = secrets.token_urlsafe(32)
    cfg = _load_admin_config()
    now = time.time()
    sessions = cfg.get("sessions", {}) or {}
    sessions = {k: v for k, v in sessions.items() if float(v.get("expires_at", 0) or 0) > now}
    sessions[session_token] = {
        "created_at": now,
        "expires_at": now + ADMIN_SESSION_TTL_SECONDS,
        "user_agent": request.headers.get("user-agent", "")
    }
    cfg["sessions"] = sessions
    _save_admin_config(cfg)
    _audit_log("admin_login", actor=cfg.get("admin_email", "admin"), target="admin", data={"ip": request.client.host if request.client else ""})
    return {
        "success": True,
        "session_token": session_token,
        "expires_in": ADMIN_SESSION_TTL_SECONDS,
        "admin": {"email": cfg.get("admin_email", "admin@ojoia.com.do")}
    }


@app.get("/admin/auth/me")
async def admin_auth_me(request: Request):
    if not _is_admin_request_authorized(request):
        return JSONResponse(status_code=401, content={"success": False, "error": "Admin no autorizado"})
    cfg = _load_admin_config()
    return {"success": True, "admin": {"email": cfg.get("admin_email", "admin@ojoia.com.do")}}


@app.post("/admin/auth/logout")
async def admin_auth_logout(request: Request):
    token = _extract_bearer_token(request)
    cfg = _load_admin_config()
    sessions = cfg.get("sessions", {}) or {}
    if token in sessions:
        del sessions[token]
        cfg["sessions"] = sessions
        _save_admin_config(cfg)
        _audit_log("admin_logout", actor=_admin_actor_from_request(request), target="admin", data={"ip": request.client.host if request.client else ""})
    return {"success": True}

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
    _audit_log("admin_user_renew", actor="admin", target=user_id, data={"plan": plan, "duration_days": duration_days, "amount": amount})
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

    _audit_log("admin_payment_confirm", actor="admin", target=user_id, data={"payment_id": payment_id})
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
    _audit_log("admin_user_suspend", actor="admin", target=user_id, data={})
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
    _audit_log("admin_user_reactivate", actor="admin", target=user_id, data={})
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
    _audit_log("admin_user_regen_token", actor="admin", target=user_id, data={})
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
                if announce_age is not None and announce_age < CAMERA_ONLINE_GRACE_SECONDS:
                    is_online = True
                if frame_age is not None and frame_age < CAMERA_ONLINE_GRACE_SECONDS:
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

@app.get("/admin/cameras/{camera_id}")
async def admin_get_camera(camera_id: str):
    uid, canonical = find_camera_owner(camera_id)
    if not uid:
        raise HTTPException(status_code=404, detail="Camara no encontrada")
    cam = await get_camera(canonical, uid)
    cam["user_id"] = uid
    return cam

@app.put("/admin/cameras/{camera_id}")
async def admin_update_camera(camera_id: str, request: dict):
    uid, canonical = find_camera_owner(camera_id)
    if not uid:
        raise HTTPException(status_code=404, detail="Camara no encontrada")
    uf = find_user_json(uid)
    if not uf or not uf.exists():
        raise HTTPException(status_code=404, detail="Usuario no encontrado")
    with open(uf) as f:
        ud = json.load(f)
    found = False
    for cam in ud.get("cameras", []):
        if cam.get("camera_id") == canonical or cam.get("physical_camera_id") == camera_id:
            updatable = ["name", "zone", "business_type", "main_concerns", "last_announce_ip", "physical_camera_id", "camera_aliases", "cooldown_min"]
            for field in updatable:
                if field in request:
                    cam[field] = request[field]
            if "vigilance_rules" in request:
                cam_cfg = get_camera_config_static(uid, canonical) or {}
                cam_cfg["rules"] = request.get("vigilance_rules")
                cam_cfg["rules_es"] = request.get("rules_es") or request.get("vigilance_rules")
                cam_cfg["system_prompt"] = request.get("system_prompt", cam_cfg.get("system_prompt", ""))
                cam_cfg["vigilance_prompt"] = request.get("vigilance_prompt", cam_cfg.get("vigilance_prompt", ""))
                cam_cfg["schedule"] = request.get("schedule", cam_cfg.get("schedule", {}))
                cam_cfg["cooldown_min"] = request.get("cooldown_min", cam_cfg.get("cooldown_min", 5))
                cam_cfg["yolo_triggers"] = request.get("yolo_triggers", cam_cfg.get("yolo_triggers", ["person"]))
                vigilance = cam_cfg.setdefault("vigilance", {})
                if isinstance(vigilance, dict):
                    normal_mode = vigilance.setdefault("normal_mode", {})
                    sentinel_mode = vigilance.setdefault("sentinel_mode", {})
                    if isinstance(normal_mode, dict):
                        normal_mode["cooldown_min"] = cam_cfg["cooldown_min"]
                        normal_mode["yolo_triggers"] = cam_cfg["yolo_triggers"]
                    if isinstance(sentinel_mode, dict):
                        sentinel_mode["cooldown_min"] = cam_cfg["cooldown_min"]
                        sentinel_mode["yolo_triggers"] = cam_cfg["yolo_triggers"]
                _save_camera_config_static(uid, canonical, cam_cfg)
                ud["vigilance_prompt"] = cam_cfg.get("vigilance_prompt") or cam_cfg.get("system_prompt", "")
                ud["rules_es"] = cam_cfg.get("rules_es") or cam_cfg.get("rules", [])
            found = True
            break
    if not found:
        raise HTTPException(status_code=404, detail="Camara no encontrada")
    with open(uf, "w") as f:
        json.dump(ud, f, indent=2, ensure_ascii=False)
    _audit_log("admin_camera_update", actor="admin", target=canonical, data={"user_id": uid, "fields": list(request.keys())})
    return {"success": True, "camera_id": canonical, "user_id": uid}

@app.delete("/admin/cameras/{camera_id}")
async def admin_delete_camera(camera_id: str, user_id: str = None):
    uid, canonical = find_camera_owner(camera_id)
    if user_id and uid and uid != user_id:
        raise HTTPException(status_code=403, detail="Camara no pertenece al usuario indicado")
    if not uid:
        raise HTTPException(status_code=404, detail="Camara no encontrada")
    result = await delete_camera(canonical, uid)
    _audit_log("admin_camera_delete", actor="admin", target=canonical, data={"user_id": uid})
    return result

@app.post("/admin/cameras/{camera_id}/cmd")
async def admin_camera_cmd(camera_id: str, request: Request):
    data = await request.json()
    result = await cam_cmd(camera_id, data)
    _audit_log("admin_camera_command", actor="admin", target=camera_id, data={"body": {k: v for k, v in data.items() if k not in ("password", "token")}})
    return result

@app.get("/admin/cameras/{camera_id}/ota")
async def admin_camera_ota(camera_id: str, request: Request):
    uid, canonical = find_camera_owner(camera_id)
    if not uid:
        raise HTTPException(status_code=404, detail="Camara no encontrada")
    base = f"{request.url.scheme}://{request.url.netloc}"
    size = FIRMWARE_BIN.stat().st_size if FIRMWARE_BIN.exists() else 0
    _audit_log("admin_camera_ota_check", actor="admin", target=canonical, data={"user_id": uid, "firmware": FIRMWARE_VERSION})
    return {"success": True, "camera_id": canonical, "user_id": uid, "update_available": True, "firmware_version": FIRMWARE_VERSION, "firmware_url": f"{base}/ota/firmware.bin", "firmware_size": size, "note": "La cámara consulta OTA automáticamente cada 10 minutos."}

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
        for fname in sorted(events_dir.iterdir(), key=lambda x: (_load_json_safely(x).get("timestamp", 0) if x.name.endswith(".json") else 0), reverse=True):
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

@app.get("/admin/events/{event_id}")
async def admin_get_event(event_id: str, user_id: str = None):
    for disk in get_disk_config().get("disks", []):
        users_base = Path(disk.get("mount", "")) / disk.get("user_folder", "users")
        if not users_base.is_dir():
            continue
        for uid in users_base.iterdir():
            if user_id and uid.name != user_id:
                continue
            for _cam_id, events_dir in resolve_user_events_dirs(uid.name):
                ef = events_dir / f"{event_id}.json"
                if ef.exists():
                    ev = json.load(open(ef))
                    ev["_user_id"] = uid.name
                    return ev
    raise HTTPException(status_code=404, detail="Evento no encontrado")

@app.post("/admin/events/{event_id}/dismiss")
async def admin_dismiss_event(event_id: str, request: dict = None):
    user_id = (request or {}).get("user_id", "")
    for disk in get_disk_config().get("disks", []):
        users_base = Path(disk.get("mount", "")) / disk.get("user_folder", "users")
        if not users_base.is_dir():
            continue
        for uid in users_base.iterdir():
            if user_id and uid.name != user_id:
                continue
            for _cam_id, events_dir in resolve_user_events_dirs(uid.name):
                ef = events_dir / f"{event_id}.json"
                if ef.exists():
                    ev = json.load(open(ef))
                    ev["dismissed"] = True
                    ev["dismissed_at"] = int(time.time())
                    with open(ef, "w") as f:
                        json.dump(ev, f, indent=2)
                    _record_event_review(uid.name, _cam_id if _cam_id != "_global" else (ev.get("camera_id") or _cam_id), event_id, "false_alarm", (request or {}).get("reason"))
                    _audit_log("admin_event_dismiss", actor="admin", target=event_id, data={"user_id": uid.name})
                    return {"success": True}
    raise HTTPException(status_code=404, detail="Evento no encontrado")

@app.post("/admin/events/{event_id}/confirm")
async def admin_confirm_event(event_id: str, request: dict = None):
    user_id = (request or {}).get("user_id", "")
    for disk in get_disk_config().get("disks", []):
        users_base = Path(disk.get("mount", "")) / disk.get("user_folder", "users")
        if not users_base.is_dir():
            continue
        for uid in users_base.iterdir():
            if user_id and uid.name != user_id:
                continue
            for _cam_id, events_dir in resolve_user_events_dirs(uid.name):
                ef = events_dir / f"{event_id}.json"
                if ef.exists():
                    ev = json.load(open(ef))
                    ev["confirmed"] = True
                    ev["confirmed_at"] = int(time.time())
                    with open(ef, "w") as f:
                        json.dump(ev, f, indent=2)
                    _record_event_review(uid.name, _cam_id if _cam_id != "_global" else (ev.get("camera_id") or _cam_id), event_id, "confirmed", (request or {}).get("reason"))
                    _audit_log("admin_event_confirm", actor="admin", target=event_id, data={"user_id": uid.name})
                    return {"success": True}
    raise HTTPException(status_code=404, detail="Evento no encontrado")

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

@app.post("/admin/system/restart/backend")
async def admin_restart_backend():
    try:
        subprocess.run(["systemctl", "--user", "restart", "api_eva.service"], check=True, timeout=10)
        _audit_log("admin_system_restart_backend", actor="admin", target="api_eva.service", data={})
        return {"success": True, "service": "api_eva.service", "message": "Backend reiniciado"}
    except Exception as e:
        _audit_log("admin_system_restart_backend_error", actor="admin", target="api_eva.service", data={"error": str(e)})
        return {"success": False, "error": str(e)}

@app.post("/admin/system/restart/tunnel")
async def admin_restart_tunnel():
    try:
        subprocess.run(["systemctl", "--user", "restart", "tunnel.service"], check=True, timeout=10)
        _audit_log("admin_system_restart_tunnel", actor="admin", target="tunnel.service", data={})
        return {"success": True, "service": "tunnel.service", "message": "Túnel reiniciado"}
    except Exception as e:
        _audit_log("admin_system_restart_tunnel_error", actor="admin", target="tunnel.service", data={"error": str(e)})
        return {"success": False, "error": str(e)}

@app.get("/admin/system/processes")
async def admin_system_processes():
    try:
        ps = subprocess.run(["ps", "-eo", "pid,ppid,stat,cmd"], capture_output=True, text=True, timeout=5)
        return {"success": True, "processes": ps.stdout.splitlines()[-200:]}
    except Exception as e:
        return {"success": False, "error": str(e)}

@app.post("/admin/backups/create")
async def admin_create_backup(request: dict = None):
    request = request or {}
    BACKUP_DIR = STORAGE_ROOT / "backups_admin"
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    name = request.get("name") or datetime.now().strftime("backup_%Y%m%d_%H%M%S")
    safe_name = re.sub(r"[^A-Za-z0-9_.-]", "_", name)
    archive = BACKUP_DIR / f"{safe_name}.zip"
    sources = [DISKS_CONFIG_FILE, EVA_CONFIG_FILE, ADMIN_CONFIG_FILE, STORAGE_ROOT / "users"]
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
        for src in sources:
            if not src.exists():
                continue
            if src.is_dir():
                for path in src.rglob("*"):
                    if path.is_file():
                        zf.write(path, path.relative_to(STORAGE_ROOT.parent))
            else:
                zf.write(src, src.relative_to(STORAGE_ROOT.parent))
    _audit_log("admin_backup_create", actor="admin", target=str(archive), data={"name": safe_name})
    return {"success": True, "backup": safe_name + ".zip", "path": str(archive)}

@app.get("/admin/backups")
async def admin_list_backups():
    BACKUP_DIR = STORAGE_ROOT / "backups_admin"
    backups = []
    if BACKUP_DIR.exists():
        for p in sorted(BACKUP_DIR.glob("*.zip"), key=lambda x: x.stat().st_mtime, reverse=True):
            backups.append({"name": p.name, "size_mb": round(p.stat().st_size / (1024 * 1024), 2), "created_at": datetime.fromtimestamp(p.stat().st_mtime).isoformat()})
    return {"backups": backups}

@app.post("/admin/backups/restore/{backup_name}")
async def admin_restore_backup(backup_name: str, request: dict):
    if not request.get("confirm"):
        return {"success": False, "error": "confirm:true requerido"}
    BACKUP_DIR = STORAGE_ROOT / "backups_admin"
    archive = BACKUP_DIR / backup_name
    if not archive.exists():
        raise HTTPException(status_code=404, detail="Backup no encontrado")
    restore_dir = STORAGE_ROOT / "backups_admin" / ("restore_" + datetime.now().strftime("%Y%m%d_%H%M%S"))
    restore_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as zf:
        zf.extractall(restore_dir)
    _audit_log("admin_backup_restore_prepare", actor="admin", target=backup_name, data={"restore_dir": str(restore_dir)})
    return {"success": True, "message": "Backup extraído para revisión", "restore_dir": str(restore_dir), "warning": "No se sobrescribió producción automáticamente."}

@app.post("/admin/server/cloudflared/save")
async def admin_cloudflared_save(request: dict):
    return {"success": True}

@app.post("/admin/server/sync-firestore")
async def admin_sync_firestore():
    return {"success": True}

@app.get("/admin/logs")
async def admin_logs(limit: int = 200, tail: int = 80):
    audit = _load_admin_logs(limit=limit)
    api_tail = []
    log_path = Path("/home/sam/ai_system/api_eva.log")
    if log_path.exists():
        try:
            lines = log_path.read_text(errors="ignore").splitlines()
            api_tail = lines[-tail:]
        except Exception as e:
            api_tail = [f"Error leyendo api_eva.log: {e}"]
    return {"audit_logs": audit, "api_tail": api_tail}

def _load_admin_metrics() -> dict:
    try:
        if ADMIN_METRICS_FILE.exists():
            with open(ADMIN_METRICS_FILE) as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception as e:
        logger.error(f"Admin metrics load error: {e}")
    return {"cameras": {}, "events": {}}

def _save_admin_metrics(data: dict):
    ADMIN_METRICS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(ADMIN_METRICS_FILE, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    try:
        os.chmod(ADMIN_METRICS_FILE, 0o600)
    except Exception:
        pass

def _camera_metric_key(user_id: str, camera_id: str) -> str:
    return f"{user_id}/{camera_id}"

def _record_event_review(user_id: str, camera_id: str, event_id: str, decision: str, reason: str = None):
    data = _load_admin_metrics()
    key = _camera_metric_key(user_id, camera_id)
    cam = data.setdefault("cameras", {}).setdefault(key, {
        "user_id": user_id,
        "camera_id": camera_id,
        "false_alarm_count": 0,
        "confirmed_count": 0,
        "dismissed_count": 0,
        "total_reviews": 0,
        "reviews": [],
        "audit_flags": [],
        "updated_at": None,
    })
    review = {
        "event_id": event_id,
        "decision": decision,
        "reason": reason or "",
        "created_at": int(time.time()),
    }
    cam["total_reviews"] = int(cam.get("total_reviews", 0)) + 1
    if decision == "false_alarm":
        cam["false_alarm_count"] = int(cam.get("false_alarm_count", 0)) + 1
        cam["last_false_alarm"] = int(time.time())
    elif decision == "confirmed":
        cam["confirmed_count"] = int(cam.get("confirmed_count", 0)) + 1
    elif decision == "dismissed":
        cam["dismissed_count"] = int(cam.get("dismissed_count", 0)) + 1
    cam["reviews"] = (cam.get("reviews") or [])[-100:] + [review]
    cam["false_alarm_rate"] = round(cam["false_alarm_count"] / max(cam["total_reviews"], 1), 3)
    cam["updated_at"] = int(time.time())
    audit_flags = []
    if cam["false_alarm_count"] >= 3 and cam["false_alarm_rate"] >= 0.5:
        audit_flags.append("alta_falsa_alarma")
    if cam.get("total_reviews", 0) >= 5 and cam["false_alarm_rate"] >= 0.4:
        audit_flags.append("prompt_requiere_revision")
    cam["audit_flags"] = sorted(set(audit_flags))
    data["events"][event_id] = {
        "user_id": user_id,
        "camera_id": camera_id,
        "decision": decision,
        "reason": reason or "",
        "created_at": int(time.time()),
    }
    _save_admin_metrics(data)
    _audit_log("admin_event_review", actor="admin", target=event_id, data={"user_id": user_id, "camera_id": camera_id, "decision": decision, "reason": reason})
    return cam

def _monitoring_camera_audit(user_id: str, camera_id: str):
    data = _load_admin_metrics()
    key = _camera_metric_key(user_id, camera_id)
    return data.get("cameras", {}).get(key, {
        "user_id": user_id,
        "camera_id": camera_id,
        "false_alarm_count": 0,
        "confirmed_count": 0,
        "dismissed_count": 0,
        "total_reviews": 0,
        "false_alarm_rate": 0,
        "reviews": [],
        "audit_flags": [],
    })

# Monitoring helpers
def _monitoring_camera_records(limit: int = 200):
    cfg = get_disk_config()
    now = time.time()
    cameras = []
    seen = set()
    for disk in cfg.get("disks", []):
        users_base = Path(disk.get("mount", "")) / disk.get("user_folder", "users")
        if not users_base.is_dir():
            continue
        for uid_dir in users_base.iterdir():
            user_file = uid_dir / "user.json"
            if not user_file.is_file():
                continue
            try:
                with open(user_file) as f:
                    user_data = json.load(f)
            except Exception:
                continue
            uid = user_data.get("user_id", uid_dir.name)
            cam_names = {c.get("camera_id", ""): c.get("name", "") for c in user_data.get("cameras", [])}
            for cam in user_data.get("cameras", []):
                cid = cam.get("camera_id", "") or ""
                if not cid or cid in seen:
                    continue
                seen.add(cid)
                last_announce = cam.get("last_announce") or 0
                last_frame = cam.get("last_frame") or 0
                last_seen = max(last_announce or 0, last_frame or 0)
                active = bool(last_seen and (now - last_seen) < CAMERA_ONLINE_GRACE_SECONDS)
                metrics = _camera_metrics(uid, cid)
                cameras.append({
                    "camera_id": cid,
                    "name": cam.get("name") or cam_names.get(cid) or cid,
                    "user_id": uid,
                    "user_name": user_data.get("name", ""),
                    "business_name": user_data.get("business_name", ""),
                    "status": "online" if active else "offline",
                    "active": active,
                    "last_announce": datetime.fromtimestamp(last_announce).isoformat() if last_announce else None,
                    "last_frame": datetime.fromtimestamp(last_frame).isoformat() if last_frame else None,
                    "last_seen": datetime.fromtimestamp(last_seen).isoformat() if last_seen else None,
                    "last_seen_ts": last_seen,
                    "announce_age_s": int(now - last_announce) if last_announce else None,
                    "frame_age_s": int(now - last_frame) if last_frame else None,
                    "offline_seconds": int(now - last_seen) if last_seen else None,
                    "metrics": metrics,
                    "pending_commands": bool((user_data.get("pending_commands") or {}).get(cid)),
                    "latest_frame_url": f"/frames/latest.jpg?camera_id={cid}&user_id={uid}",
                    "latest_raw_url": f"/frames/latest-raw.jpg?camera_id={cid}&user_id={uid}",
                })
    cameras.sort(key=lambda c: (0 if c["active"] else 1, -(c.get("last_seen_ts") or 0)))
    return cameras[:limit]


def _admin_camera_config_payload(user_data: dict, camera_record: dict, cam_cfg: dict) -> dict:
    normalized_cfg = normalize_camera_vigilance_config(cam_cfg or {})
    vigilance = normalized_cfg.get("vigilance") if isinstance(normalized_cfg.get("vigilance"), dict) else {}
    normal_mode = vigilance.get("normal_mode") if isinstance(vigilance.get("normal_mode"), dict) else {}
    schedule = normalized_cfg.get("schedule") or (user_data or {}).get("schedule") or {}
    rules = (
        normalized_cfg.get("rules")
        or normalized_cfg.get("vigilance_rules")
        or normalized_cfg.get("rules_es")
        or vigilance.get("alert_behaviors")
        or normal_mode.get("alert_behaviors")
        or []
    )
    system_prompt = normalized_cfg.get("system_prompt") or normalized_cfg.get("vigilance_prompt") or ""
    return {
        "rules": rules,
        "rules_es": rules,
        "system_prompt": system_prompt,
        "vigilance_prompt": normalized_cfg.get("vigilance_prompt") or system_prompt,
        "schedule": schedule,
        "vigilance": vigilance,
        "yolo_triggers": normal_mode.get("yolo_triggers") or normalized_cfg.get("yolo_triggers") or ["person"],
        "cooldown_min": normal_mode.get("cooldown_min") or normalized_cfg.get("cooldown_min") or (camera_record or {}).get("cooldown_min") or 5,
        "current_mode": _get_current_mode(schedule, vigilance),
    }


def _prompt_versions_path(user_id: str, camera_id: str) -> Path:
    return STORAGE_ROOT / "users" / user_id / "cameras" / camera_id / "prompt_versions.json"


def _load_prompt_versions(user_id: str, camera_id: str) -> dict:
    path = _prompt_versions_path(user_id, camera_id)
    data = _load_json_safely(path) or {}
    if not isinstance(data, dict):
        data = {}
    if "versions" not in data:
        data["versions"] = data.get("history") or []
    data["versions"] = data.get("versions") or []
    return data


def _save_prompt_versions(user_id: str, camera_id: str, data: dict):
    path = _prompt_versions_path(user_id, camera_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    _save_json_safely(path, data)


def _append_prompt_version(user_id: str, camera_id: str, payload: dict) -> dict:
    data = _load_prompt_versions(user_id, camera_id)
    versions = data.get("versions") or []
    next_version = (max([int(v.get("version", 0)) for v in versions if isinstance(v, dict)] or [0]) + 1)
    version = {
        "version": next_version,
        "created_at": int(time.time()),
        "applied": bool(payload.get("applied", False)),
        "reason": payload.get("reason") or "auditoria",
        "source": payload.get("source") or "manual",
        "score": payload.get("score"),
        "diagnosis": payload.get("diagnosis") or [],
        "prompt": payload.get("prompt") or payload.get("suggested_prompt") or "",
        "rules": payload.get("rules") or payload.get("suggested_rules") or [],
        "rules_es": payload.get("rules_es") or payload.get("rules") or payload.get("suggested_rules") or [],
        "yolo_triggers": payload.get("yolo_triggers") or payload.get("suggested_yolo_triggers") or ["person"],
        "cooldown_min": payload.get("cooldown_min") or payload.get("suggested_cooldown_min") or 5,
        "metadata": payload.get("metadata") or {},
    }
    versions.append(version)
    data["versions"] = versions[-50:]
    data["updated_at"] = int(time.time())
    _save_prompt_versions(user_id, camera_id, data)
    return version


def _prompt_list_join(value) -> str:
    if isinstance(value, list):
        return ", ".join(str(x).strip() for x in value if str(x).strip())
    if isinstance(value, str):
        return value.strip(" ,\n\t")
    return ""


def _build_improved_vigilance_prompt(context: dict) -> str:
    user = context.get("user") or {}
    config = context.get("config") or {}
    vigilance = config.get("vigilance") or {}
    schedule = config.get("schedule") or user.get("schedule") or {}
    business_name = context.get("business_name") or user.get("business_name") or "el negocio"
    business_type = context.get("business_type") or user.get("business_type") or "negocio"
    zone = context.get("zone") or "zona principal"
    concern = _prompt_list_join(config.get("concern") or vigilance.get("concern") or user.get("main_concerns") or "seguridad general")
    normal_state = _prompt_list_join(vigilance.get("normal_state") or "actividad normal esperada para esta zona")
    authorized = _prompt_list_join(vigilance.get("authorized_people") or "personas autorizadas")
    important = _prompt_list_join(vigilance.get("important_objects") or "objetos importantes de la zona")
    alert_behaviors = config.get("rules") or config.get("rules_es") or vigilance.get("alert_behaviors") or []
    ignore_behaviors = vigilance.get("ignore_behaviors") or []
    open_time = schedule.get("open", "08:00")
    close_time = schedule.get("close", "22:00")
    rules_text = "\n".join(f"- {r}" for r in alert_behaviors) if alert_behaviors else "- actividad concreta, observable y relacionada con robo, daño, intrusión, fuego, humo o cámara obstruida"
    ignore_text = "\n".join(f"- {r}" for r in ignore_behaviors) if ignore_behaviors else "- baja actividad\n- escena repetida\n- falta de movimiento"
    return (
        f"Eres Eva, vigilante de seguridad de {business_name} ({business_type}) en República Dominicana. "
        f"Cámara ubicada en {zone}. Horario normal: {open_time} a {close_time}. "
        f"Preocupación principal del usuario: {concern}.\n\n"
        f"Estado normal esperado: {normal_state}.\n"
        f"Personas autorizadas: {authorized}.\n"
        f"Objetos importantes: {important}.\n\n"
        "Alerta solo si ves una acción concreta, observable y verificable en la imagen:\n"
        f"{rules_text}\n\n"
        "Nunca alertes solo por estas situaciones normales:\n"
        f"{ignore_text}\n\n"
        "Para una alerta de robo, intrusión o manipulación indebida exige al menos dos señales claras: persona no autorizada, mano visible, efectivo/producto, bolsillo/funda, caja abierta, acción de ocultar, retirar o mover dinero/productos.\n"
        "No inventes intenciones. No uses baja actividad, escena repetida, mala cobertura o sospecha vaga como evidencia.\n"
        "Si todo está normal, responde violation=false, importance='normal', summary='actividad normal en " + zone + "', details con conteo visible y anomalias=[].\n"
        "Responde SOLO JSON válido con: violation, mode, summary, details, anomalias, importance, evidence."
    )


def _fallback_prompt_audit(context: dict) -> dict:
    config = context.get("config") or {}
    audit = context.get("audit") or {}
    prompt = config.get("system_prompt") or config.get("vigilance_prompt") or ""
    rules = config.get("rules") or config.get("rules_es") or []
    vigilance = config.get("vigilance") or {}
    cooldown = int(config.get("cooldown_min") or 5)
    yolo_triggers = config.get("yolo_triggers") or ["person"]
    false_alarm_rate = float(audit.get("false_alarm_rate") or 0)
    false_alarm_count = int(audit.get("false_alarm_count") or 0)
    diagnosis = []
    score = 100
    if len(prompt) < 300:
        diagnosis.append("El prompt es muy corto y puede dejar demasiada interpretación al modelo.")
        score -= 15
    if re.search(r"raro|sospechoso|inusual|anormal", prompt.lower()) and not rules:
        diagnosis.append("El prompt usa términos subjetivos sin convertirlos en acciones observables.")
        score -= 15
    if not rules:
        diagnosis.append("Faltan reglas concretas de alerta basadas en objetos, acciones y evidencia.")
        score -= 15
    if not vigilance.get("ignore_behaviors"):
        diagnosis.append("Faltan reglas negativas para reducir falsas alarmas.")
        score -= 10
    if not vigilance.get("normal_state"):
        diagnosis.append("Falta una descripción clara del estado normal esperado.")
        score -= 10
    if false_alarm_rate >= 0.4 or false_alarm_count >= 3:
        diagnosis.append("Hay falsas alarmas suficientes para recalibrar reglas negativas, cooldown o sensibilidad.")
        score -= 15
    if cooldown < 5:
        diagnosis.append("El cooldown es bajo y puede generar ráfagas de alertas repetidas.")
        score -= 5
    suggested_rules = list(rules) if rules else list(vigilance.get("alert_behaviors") or [])
    suggested_cooldown = cooldown
    if false_alarm_rate >= 0.4 or false_alarm_count >= 3:
        suggested_cooldown = min(20, max(8, suggested_cooldown + 2))
    suggested_yolo = list(yolo_triggers) if yolo_triggers else ["person"]
    return {
        "success": True,
        "source": "fallback",
        "auditor_model": "local_rules",
        "score": max(35, min(98, score)),
        "diagnosis": diagnosis or ["La configuración ya tiene una base razonable. Se recomienda probar mejoras pequeñas."],
        "suggested_prompt": _build_improved_vigilance_prompt(context),
        "suggested_rules": suggested_rules,
        "suggested_yolo_triggers": suggested_yolo,
        "suggested_cooldown_min": suggested_cooldown,
        "tests_needed": [
            "Probar frames con actividad normal del negocio.",
            "Probar frames con clientes esperando o fila normal.",
            "Probar frames con manipulación real de dinero/productos.",
            "Probar frames con cámara obstruida o mala iluminación."
        ],
    }


def _parse_auditor_json(text: str) -> dict:
    text = (text or "").strip()
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else {}
    except Exception:
        match = re.search(r"\{.*\}", text, re.S)
        if not match:
            return {}
        try:
            data = json.loads(match.group(0))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}


async def _run_prompt_auditor(context: dict) -> dict:
    fallback = _fallback_prompt_audit(context)
    audit_prompt = (
        "Eres un auditor experto de prompts de vigilancia por cámara. "
        "Tu trabajo es mejorar confiabilidad, reducir falsas alarmas y mantener alertas basadas solo en evidencia observable. "
        "Devuelve SOLO JSON válido con estas claves: score, diagnosis, suggested_prompt, suggested_rules, suggested_yolo_triggers, suggested_cooldown_min, tests_needed, apply_recommendation. "
        "No inventes datos. Si el contexto no alcanza, recomienda mejora conservadora.\n\n"
        "CONTEXTO JSON:\n" + json.dumps(context, ensure_ascii=False, indent=2)
    )
    try:
        async with httpx.AsyncClient(timeout=45) as client:
            resp = await client.post(PROMPT_AUDITOR_URL, json={
                "model": "qwen",
                "messages": [{"role": "user", "content": audit_prompt}],
                "max_tokens": 1800,
            })
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"]
            parsed = _parse_auditor_json(content)
            if not parsed:
                raise ValueError("auditor response empty")
            parsed["success"] = True
            parsed["source"] = "qwen"
            parsed["auditor_model"] = "qwen"
            parsed["suggested_prompt"] = parsed.get("suggested_prompt") or fallback["suggested_prompt"]
            parsed["suggested_rules"] = parsed.get("suggested_rules") or fallback["suggested_rules"]
            parsed["suggested_yolo_triggers"] = parsed.get("suggested_yolo_triggers") or fallback["suggested_yolo_triggers"]
            parsed["suggested_cooldown_min"] = parsed.get("suggested_cooldown_min") or fallback["suggested_cooldown_min"]
            parsed["tests_needed"] = parsed.get("tests_needed") or fallback["tests_needed"]
            return parsed
    except Exception as e:
        fallback["warning"] = f"Auditor no disponible: {e}"
        return fallback


def _admin_prompt_audit_context(user_id: str, camera_id: str) -> dict:
    uf = find_user_json(user_id)
    user_data = {}
    camera_record = {}
    if uf and uf.exists():
        try:
            user_data = json.load(open(uf))
        except Exception:
            user_data = {}
    for cam in user_data.get("cameras", []):
        if cam.get("camera_id") == camera_id:
            camera_record = cam
            break
    cam_cfg = get_camera_config_static(user_id, camera_id) or {}
    config = _admin_camera_config_payload(user_data, camera_record, cam_cfg)
    recent_frames = _get_recent_frames(user_id, camera_id, limit=8, minutes=45)
    recent_alerts = []
    for _cam_id, events_dir in resolve_user_events_dirs(user_id):
        if not events_dir.exists():
            continue
        for fname in sorted(events_dir.glob("*.json"), key=lambda x: (_load_json_safely(x).get("timestamp", 0) if x.name.endswith(".json") else 0), reverse=True):
            try:
                ev = json.load(open(fname))
            except Exception:
                continue
            if not _event_violation(ev):
                continue
            if ev.get("camera_id") != camera_id and _cam_id != camera_id:
                continue
            recent_alerts.append({
                "event_id": ev.get("event_id"),
                "timestamp": ev.get("timestamp"),
                "description": ev.get("description") or (ev.get("qwen_analysis") or {}).get("summary", ""),
                "importance": ev.get("importance"),
                "confirmed": bool(ev.get("confirmed")),
                "dismissed": bool(ev.get("dismissed")),
            })
            if len(recent_alerts) >= 20:
                break
        if len(recent_alerts) >= 20:
            break
    return {
        "user": {
            "name": user_data.get("name", ""),
            "business_name": user_data.get("business_name", ""),
            "business_type": user_data.get("business_type", ""),
            "main_concerns": user_data.get("main_concerns", []),
            "schedule": user_data.get("schedule", {}),
        },
        "camera": {
            "camera_id": camera_id,
            "name": camera_record.get("name", camera_id),
            "zone": camera_record.get("zone", ""),
            "physical_camera_id": camera_record.get("physical_camera_id", ""),
        },
        "business_name": user_data.get("business_name", ""),
        "business_type": user_data.get("business_type", ""),
        "zone": camera_record.get("zone", ""),
        "config": config,
        "audit": _monitoring_camera_audit(user_id, camera_id),
        "recent_frames": [{"timestamp": f.get("timestamp"), "size": f.get("size"), "index": f.get("index")} for f in recent_frames],
        "recent_alerts": recent_alerts,
    }


@app.get("/admin/monitoring/overview")
async def admin_monitoring_overview():
    cameras = _monitoring_camera_records(limit=1000)
    now = time.time()
    start_of_today = int(datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    alerts = []
    users = set()
    for disk in get_disk_config().get("disks", []):
        users_base = Path(disk.get("mount", "")) / disk.get("user_folder", "users")
        if not users_base.is_dir():
            continue
        for uid_dir in users_base.iterdir():
            user_file = uid_dir / "user.json"
            if not user_file.is_file():
                continue
            uid = uid_dir.name
            users.add(uid)
            for _cam_id, events_dir in resolve_user_events_dirs(uid):
                if not events_dir.exists():
                    continue
                for fname in events_dir.glob("*.json"):
                    try:
                        ev = _load_json_safely(fname) or {}
                    except Exception:
                        continue
                    if _event_violation(ev):
                        ts = int(ev.get("timestamp", 0) or 0)
                        if ts >= start_of_today:
                            alerts.append(ev)
    metrics = _load_admin_metrics()
    review_flags = [m for m in metrics.get("cameras", {}).values() if m.get("audit_flags")]
    queue_status = await admin_queue_status()
    return {
        "users": len(users),
        "cameras_total": len(cameras),
        "cameras_online": sum(1 for c in cameras if c["active"]),
        "cameras_offline": sum(1 for c in cameras if not c["active"]),
        "cameras_offline_15m": sum(1 for c in cameras if c.get("offline_seconds") and c["offline_seconds"] > 900),
        "alerts_today": len(alerts),
        "alerts_last_hour": sum(1 for a in alerts if int(a.get("timestamp", 0) or 0) >= now - 3600),
        "audit": {
            "review_needed_cameras": len(review_flags),
            "false_alarm_reviews": sum(int(m.get("false_alarm_count", 0)) for m in metrics.get("cameras", {}).values()),
            "total_event_reviews": sum(int(m.get("total_reviews", 0)) for m in metrics.get("cameras", {}).values()),
        },
        "queue": {
            "frame_count": queue_status.get("queue_length", 0),
            "firebase_pending": (queue_status.get("firebase_queue") or {}).get("pending_frames", 0),
            "grid_ready": queue_status.get("grid_ready", False),
        }
    }

@app.get("/admin/monitoring/cameras")
async def admin_monitoring_cameras(limit: int = 100, status: str = None):
    cameras = _monitoring_camera_records(limit=limit)
    if status:
        wanted = status == "online"
        cameras = [c for c in cameras if c.get("active") == wanted]
    return {"cameras": cameras, "total": len(cameras)}

@app.get("/admin/monitoring/cameras/{camera_id}")
async def admin_monitoring_camera_detail(camera_id: str, user_id: str = None, recent_frames: int = 12, recent_alerts: int = 12):
    owner_uid = None
    cam_data = None
    user_data = None
    for c in _monitoring_camera_records(limit=1000):
        if c.get("camera_id") == camera_id and (not user_id or c.get("user_id") == user_id):
            cam_data = c
            owner_uid = c.get("user_id")
            break
    if not owner_uid:
        raise HTTPException(status_code=404, detail="Camara no encontrada")
    uf = find_user_json(owner_uid)
    if uf and uf.exists():
        with open(uf) as f:
            user_data = json.load(f)
    camera_record = None
    for cam in user_data.get("cameras", []):
        if cam.get("camera_id") == camera_id:
            camera_record = cam
            break
    cam_cfg = get_camera_config_static(owner_uid, camera_id) or {}
    normalized_cfg = normalize_camera_vigilance_config(cam_cfg)
    display_config = _admin_camera_config_payload(user_data, camera_record or {}, cam_cfg)
    recent = _get_recent_frames(owner_uid, camera_id, limit=max(recent_frames, 1), minutes=45)
    alerts = []
    for _cam_id, events_dir in resolve_user_events_dirs(owner_uid):
        if not events_dir.exists():
            continue
        for fname in sorted(events_dir.glob("*.json"), key=lambda x: (_load_json_safely(x).get("timestamp", 0) if x.name.endswith(".json") else 0), reverse=True):
            try:
                ev = json.load(open(fname))
            except Exception:
                continue
            if not _event_violation(ev):
                continue
            if ev.get("camera_id") != camera_id and _cam_id != camera_id:
                continue
            ev["_events_dir"] = str(events_dir)
            ev = _enrich_event(ev, owner_uid, {camera_id: (camera_record or {}).get("name", camera_id)}, _cam_id)
            ev["image_url"] = f"/admin/events/{ev.get('event_id')}/image?user_id={owner_uid}"
            alerts.append(ev)
            if len(alerts) >= max(recent_alerts, 1):
                break
        if len(alerts) >= max(recent_alerts, 1):
            break
    audit = _monitoring_camera_audit(owner_uid, camera_id)
    return {
        **(cam_data or {}),
        "user": user_data or {},
        "camera_record": camera_record or {},
        "config": display_config,
        "recent_frames": recent,
        "recent_alerts": alerts,
        "audit": audit,
        "audit_score": "ok" if not audit.get("audit_flags") else "review_needed",
        "audit_flags": audit.get("audit_flags", []),
    }


@app.post("/admin/cameras/{camera_id}/prompt-audit")
async def admin_prompt_audit(camera_id: str, request: dict):
    uid, canonical = find_camera_owner(camera_id)
    if not uid:
        raise HTTPException(status_code=404, detail="Camara no encontrada")
    if request.get("user_id") and request.get("user_id") != uid:
        raise HTTPException(status_code=403, detail="Camara no pertenece al usuario indicado")
    context = _admin_prompt_audit_context(uid, canonical)
    result = await _run_prompt_auditor(context)
    result["camera_id"] = canonical
    result["user_id"] = uid
    return result


@app.post("/admin/cameras/{camera_id}/prompt-apply")
async def admin_prompt_apply(camera_id: str, request: dict):
    uid, canonical = find_camera_owner(camera_id)
    if not uid:
        raise HTTPException(status_code=404, detail="Camara no encontrada")
    if request.get("user_id") and request.get("user_id") != uid:
        raise HTTPException(status_code=403, detail="Camara no pertenece al usuario indicado")
    uf = find_user_json(uid)
    if not uf or not uf.exists():
        raise HTTPException(status_code=404, detail="Usuario no encontrado")
    with open(uf) as f:
        ud = json.load(f)
    cam_cfg = get_camera_config_static(uid, canonical) or {}
    prompt = request.get("prompt") or request.get("suggested_prompt") or cam_cfg.get("system_prompt") or cam_cfg.get("vigilance_prompt") or ""
    rules = request.get("rules") or request.get("rules_es") or request.get("suggested_rules") or cam_cfg.get("rules") or cam_cfg.get("rules_es") or []
    yolo_triggers = request.get("yolo_triggers") or request.get("suggested_yolo_triggers") or cam_cfg.get("yolo_triggers") or ["person"]
    cooldown = int(request.get("cooldown_min") or request.get("suggested_cooldown_min") or cam_cfg.get("cooldown_min") or 5)
    cam_cfg["system_prompt"] = prompt
    cam_cfg["vigilance_prompt"] = prompt
    cam_cfg["rules"] = rules
    cam_cfg["rules_es"] = rules
    cam_cfg["yolo_triggers"] = yolo_triggers
    cam_cfg["cooldown_min"] = cooldown
    vigilance = cam_cfg.setdefault("vigilance", {})
    if isinstance(vigilance, dict):
        normal_mode = vigilance.setdefault("normal_mode", {})
        sentinel_mode = vigilance.setdefault("sentinel_mode", {})
        if isinstance(normal_mode, dict):
            normal_mode["cooldown_min"] = cooldown
            normal_mode["yolo_triggers"] = yolo_triggers
        if isinstance(sentinel_mode, dict):
            sentinel_mode["cooldown_min"] = cooldown
            sentinel_mode["yolo_triggers"] = yolo_triggers
    _save_camera_config_static(uid, canonical, cam_cfg)
    ud["vigilance_prompt"] = prompt
    ud["rules_es"] = rules
    for cam in ud.get("cameras", []):
        if cam.get("camera_id") == canonical:
            cam["cooldown_min"] = cooldown
            break
    with open(uf, "w") as f:
        json.dump(ud, f, indent=2, ensure_ascii=False)
    version = _append_prompt_version(uid, canonical, {
        "applied": True,
        "reason": request.get("reason") or "aplicar mejora desde admin",
        "source": request.get("source") or "admin",
        "score": request.get("score"),
        "diagnosis": request.get("diagnosis") or [],
        "prompt": prompt,
        "rules": rules,
        "rules_es": rules,
        "yolo_triggers": yolo_triggers,
        "cooldown_min": cooldown,
    })
    _audit_log("admin_prompt_apply", actor="admin", target=canonical, data={"user_id": uid, "version": version.get("version")})
    return {"success": True, "camera_id": canonical, "user_id": uid, "version": version}


@app.get("/admin/cameras/{camera_id}/prompt-versions")
async def admin_prompt_versions(camera_id: str, user_id: str = None):
    uid, canonical = find_camera_owner(camera_id)
    if not uid:
        raise HTTPException(status_code=404, detail="Camara no encontrada")
    if user_id and user_id != uid:
        raise HTTPException(status_code=403, detail="Camara no pertenece al usuario indicado")
    data = _load_prompt_versions(uid, canonical)
    return {"success": True, "camera_id": canonical, "user_id": uid, "versions": data.get("versions", [])}


def _find_event_file(event_id: str, user_id: str = None):
    for disk in get_disk_config().get("disks", []):
        users_base = Path(disk.get("mount", "")) / disk.get("user_folder", "users")
        if not users_base.is_dir():
            continue
        for uid_dir in users_base.iterdir():
            uid = uid_dir.name
            if user_id and uid != user_id:
                continue
            for _cam_id, events_dir in resolve_user_events_dirs(uid):
                ef = events_dir / f"{event_id}.json"
                img = events_dir / f"{event_id}.jpg"
                if ef.exists():
                    return uid, _cam_id, ef, img
    return None, None, None, None

@app.get("/admin/events/{event_id}/image")
async def admin_event_image(event_id: str, user_id: str = None):
    uid, _cam_id, _ef, img = _find_event_file(event_id, user_id)
    if not img or not img.exists():
        return Response(status_code=204)
    return FileResponse(str(img), media_type="image/jpeg", headers={"Cache-Control": "no-store, no-cache, must-revalidate"})

@app.get("/admin/monitoring/alerts")
async def admin_monitoring_alerts(limit: int = 50, hours: int = 24, user_id: str = None, camera_id: str = None):
    now = int(time.time())
    since = now - (hours * 3600)
    alerts = []
    for disk in get_disk_config().get("disks", []):
        users_base = Path(disk.get("mount", "")) / disk.get("user_folder", "users")
        if not users_base.is_dir():
            continue
        for uid_dir in users_base.iterdir():
            uid = uid_dir.name
            if user_id and uid != user_id:
                continue
            user_file = uid_dir / "user.json"
            cam_names = {}
            if user_file.is_file():
                try:
                    with open(user_file) as f:
                        ud = json.load(f)
                    cam_names = {c.get("camera_id", ""): c.get("name", "") for c in ud.get("cameras", [])}
                except Exception:
                    pass
            for cam_id, events_dir in resolve_user_events_dirs(uid):
                if not events_dir.exists():
                    continue
                for fname in sorted(events_dir.glob("*.json"), key=lambda x: (_load_json_safely(x).get("timestamp", 0) if x.name.endswith(".json") else 0), reverse=True):
                    try:
                        ev = json.load(open(fname))
                    except Exception:
                        continue
                    if not _event_violation(ev):
                        continue
                    ts = int(ev.get("timestamp", 0) or 0)
                    if ts < since:
                        continue
                    if camera_id and ev.get("camera_id") != camera_id and cam_id != camera_id:
                        continue
                    ev["_events_dir"] = str(events_dir)
                    ev = _enrich_event(ev, uid, cam_names, cam_id)
                    ev["image_url"] = f"/admin/events/{ev.get('event_id')}/image?user_id={uid}"
                    alerts.append(ev)
                    if len(alerts) >= max(limit, 1):
                        return {"alerts": alerts, "total": len(alerts)}
    return {"alerts": alerts, "total": len(alerts)}

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

@app.post("/admin/users")
async def admin_create_user(request: dict):
    cfg = get_disk_config()
    now_ts = int(time.time())
    uid = "adm_" + str(now_ts) + "_" + secrets.token_hex(4)
    plan = request.get("plan") or "free"
    plan_def = cfg.get("plans", {}).get(plan, {})
    duration = int(request.get("duration_days") or plan_def.get("duration_days", 30) or 30)
    plan_end = now_ts + (duration * 86400) if duration > 0 else 0
    access_token = request.get("access_token") or ("oj_live_" + secrets.token_urlsafe(32))
    schedule_open = request.get("schedule_open") or request.get("open") or "08:00"
    schedule_close = request.get("schedule_close") or request.get("close") or "22:00"
    main_concerns = request.get("main_concerns") or []
    if isinstance(main_concerns, str):
        main_concerns = [c.strip() for c in main_concerns.split(",") if c.strip()]
    storage_path = get_user_storage_path(uid, plan)
    storage_path.mkdir(parents=True, exist_ok=True)
    user_data = {
        "user_id": uid,
        "name": request.get("name") or "Usuario Admin",
        "email": request.get("email") or "",
        "phone": request.get("phone") or "",
        "business_name": request.get("business_name") or request.get("business") or "Negocio",
        "business_type": request.get("business_type") or "retail",
        "main_concerns": main_concerns,
        "employee_count": request.get("employee_count") or "1",
        "camera_expected_count": request.get("camera_expected_count") or 1,
        "plan": plan,
        "status": request.get("status") or "active",
        "created_at": str(now_ts),
        "plan_start": now_ts,
        "plan_end": plan_end,
        "trial_end": None,
        "billing_cycle": request.get("billing_cycle") or "monthly",
        "grace_period_days": request.get("grace_period_days") or cfg.get("grace_period_days", 3),
        "next_due": plan_end,
        "payments": [],
        "last_payment": None,
        "access_token": access_token,
        "schedule": request.get("schedule") or {"open": schedule_open, "close": schedule_close},
        "cameras": request.get("cameras") or [],
        "fcm_tokens": [],
        "storage_path": str(storage_path),
        "disk_mount": str(storage_path.parent),
        "vigilance_rules": request.get("vigilance_rules") or _generate_rules_es(request.get("business_type") or "retail", main_concerns),
        "vigilance_prompt": request.get("vigilance_prompt") or _build_initial_prompt(request.get("business_type") or "retail", main_concerns, request.get("vigilance_rules") or [], schedule_open, schedule_close),
        "rules_es": request.get("rules_es") or _generate_rules_es(request.get("business_type") or "retail", main_concerns),
    }
    with open(storage_path / "user.json", "w") as f:
        json.dump(user_data, f, indent=2, ensure_ascii=False)
    compat_dir = STORAGE_ROOT / "users" / uid
    compat_dir.mkdir(parents=True, exist_ok=True)
    with open(compat_dir / "user.json", "w") as f:
        json.dump(user_data, f, indent=2, ensure_ascii=False)
    _audit_log("admin_user_create", actor="admin", target=uid, data={"plan": plan, "email": user_data.get("email", "")})
    return {"success": True, "user_id": uid, "access_token": access_token, "user": user_data}

@app.put("/admin/users/{user_id}")
async def admin_update_user(user_id: str, request: dict):
    user_file = find_user_json(user_id)
    if not user_file or not user_file.exists():
        raise HTTPException(status_code=404, detail="User not found")
    with open(user_file) as f:
        ud = json.load(f)
    updatable = ["name", "email", "phone", "business_name", "business_type", "main_concerns", "employee_count", "camera_expected_count", "plan", "status", "plan_end", "trial_end", "billing_cycle", "grace_period_days", "next_due", "schedule", "what_to_monitor", "schedule_open", "schedule_close", "access_token"]
    for field in updatable:
        if field in request:
            if field == "main_concerns" and isinstance(request[field], str):
                ud[field] = [c.strip() for c in request[field].split(",") if c.strip()]
            elif field in ("schedule_open", "schedule_close"):
                ud.setdefault("schedule", {})["open" if field == "schedule_open" else "close"] = request[field]
            else:
                ud[field] = request[field]
    with open(user_file, "w") as f:
        json.dump(ud, f, indent=2, ensure_ascii=False)
    compat = STORAGE_ROOT / "users" / user_id / "user.json"
    if compat.exists():
        with open(compat, "w") as f:
            json.dump(ud, f, indent=2, ensure_ascii=False)
    _audit_log("admin_user_update", actor="admin", target=user_id, data={"fields": list(request.keys())})
    return {"success": True, "user": ud}

@app.delete("/admin/users/{user_id}")
async def admin_delete_user_full(user_id: str):
    user_file = find_user_json(user_id)
    if not user_file or not user_file.exists():
        raise HTTPException(status_code=404, detail="User not found")
    try:
        auth.delete_user(user_id)
    except Exception:
        pass
    for path in [user_file.parent, STORAGE_ROOT / "users" / user_id]:
        if path.exists() and path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
    _audit_log("admin_user_delete", actor="admin", target=user_id, data={})
    return {"success": True, "message": f"Usuario {user_id} eliminado"}

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

# ── Firebase Firestore Queue Processor ───────────────────────────────────────

FIRESTORE_PROJECT_ID = "ojoia-67216"
FIRESTORE_FRAME_COLLECTION = "system/frame_queue/frames"

class _QueueRequest:
    def __init__(self, headers=None, client_ip="127.0.0.1"):
        self.headers = headers or {}
        self.client = type("Client", (), {"host": client_ip})()

def _firestore_frame_collection():
    from firebase_admin import firestore
    db = firestore.client()
    return db.collection("system").document("frame_queue").collection("frames")

def _firestore_queue_summary(limit=500):
    try:
        frames = []
        total_kb = 0
        docs = list(_firestore_frame_collection().order_by("queued_at").limit(limit).stream())
        for doc in docs:
            data = doc.to_dict() or {}
            size_kb = float(data.get("size_kb", 0) or 0)
            total_kb += size_kb
            frames.append({
                "id": doc.id,
                "camera_id": data.get("camera_id", "unknown"),
                "session_id": data.get("session_id", ""),
                "timestamp": data.get("timestamp", ""),
                "queued_at": data.get("queued_at", ""),
                "size_kb": size_kb,
                "status": data.get("status", "pending")
            })
        return {"success": True, "queue_length": len(frames), "total_kb": round(total_kb, 1), "frames": frames}
    except Exception as e:
        logger.error(f"Firebase Firestore queue status error: {e}")
        return {"success": False, "error": str(e), "queue_length": 0, "total_kb": 0, "frames": []}

@app.get("/admin/queue/firestore/status")
async def admin_firestore_queue_status():
    return _firestore_queue_summary()

@app.post("/admin/queue/firestore/process")
async def admin_process_firestore_queue(request: Request = None):
    try:
        from io import BytesIO
        import base64

        docs = list(_firestore_frame_collection().order_by("queued_at").limit(FIREBASE_QUEUE_BATCH_SIZE).stream())
        if not docs:
            return {"success": True, "processed": 0, "message": "No frames in Firestore queue"}

        results = []
        for doc in docs:
            data = doc.to_dict() or {}
            frame_id = doc.id
            camera_id = data.get("camera_id", "unknown")
            session_id = data.get("session_id", "")
            try:
                image_b64 = data.get("image_base64", "")
                if not image_b64:
                    doc.reference.delete()
                    results.append({"frame_id": frame_id, "camera_id": camera_id, "session_id": session_id, "status": "deleted_corrupt", "error": "missing image_base64"})
                    continue
                img_bytes = base64.b64decode(image_b64, validate=True)
                if not img_bytes:
                    doc.reference.delete()
                    results.append({"frame_id": frame_id, "camera_id": camera_id, "session_id": session_id, "status": "deleted_corrupt", "error": "empty image"})
                    continue

                headers = {}
                for src, dst in (("quality", "x-quality"), ("framesize", "x-framesize"), ("h_mirror", "x-hmirror"), ("v_flip", "x-vflip"), ("led", "x-led"), ("uptime", "x-uptime")):
                    if data.get(src) is not None:
                        headers[dst] = str(data.get(src))
                fake_request = _QueueRequest(headers=headers, client_ip="firestore-queue")
                image = UploadFile(filename=f"{camera_id}.jpg", file=BytesIO(img_bytes))
                result = await _process_ingest(fake_request, camera_id, "default", image)
                if result.get("success"):
                    doc.reference.delete()
                else:
                    doc.reference.update({"status": "error", "error": str(result.get("error", "unknown")), "updated_at": datetime.utcnow().isoformat() + "Z"})
                results.append({
                    "frame_id": frame_id,
                    "camera_id": camera_id,
                    "session_id": session_id,
                    "size_kb": round(len(img_bytes) / 1024, 1),
                    "status": "processed" if result.get("success") else "error",
                    "result": result
                })
            except Exception as frame_err:
                logger.error(f"Error processing Firestore frame {frame_id}: {frame_err}")
                try:
                    doc.reference.delete()
                except Exception:
                    pass
                results.append({
                    "frame_id": frame_id,
                    "camera_id": camera_id,
                    "session_id": session_id,
                    "status": "deleted_error",
                    "error": str(frame_err)
                })

        processed = len([r for r in results if r["status"] == "processed"])
        errors = len([r for r in results if r["status"] in ("error", "deleted_error", "deleted_corrupt")])
        return {"success": True, "processed": processed, "errors": errors, "results": results}
    except Exception as e:
        logger.error(f"Firebase Firestore queue process error: {e}")
        return {"success": False, "error": str(e), "processed": 0}

@app.post("/admin/queue/firestore/clear")
async def admin_clear_firestore_queue():
    try:
        from firebase_admin import firestore
        docs = list(_firestore_frame_collection().stream())
        db = firestore.client()
        batch_size = 450
        batches = 0
        batch = None
        for i, doc in enumerate(docs):
            if i % batch_size == 0:
                if batch is not None:
                    batch.commit()
                    batches += 1
                batch = db.batch()
            batch.delete(doc.reference)
        if batch is not None:
            batch.commit()
            batches += 1
        return {"success": True, "deleted": len(docs), "batches": batches}
    except Exception as e:
        logger.error(f"Firebase Firestore queue clear error: {e}")
        return {"success": False, "error": str(e)}


# ── Firebase Storage Queue Processor ──────────────────────────────────────

def _save_camera_cooldown(user_id: str, camera_id: str, cooldown_min: int):
    cam_cfg = get_camera_config_static(user_id, camera_id) or {}
    cam_cfg["cooldown_min"] = cooldown_min
    vigilance = cam_cfg.setdefault("vigilance", {})
    if isinstance(vigilance, dict):
        for mode_key in ("normal_mode", "sentinel_mode"):
            mode = vigilance.get(mode_key)
            if isinstance(mode, dict):
                mode["cooldown_min"] = cooldown_min
    _save_camera_config_static(user_id, camera_id, cam_cfg)

    uf = STORAGE_ROOT / "users" / user_id / "user.json"
    if uf.exists():
        ud = json.loads(uf.read_text())
        updated = False
        for c in ud.get("cameras", []):
            if c.get("camera_id") == camera_id:
                c["cooldown_min"] = cooldown_min
                updated = True
                break
        if not updated and ud.get("cameras"):
            ud["cameras"][0]["cooldown_min"] = cooldown_min
        _save_json_safely(uf, ud)
    return {"ok": True, "cooldown_min": cooldown_min}


@app.post("/api/cameras/{camera_id}/cooldown")
async def save_camera_cooldown(camera_id: str, request: dict):
    try:
        user_id = request.get("user_id", "")
        cooldown_min = max(5, min(60, int(request.get("cooldown_min", 5))))
        if not user_id:
            return JSONResponse(status_code=400, content={"ok": False, "error": "user_id required"})
        uf = STORAGE_ROOT / "users" / user_id / "user.json"
        if not uf.exists():
            return JSONResponse(status_code=404, content={"ok": False, "error": "User not found"})
        return JSONResponse(status_code=200, content=_save_camera_cooldown(user_id, camera_id, cooldown_min))
    except Exception as e:
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})

@app.get("/admin/eva-config")
async def admin_eva_config():
    cfg = _load_eva_config()
    return {"prompt": cfg.get("prompt", ""), "docs": cfg.get("docs", []), "violation_cooldown_min": cfg.get("violation_cooldown_min", 5), "analysis_interval_s": cfg.get("analysis_interval_s", 3), "grid_size": cfg.get("grid_size", 16)}

def _load_eva_config() -> dict:
    try:
        with open(EVA_CONFIG_FILE) as f:
            return json.load(f)
    except Exception:
        return {}

def _save_eva_config(cfg: dict):
    EVA_CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(EVA_CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)

@app.get("/admin/system/analysis-interval")
async def admin_get_analysis_interval():
    cfg = _load_eva_config()
    interval = float(cfg.get("analysis_interval_s", 3))
    grid_size = int(cfg.get("grid_size", 16))
    return {
        "success": True,
        "analysis_interval_s": interval,
        "grid_size": grid_size,
        "estimated_grid_fill_s": round(interval * grid_size, 1)
    }

@app.post("/admin/system/analysis-interval")
async def admin_set_analysis_interval(request: dict):
    cfg = _load_eva_config()
    try:
        interval = float(request.get("analysis_interval_s", cfg.get("analysis_interval_s", 3)))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="analysis_interval_s must be a number")
    interval = max(1.0, min(10.0, interval))
    try:
        grid_size = int(request.get("grid_size", cfg.get("grid_size", 16)))
    except (TypeError, ValueError):
        grid_size = 16
    grid_size = max(4, min(32, grid_size))
    cfg["analysis_interval_s"] = interval
    cfg["grid_size"] = grid_size
    _save_eva_config(cfg)
    return {"success": True, "analysis_interval_s": interval, "grid_size": grid_size, "estimated_grid_fill_s": round(interval * grid_size, 1)}


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
        for fname in sorted(events_dir.iterdir(), key=lambda x: (_load_json_safely(x).get("timestamp", 0) if x.name.endswith(".json") else 0), reverse=True):
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
        for fname in sorted(events_dir.iterdir(), key=lambda x: (_load_json_safely(x).get("timestamp", 0) if x.name.endswith(".json") else 0), reverse=True):
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
async def admin_process_firebase_queue(request: Request):
    """Process pending frames from Firebase Storage."""
    try:
        from firebase_admin import storage as fb_storage
        from io import BytesIO
        import tempfile, os

        bucket_name = FIREBASE_QUEUE_BUCKET
        bucket = fb_storage.bucket(bucket_name)
        blobs = list(bucket.list_blobs(prefix=FIREBASE_QUEUE_PREFIX, max_results=FIREBASE_QUEUE_BATCH_SIZE))

        if not blobs:
            return {"success": True, "processed": 0, "message": "No frames in queue"}

        results = []
        for blob in blobs:
            parts = blob.name.split("/")
            camera_id = parts[1] if len(parts) > 1 else "unknown"
            session_id = parts[2] if len(parts) > 2 else ""
            filename = parts[3] if len(parts) > 3 else blob.name
            user_id = "default"

            with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
                tmp_path = tmp.name

            try:
                blob.download_to_filename(tmp_path)
                with open(tmp_path, "rb") as f:
                    img_bytes = f.read()

                image = UploadFile(filename=filename, file=BytesIO(img_bytes))
                result = await _process_ingest(request, camera_id, user_id, image)

                blob.delete()

                results.append({
                    "camera_id": camera_id,
                    "session_id": session_id,
                    "path": blob.name,
                    "size_kb": round(len(img_bytes) / 1024, 1),
                    "status": "processed" if result.get("success") else "error",
                    "result": result
                })
                logger.info(f"Processed frame: {camera_id} from Firebase queue")

            except Exception as frame_err:
                logger.error(f"Error processing frame {blob.name}: {frame_err}")
                results.append({
                    "camera_id": camera_id,
                    "session_id": session_id,
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
    """Full queue status: local + Firebase Firestore."""
    frame_count = sum(g.get_frame_count() for g in orchestrator.grids.values())
    firestore_summary = _firestore_queue_summary(limit=500)
    fb_queue_length = firestore_summary.get("queue_length", 0)
    fb_total_kb = firestore_summary.get("total_kb", 0)

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
            "bucket": FIRESTORE_FRAME_COLLECTION,
            "success": firestore_summary.get("success", False),
            "error": firestore_summary.get("error", "")
        }
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8005)

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

# ── IDENTITY WAN PROXY ────────────────────────────────────────
# Proxy al módulo identity_wan (puerto 8025) para evitar CORS desde el frontend

IDW_URL = "http://localhost:8025"

@app.get("/test-identity")
async def test_identity_page():
    """Sirve la página de prueba de identity_wan"""
    from fastapi.responses import HTMLResponse
    test_path = Path(__file__).parent.parent / "ojoia" / "frontend" / "test-identity" / "index.html"
    if not test_path.exists():
        test_path = Path("/home/sam/ojoia-eva/frontend/test-identity/index.html")
    if test_path.exists():
        return HTMLResponse(test_path.read_text())
    raise HTTPException(404, "test-identity.html no encontrado")

@app.get("/api/idw/health")
async def idw_health():
    """Proxy: health del módulo identity_wan"""
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(f"{IDW_URL}/health")
            return resp.json()
    except Exception as e:
        return {"status": "error", "detail": str(e)}

@app.post("/api/idw/lynx/prepare")
async def idw_lynx_prepare(request: dict):
    """Proxy: preparar Lynx/FaceID"""
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(f"{IDW_URL}/api/identity/lynx/prepare", json=request)
            return resp.json()
    except Exception as e:
        raise HTTPException(502, str(e))

@app.post("/api/idw/lynx/encode-face")
async def idw_lynx_encode_face(request: dict):
    """Proxy: codificar cara con Lynx"""
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(f"{IDW_URL}/api/identity/lynx/encode-face", json=request)
            return resp.json()
    except Exception as e:
        raise HTTPException(502, str(e))

@app.post("/api/idw/ipadapter/prepare")
async def idw_ipadapter_prepare(request: dict):
    """Proxy: preparar IP-Adapter"""
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(f"{IDW_URL}/api/identity/ipadapter/prepare", json=request)
            return resp.json()
    except Exception as e:
        raise HTTPException(502, str(e))

@app.post("/api/idw/ipadapter/encode")
async def idw_ipadapter_encode(request: dict):
    """Proxy: codificar IP-Adapter"""
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(f"{IDW_URL}/api/identity/ipadapter/encode", json=request)
            return resp.json()
    except Exception as e:
        raise HTTPException(502, str(e))

@app.post("/api/idw/wan/i2v-faceid/submit")
async def idw_wan_faceid_submit(request: dict):
    """Proxy: enviar job Wan I2V + FaceID"""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(f"{IDW_URL}/api/wan/i2v-faceid/submit", json=request)
            return resp.json()
    except Exception as e:
        raise HTTPException(502, str(e))

@app.post("/api/idw/wan/i2v-ipadapter/submit")
async def idw_wan_ipadapter_submit(request: dict):
    """Proxy: enviar job Wan I2V + IP-Adapter"""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(f"{IDW_URL}/api/wan/i2v-ipadapter/submit", json=request)
            return resp.json()
    except Exception as e:
        raise HTTPException(502, str(e))

@app.get("/api/idw/wan/{mode}/status/{job_id}")
async def idw_wan_job_status(mode: str, job_id: str):
    """Proxy: estado de job Wan"""
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(f"{IDW_URL}/api/wan/i2v-{mode}/status/{job_id}")
            return resp.json()
    except Exception as e:
        raise HTTPException(502, str(e))

@app.get("/api/idw/wan/{mode}/download/{job_id}")
async def idw_wan_download(mode: str, job_id: str):
    """Proxy: descargar video generado"""
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(f"{IDW_URL}/api/wan/i2v-{mode}/download/{job_id}")
            from fastapi.responses import StreamingResponse
            import io
            return StreamingResponse(io.BytesIO(resp.content), media_type="video/mp4",
                                     headers={"Content-Disposition": f"attachment; filename={job_id}.mp4"})
    except Exception as e:
        raise HTTPException(502, str(e))

# ── IDENTITY: REGISTRO E IDENTIFICACIÓN DE CARAS ──────────────

import uuid as _uuid

FACE_STORAGE_BASE = STORAGE_ROOT / "identity" / "faces"

def _face_dir(person_id: str) -> Path:
    d = FACE_STORAGE_BASE / person_id
    d.mkdir(parents=True, exist_ok=True)
    return d

@app.post("/api/identity/register-face")
async def register_face(request: dict):
    """Recibe imagen base64 y registra una persona"""
    try:
        person_name = request.get("person_name", "").strip()
        person_id = request.get("person_id", "").strip()
        image_b64 = request.get("image_b64", "")
        user_id = request.get("user_id", "z6q9KStIs1boz31q2fiHJREPBMH2")
        if not person_name:
            raise HTTPException(400, "person_name requerido")
        if not image_b64:
            raise HTTPException(400, "image_b64 requerido")
        if not person_id:
            person_id = re.sub(r'[^a-z0-9_]', '_', person_name.lower().strip())
            person_id = f"{person_id}_{_uuid.uuid4().hex[:6]}"
        fdir = _face_dir(person_id)
        img_data = base64.b64decode(image_b64.split(",")[-1])
        img_path = fdir / "face_registered.jpg"
        with open(img_path, "wb") as f:
            f.write(img_data)
        meta = {
            "person_id": person_id,
            "person_name": person_name,
            "user_id": user_id,
            "registered_at": int(time.time()),
            "image_path": str(img_path),
        }
        _save_json(fdir / "meta.json", meta)
        
        # Generar embedding automáticamente con InsightFace
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.post(f"{IDW_URL}/api/identity/encode-face", json={
                    "face_image_path": str(img_path),
                    "output_dir": str(fdir),
                })
                enc_result = r.json()
                if enc_result.get("status") == "ok":
                    meta["embedding_path"] = enc_result.get("embedding_path")
                    meta["embedding_dim"] = enc_result.get("embedding_dim")
                    _save_json(fdir / "meta.json", meta)
                    log.info(f"Face encoded: {person_name} (dim={enc_result.get('embedding_dim')})")
                else:
                    log.warning(f"Face encoding failed for {person_name}: {enc_result}")
        except Exception as e:
            log.warning(f"Face encoding error (non-critical): {e}")
        
        return {"status": "ok", "person_id": person_id, "person_name": person_name}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))

@app.get("/api/identity/list-faces")
async def list_faces(user_id: str = "z6q9KStIs1boz31q2fiHJREPBMH2"):
    """Lista caras registradas"""
    try:
        faces = []
        if FACE_STORAGE_BASE.exists():
            for d in FACE_STORAGE_BASE.iterdir():
                mp = d / "meta.json"
                if mp.exists():
                    m = _load_json(mp)
                    if m and m.get("user_id") == user_id:
                        faces.append(m)
        return {"status": "ok", "faces": faces}
    except Exception as e:
        raise HTTPException(500, str(e))

@app.post("/api/identity/identify-face")
async def identify_face(request: dict):
    """Identifica caras en un frame usando InsightFace"""
    try:
        image_b64 = request.get("image_b64", "")
        user_id = request.get("user_id", "z6q9KStIs1boz31q2fiHJREPBMH2")
        if not image_b64:
            raise HTTPException(400, "image_b64 requerido")
        
        # Decodificar imagen
        img_data = base64.b64decode(image_b64.split(",")[-1])
        frame_path = Path(f"/tmp/identify_{_uuid.uuid4().hex[:8]}.jpg")
        with open(frame_path, "wb") as f:
            f.write(img_data)
        
        # Usar el módulo identity_wan para identificar
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.post(f"{IDW_URL}/api/identity/identify", json={
                    "face_image_path": str(frame_path),
                    "user_id": user_id,
                    "threshold": 0.5,
                })
                result = r.json()
                frame_path.unlink(missing_ok=True)
                return result
        except Exception as e:
            frame_path.unlink(missing_ok=True)
            # Fallback: devolver caras registradas sin comparación
            faces = []
            if FACE_STORAGE_BASE.exists():
                for d in FACE_STORAGE_BASE.iterdir():
                    mp = d / "meta.json"
                    if mp.exists():
                        m = _load_json(mp)
                        if m and m.get("user_id") == user_id:
                            faces.append(m)
            return {
                "status": "ok",
                "identified": [],
                "faces_count": len(faces),
                "message": f"Face model unavailable: {e}",
            }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))

# ── FACE IMAGE con CORS ───────────────────────────────────────

@app.api_route("/api/identity/face-img/{person_id}", methods=["GET", "OPTIONS"])
async def get_face_img(person_id: str):
    """Devuelve la imagen de una cara registrada con CORS explícito"""
    img_path = FACE_STORAGE_BASE / person_id / "face_registered.jpg"
    if not img_path.exists():
        raise HTTPException(404, "Imagen no encontrada")
    from fastapi.responses import Response
    data = img_path.read_bytes()
    return Response(
        content=data,
        media_type="image/jpeg",
        headers={
            "Access-Control-Allow-Origin": "*",
            "Cache-Control": "no-cache, no-store",
            "Cross-Origin-Resource-Policy": "cross-origin",
        }
    )

# ═══════════════════════════════════════════════════════════════════════════
#  CHATRD — Chat con Qwen + Generación de imágenes SDXL
# ═══════════════════════════════════════════════════════════════════════════

from fastapi import APIRouter
from pydantic import BaseModel
from typing import Optional, List

chatrd_router = APIRouter()

QWEN_URL = "http://localhost:8004/v1"
COMFYUI_URL = "http://localhost:8007"
WHISPER_URL = "http://localhost:8008"

class ChatRequest(BaseModel):
    message: str
    image: Optional[str] = None
    history: Optional[List[dict]] = None
    model: str = "Qwen/Qwen2.5-VL-7B-Instruct-AWQ"

class ImageRequest(BaseModel):
    prompt: str
    negative_prompt: str = ""
    model: str = "turbo"
    width: int = 1024
    height: int = 1024
    steps: int = 30
    cfg: float = 7.0
    seed: Optional[int] = None

class ChatResponse(BaseModel):
    response: str
    model: str

class ImageResponse(BaseModel):
    image_url: str
    seed: int
    model: str

@chatrd_router.get("/api/chatrd/models")
async def chatrd_models():
    """Modelos disponibles para chat y generación"""
    return {
        "chat_models": [
            {"id": "Qwen/Qwen2.5-VL-7B-Instruct-AWQ", "name": "Qwen2.5-VL-7B", "type": "chat+vision"}
        ],
        "image_models": [
            {"id": "turbo", "name": "SDXL Turbo 1.0", "speed": "fast"},
            {"id": "juggernaut", "name": "JuggernautXL v10", "speed": "quality"},
            {"id": "flux", "name": "FLUX.1 Dev", "speed": "quality"}
        ]
    }

@chatrd_router.post("/api/chatrd/chat")
async def chatrd_chat(request: ChatRequest):
    """Chat con Qwen2.5-VL (soporta imágenes)"""
    messages = []
    if request.history:
        for msg in request.history[-10:]:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            if role == "user" and request.image and msg == request.history[-1]:
                messages.append({
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{request.image}"}},
                        {"type": "text", "text": content}
                    ]
                })
            else:
                messages.append({"role": role, "content": content})
    
    messages.append({"role": "user", "content": request.message})
    
    payload = {
        "model": request.model,
        "messages": messages,
        "max_tokens": 2048,
        "temperature": 0.7,
        "stream": False
    }
    
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(f"{QWEN_URL}/chat/completions", json=payload)
            data = resp.json()
            if "choices" in data and data["choices"]:
                reply = data["choices"][0]["message"]["content"]
            else:
                reply = str(data)
            return {"response": reply, "model": request.model}
    except Exception as e:
        raise HTTPException(502, f"Qwen error: {str(e)}")

@chatrd_router.post("/api/chatrd/image")
async def chatrd_image(request: ImageRequest):
    """Generar imagen con SDXL vía ComfyUI"""
    model_map = {
        "turbo": "sd_xl_turbo_1.0_fp16.safetensors",
        "juggernaut": "JuggernautXL_v10.safetensors",
        "flux": "flux1-dev.safetensors"
    }
    ckpt = model_map.get(request.model, "sd_xl_turbo_1.0_fp16.safetensors")
    seed = request.seed or int(time.time()) % 2147483647
    
    workflow = {
        "3": {
            "class_type": "KSampler",
            "inputs": {
                "seed": seed,
                "steps": request.steps,
                "cfg": request.cfg,
                "sampler_name": "euler",
                "scheduler": "normal",
                "denoise": 1.0,
                "model": ["4", 0],
                "positive": ["6", 0],
                "negative": ["7", 0],
                "latent_image": ["5", 0]
            }
        },
        "4": {
            "class_type": "CheckpointLoaderSimple",
            "inputs": {"ckpt_name": ckpt}
        },
        "5": {
            "class_type": "EmptyLatentImage",
            "inputs": {"width": request.width, "height": request.height, "batch_size": 1}
        },
        "6": {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": request.prompt, "clip": ["4", 1]}
        },
        "7": {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": request.negative_prompt or "blurry, bad quality, distorted", "clip": ["4", 1]}
        },
        "8": {
            "class_type": "VAEDecode",
            "inputs": {"samples": ["3", 0], "vae": ["4", 2]}
        },
        "9": {
            "class_type": "SaveImage",
            "inputs": {"images": ["8", 0], "filename_prefix": "chatrd"}
        }
    }
    
    try:
        async with httpx.AsyncClient(timeout=300) as client:
            resp = await client.post(f"{COMFYUI_URL}/prompt", json={"prompt": workflow})
            data = resp.json()
            prompt_id = data.get("prompt_id")
            if not prompt_id:
                raise HTTPException(502, f"ComfyUI error: {data}")
            
            for _ in range(120):
                await asyncio.sleep(1)
                r = await client.get(f"{COMFYUI_URL}/history/{prompt_id}")
                h = r.json()
                if prompt_id in h and h[prompt_id].get("outputs"):
                    outputs = h[prompt_id]["outputs"]
                    if "9" in outputs:
                        img_data = outputs["9"]["images"][0]
                        img_url = f"/api/chatrd/image-result/{prompt_id}/9"
                        return {
                            "image_url": img_url,
                            "seed": seed,
                            "model": request.model,
                            "prompt_id": prompt_id
                        }
            
            raise HTTPException(504, "Image generation timeout")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"ComfyUI error: {str(e)}")

@chatrd_router.get("/api/chatrd/image-result/{prompt_id}/{node_id}")
async def chatrd_image_result(prompt_id: str, node_id: str):
    """Obtener imagen generada por ComfyUI"""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"{COMFYUI_URL}/history/{prompt_id}"
            )
            h = resp.json()
            if prompt_id in h and node_id in h[prompt_id].get("outputs", {}):
                img_info = h[prompt_id]["outputs"][node_id]["images"][0]
                filename = img_info["filename"]
                subfolder = img_info.get("subfolder", "")
                folder_type = img_info.get("type", "output")
                r = await client.get(
                    f"{COMFYUI_URL}/view?filename={filename}&subfolder={subfolder}&type={folder_type}"
                )
                return Response(content=r.content, media_type="image/png")
            raise HTTPException(404, "Image not found")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, str(e))

@chatrd_router.post("/api/chatrd/transcribe")
async def chatrd_transcribe(file: UploadFile = File(...)):
    """Transcribir audio con Whisper"""
    try:
        contents = await file.read()
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                f"{WHISPER_URL}/transcribe",
                files={"file": (file.filename or "audio.wav", contents, "audio/wav")},
                data={"language": "es"}
            )
            return resp.json()
    except Exception as e:
        raise HTTPException(502, f"Whisper error: {str(e)}")

app.include_router(chatrd_router)
