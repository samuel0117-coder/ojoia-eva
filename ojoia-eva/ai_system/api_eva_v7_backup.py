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
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, List, Any

from fastapi import FastAPI, UploadFile, File, HTTPException, Form, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from starlette.status import HTTP_200_OK
from pydantic import BaseModel
import httpx
import firebase_admin
from firebase_admin import auth, credentials

# Importar modulos locales
from gateway_resize import resize_image, image_to_base64
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

# App FastAPI
app = FastAPI(title="OjoIA Eva API", version="7.0")

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Middleware para headers CORS y no-cache
@app.middleware("http")
async def add_cors_and_no_cache(request: Request, call_next):
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

# Helpers de Almacenamiento
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
    """Resolver user_id desde camera_id si provided es default."""
    if provided_user_id and provided_user_id != "default":
        return provided_user_id
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
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get("http://localhost:8004/health")
            status = "ok" if resp.status_code == 200 else "degraded"
    except:
        status = "error"
    return {"status": status, "service": "eva-api", "version": "7.0"}

@app.get("/frames/latest")
async def get_latest_frame(camera_id: Optional[str] = None):
    frame_bytes = orchestrator.grid.get_last_frame_bytes()
    last_cam = orchestrator.grid.get_last_camera_id()
    if camera_id and last_cam != camera_id:
        frame_bytes = b""
    image_b64 = base64.b64encode(frame_bytes).decode() if frame_bytes else ""
    return {
        "success": bool(frame_bytes),
        "image_b64": image_b64,
        "camera_id": last_cam,
        "yolo": {"count": orchestrator.grid.get_last_yolo_count()},
        "metadata": {"timestamp": datetime.now().isoformat()}
    }

@app.get("/grid/latest")
async def get_latest_grid(partial: int = 1):
    info = orchestrator.grid.get_grid_info()
    grid_b64 = ""
    if info["frame_count"] > 0:
        grid_img = orchestrator.grid.get_grid_image()
        grid_b64 = base64.b64encode(grid_img).decode()
    return {
        "frames_used": info["frame_count"],
        "grid_b64": grid_b64,
        "camera_ids": info["camera_ids"],
        "partial": bool(partial)
    }

# Auth Firebase
@app.post("/auth/firebase/verify")
async def verify_firebase(request: dict):
    id_token = request.get("id_token") or request.get("idToken")
    if not id_token:
        raise HTTPException(status_code=400, detail="Missing idToken")
    try:
        decoded = auth.verify_id_token(id_token)
        uid = decoded["uid"]
        email = decoded.get("email", "")
        name = request.get("name", "")
        business_name = request.get("business_name", "")
        business_type = request.get("business_type", "")
        plan = request.get("plan", "founder") or "founder"
        storage_path = get_user_storage_path(uid, plan)
        existing = {}
        user_file = find_user_json(uid)
        if user_file and user_file.exists():
            with open(user_file) as f:
                existing = json.load(f)
        user_data = {
            "user_id": uid,
            "name": name or existing.get("name", ""),
            "email": email or existing.get("email", ""),
            "business_name": business_name or existing.get("business_name", ""),
            "business_type": business_type or existing.get("business_type", ""),
            "plan": plan,
            "status": "active",
            "created_at": existing.get("created_at", str(int(time.time()))),
            "schedule": existing.get("schedule", {"open": "08:00", "close": "22:00"}),
            "cameras": existing.get("cameras", []),
            "fcm_tokens": existing.get("fcm_tokens", []),
            "storage_path": str(storage_path),
            "disk_mount": str(storage_path.parent)
        }
        storage_path.mkdir(parents=True, exist_ok=True)
        with open(storage_path / "user.json", "w") as f:
            json.dump(user_data, f, indent=2)
        compat_dir = STORAGE_ROOT / "users" / uid
        compat_dir.mkdir(parents=True, exist_ok=True)
        with open(compat_dir / "user.json", "w") as f:
            json.dump(user_data, f, indent=2)
        return {
            "success": True,
            "user_id": uid,
            "email": email,
            "name": user_data["name"],
            "business_name": business_name,
            "plan": plan
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
            "plan": data.get("plan", "founder"),
            "status": data.get("status", "active"),
            "schedule": data.get("schedule", {}),
            "cameras": data.get("cameras", [])
        }
    return {"user_id": user_id, "plan": "founder", "status": "active", "cameras": []}

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
    if "vigilance_prompt" in data:
        user_data["vigilance_prompt"] = data["vigilance_prompt"]
    if "vigilance_rules" in data:
        user_data["vigilance_rules"] = data["vigilance_rules"]
    with open(user_file, "w") as f:
        json.dump(user_data, f, indent=2)
    return {"success": True}

@app.get("/api/user/events")
async def get_user_events(user_id: str, date: str = None, filter: str = None, limit: int = 50):
    events = []
    now = int(time.time())
    start_of_today = now - (now % 86400)
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
            qa = ev.get("metadata", {}).get("qwen_analysis", "")[:200]
            ev["qwen"] = {"violation": ev.get("event_type") == "violation", "description": qa}
            ev["yolo"] = {"count": 1}
            if "metadata" in ev and "grid_b64" in ev["metadata"]:
                del ev["metadata"]["grid_b64"]
            events.append(ev)
    return {"events": events}

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
    return event

@app.get("/api/user/events/stats")
async def get_user_events_stats(user_id: str, date: str = None):
    now = int(time.time())
    start_of_today = now - (now % 86400)
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
        return {"cameras": ud.get("cameras", [])}
    return {"cameras": []}

@app.get("/api/cameras/{camera_id}/grid")
async def get_camera_grid(camera_id: str):
    last_cam = orchestrator.grid.get_last_camera_id()
    frame_bytes = orchestrator.grid.get_last_frame_bytes() if last_cam == camera_id else b""
    image_b64 = base64.b64encode(frame_bytes).decode() if frame_bytes else ""
    return {
        "success": True,
        "camera_id": camera_id,
        "image_b64": image_b64,
        "yolo": {"count": 0},
        "qwen": {"violation": False}
    }

# Ingesta de Frames (ESP32-CAM)
@app.post("/ingest/frame")
@app.post("/frames/ingest")
async def ingest_frame(request: Request, camera_id: str = Form(None),
                       user_id: str = Form("default"), image: UploadFile = File(None)):
    cid = camera_id or "unknown"
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

async def _process_ingest(request: Request, camera_id: str, user_id: str, image: UploadFile):
    try:
        client_ip = request.client.host if request.client else "unknown"
        user_id = resolve_user_id(camera_id, user_id, client_ip)
        img_bytes = await image.read()
        frame_size = len(img_bytes)
        logger.info(f"Frame: IP={client_ip} Cam={camera_id} User={user_id} Size={frame_size}B")
        # 1. YOLO detection
        yolo_count = 0
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                yolo_resp = await client.post(
                    "http://localhost:8002/detect",
                    files={"image": ("frame.jpg", img_bytes, "image/jpeg")},
                )
                if yolo_resp.status_code == 200:
                    yolo_count = yolo_resp.json().get("count", 0)
        except Exception as e:
            logger.warning(f"YOLO unavailable: {e}")
        logger.info(f"YOLO: {yolo_count} objects")
        # 2. Agregar al grid SOLO si YOLO detecto objetos
        grid_result = {"frame_count": 0, "grid_full": False, "ready_for_analysis": False}
        if yolo_count > 0:
            v_prompt, v_rules = "", ""
            uf = find_user_json(user_id)
            if uf and uf.exists():
                with open(uf) as f:
                    ud = json.load(f)
                v_prompt = ud.get("vigilance_prompt", "")
                v_rules_raw = ud.get("vigilance_rules", [])
                v_rules = v_rules_raw if isinstance(v_rules_raw, str) else "\n".join(v_rules_raw) if v_rules_raw else ""
            grid_result = orchestrator.add_frame(
                img_bytes, camera_id, user_id,
                yolo_count=yolo_count,
                vigilance_prompt=v_prompt,
                vigilance_rules=v_rules
            )
            logger.info(f"Grid: {grid_result['frame_count']}/16 | YOLO:{yolo_count}")
        else:
            logger.info(f"Skip: no YOLO objects")
        # 3. Registrar camara si es nueva
        if user_id and user_id != "default":
            uf = find_user_json(user_id)
            if uf and uf.exists():
                with open(uf) as f:
                    ud = json.load(f)
                existing_ids = [c.get("camera_id") for c in ud.get("cameras", [])]
                if camera_id not in existing_ids:
                    ud.setdefault("cameras", []).append({
                        "camera_id": camera_id,
                        "name": camera_id,
                        "zone": "",
                        "active": True,
                        "first_seen": int(time.time()),
                        "last_frame": int(time.time())
                    })
                    uf.parent.mkdir(parents=True, exist_ok=True)
                    with open(uf, "w") as f:
                        json.dump(ud, f, indent=2)
                    compat_dir = STORAGE_ROOT / "users" / user_id
                    compat_dir.mkdir(parents=True, exist_ok=True)
                    with open(compat_dir / "user.json", "w") as f:
                        json.dump(ud, f, indent=2)
        return {
            "success": True,
            "camera_id": camera_id,
            "user_id": user_id,
            "client_ip": client_ip,
            "frame_size": frame_size,
            "timestamp": datetime.now().isoformat(),
            "yolo": {"count": yolo_count},
            **grid_result
        }
    except Exception as e:
        logger.error(f"Ingest error: {e}")
        return {"success": False, "error": str(e)}

# Anuncio de Dispositivo
@app.post("/devices/announce")
async def device_announce(request: dict = None):
    if not request:
        request = {}
    camera_id = request.get("camera_id", "unknown")
    user_id = request.get("user_id", "")
    if not camera_id:
        raise HTTPException(status_code=400, detail="camera_id required")
    logger.info(f"Device announce: Cam={camera_id} User={user_id}")
    image_b64 = ""
    frame_bytes = orchestrator.grid.get_last_frame_bytes()
    last_cam = orchestrator.grid.get_last_camera_id()
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

# ── Chat de Eva — Flujo "Camara Primero, Reglas Despues" ─────────
@app.post("/config/chat")
async def config_chat(request: dict):
    user_id = request.get("user_id", "")
    message = request.get("message", "hola")
    session_id = request.get("session_id", "")
    cam_id = request.get("cam_id")
    include_frame = request.get("include_frame", False)

    # Inicializar sesion si es nuevo saludo
    if message == "__greet__" or user_id not in eva_sessions:
        eva_sessions[user_id] = {
            "msgs": [],
            "phase": "saludo",
            "zone": "",
            "rules": [],
            "concerns": [],
            "business_info": [],
            "camera_id": cam_id or "",
            "camera_connected": False,
            "camera_image_b64": "",
            "image_description": "",
            "configured": False,
            "schedule": {"open": "08:00", "close": "22:00"}
        }

    session = eva_sessions[user_id]

    # Cargar contexto del negocio desde user.json
    owner = "amigo"
    biz = "tu negocio"
    biz_type = "negocio"
    schedule = session["schedule"]

    uf = find_user_json(user_id)
    if uf and uf.exists():
        with open(uf) as f:
            ud = json.load(f)
        owner = ud.get("name", owner)
        biz = ud.get("business_name", biz)
        biz_type = ud.get("business_type", biz_type)
        schedule = ud.get("schedule", schedule)
        session["schedule"] = schedule

    first_name = owner.split()[0] if owner else "amigo"
    current_phase = session["phase"]
    zone = session.get("zone", "")

    # Helper: Construir system prompt
    def build_system(phase_instruction: str, extra: str = "") -> str:
        rules_display = session["rules"] if session["rules"] else ["ninguna aun"]
        concerns_display = session["concerns"] if session["concerns"] else ["aun no mencionadas"]
        return (
            f"Eres Eva, asistente de seguridad de OjoIA para negocios en Republica Dominicana.\n\n"
            f"CONTEXTO DEL NEGOCIO:\n"
            f"- Dueno: {owner} (llamalo por su nombre: {first_name})\n"
            f"- Negocio: {biz} ({biz_type})\n"
            f"- Horario laboral: {schedule.get('open', '08:00')} a {schedule.get('close', '22:00')}\n"
            f"- Zona actual de la camara: {zone if zone else 'no definida aun'}\n\n"
            f"ESTADO DE LA CONFIGURACION:\n"
            f"- Fase actual: {current_phase}\n"
            f"- Preocupaciones del usuario: {', '.join(concerns_display)}\n"
            f"- Reglas confirmadas ({len(session['rules'])}/3): {', '.join(rules_display)}\n"
            f"- Info del negocio compartida: {' '.join(session['business_info']) if session['business_info'] else 'aun no'}\n"
            f"- Descripcion de la imagen: {session['image_description'] if session['image_description'] else 'sin imagen aun'}\n\n"
            f"INSTRUCCION ACTUAL PARA ESTA RESPUESTA:\n"
            f"{phase_instruction}\n"
            f"{extra}\n\n"
            f"REGLAS ESTRICTAS PARA EVA:\n"
            f"1. TONO: Cercano, directo, dominicano. Maximo 3-4 lineas por respuesta.\n"
            f"2. NO usar terminos tecnicos: YOLO, grid, scanner, tokens, API, JSON, etc.\n"
            f"3. CONECTAR LA CAMARA ANTES DEL TURNO 5: No discutir reglas sin ver la imagen real.\n"
            f"4. REGLAS BASADAS EN IMAGEN: Proponer reglas que puedas OBSERVAR visualmente.\n"
            f"5. MAXIMO 3 REGLAS: Claras, especificas, observables.\n"
            f"6. FORMATO DE PROPUESTA: 'Te propongo esta regla: [regla]. Te parece bien o quieres que la ajuste?'\n"
            f"7. CONFIRMACION: Solo guardar regla cuando el usuario diga 'si', 'perfecto', 'dale', etc.\n"
            f"8. AJUSTES DE POSICION: Ser especifica: 'inclina 10 grados hacia abajo', 'mueve 20cm a la derecha'.\n"
            f"9. BLOQUE TECNICO: El usuario NUNCA debe ver [CAMERA_CONFIG]. Solo tu lo generas al confirmar.\n\n"
            f"RESPUESTAS TIPICAS DOMINICANAS:\n"
            f"- 'Excelente!' 'Perfecto, mi hermano' 'Dale, eso esta claro'\n"
            f"- 'Me explico?' 'Que te parece?' 'Avisame cuando estes listo'"
        )

    # Helper: Construir mensajes para Qwen
    def build_messages(sys_prompt: str) -> list:
        msgs = [{"role": "system", "content": sys_prompt}]
        for m in session["msgs"][-12:]:
            role = m.get("role", "user")
            content = m.get("content", "")
            if role == "assistant" and "[CAMERA_CONFIG]" in str(content):
                continue
            if isinstance(content, list):
                has_img = any(isinstance(c, dict) and c.get("type") == "image_url" for c in content)
                if has_img:
                    text_parts = [c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"]
                    content = " ".join(text_parts)
            if not content.strip():
                continue
            msgs.append({"role": role, "content": content})
        return msgs

    # Helper: Llamar a Qwen
    async def call_qwen(messages: list, max_tokens: int = 300) -> str:
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    "http://localhost:8004/v1/chat/completions",
                    json={"model": "qwen", "messages": messages, "max_tokens": max_tokens}
                )
                if resp.status_code == 200:
                    return resp.json().get("choices", [{}])[0].get("message", {}).get("content", "")
        except Exception as e:
            logger.error(f"Qwen call error: {e}")
        return "Disculpa, tuve un problema de conexion. Puedes repetir?"

    # Helper: Describir imagen con Qwen
    async def describe_image(img_b64: str) -> str:
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                resp = await client.post(
                    "http://localhost:8004/v1/chat/completions",
                    json={
                        "model": "qwen",
                        "messages": [{
                            "role": "user",
                            "content": [
                                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"}},
                                {"type": "text", "text": "Describe esta imagen en detalle (max 60 palabras). Que zona es? Que objetos ves (mostrador, caja registradora, estantes, puerta, personas, columna, etc)? La imagen se ve nitida y enfocada? Hay reflejos, zonas oscuras, o obstaculos que bloqueen la vision? El angulo de la camara es bueno o esta torcido/cortado? Hay algo que sugieras ajustar?"}
                            ]
                        }],
                        "max_tokens": 150
                    }
                )
                if resp.status_code == 200:
                    return resp.json().get("choices", [{}])[0].get("message", {}).get("content", "")
        except:
            pass
        return "Veo la zona donde instalaste la camara."

    def is_poor_quality(desc: str) -> bool:
        keywords = ["reflejo", "reflejos", "angulo", "angle", "malo", "bajo", "cortado",
                   "oscuro", "borroso", "no se ve", "dificil", "problem", "obscure"]
        return any(kw in desc.lower() for kw in keywords)

    # ── FASE 1: SALUDO ──
    if current_phase == "saludo" and not session["msgs"]:
        session["msgs"] = []
        answer = f"Hola {first_name} 👋 Vi que tienes un {biz_type} en tu registro. Vamos a instalar una cámara nueva. ¿Dónde quieres ponerla?"
        session["phase"] = "zona"
        session["msgs"].append({"role": "user", "content": "Hola"})
        session["msgs"].append({"role": "assistant", "content": answer})
        eva_sessions[user_id] = session
        return {"success": True, "response": answer, "ready_to_confirm": False, "camera_image_b64": ""}

    # ── FASE 2: ZONA ──
    if current_phase == "zona":
        session["zone"] = message.strip()
        zone = session["zone"]
        session["phase"] = "hardware"
        answer = "Enciende la cámara. La luz del frente va a encender y apagarse. Avísame cuando lo veas."
        session["msgs"].append({"role": "user", "content": message})
        session["msgs"].append({"role": "assistant", "content": answer})
        eva_sessions[user_id] = session
        return {"success": True, "response": answer, "ready_to_confirm": False, "camera_image_b64": "", "zone": zone}

    # ── FASE 3: HARDWARE ──
    if current_phase == "hardware":
        lower = message.lower()
        hw_keywords = ["ya", "listo", "enciend", "prendi", "prendida", "azul", "parpade",
                      "la veo", "ya la veo", "luz azul", "parpadeando", "luz", "led", "ya esta"]
        zone = session.get("zone", "")
        if any(w in lower for w in hw_keywords):
            session["phase"] = "wifi"
            session["msgs"].append({"role": "user", "content": message})
            answer = f"Bien, el ojo está encendido pero aún no puedo conectarme a él para vigilar tu negocio. Necesito que le des internet. Ve al WiFi de tu celular o dispositivo. Vas a ver una red OjoIA-XXXX. Conéctate ahí, abrirá una página de conexión. En ella elige el WiFi de tu {biz_type} y ponle la clave."
            session["msgs"].append({"role": "assistant", "content": answer})
            eva_sessions[user_id] = session
            return {"success": True, "response": answer, "ready_to_confirm": False, "camera_image_b64": "", "zone": zone}
        session["msgs"].append({"role": "user", "content": message})
        answer = "Enciende la cámara. La luz del frente va a encender y apagarse. Avísame cuando lo veas."
        session["msgs"].append({"role": "assistant", "content": answer})
        eva_sessions[user_id] = session
        return {"success": True, "response": answer, "ready_to_confirm": False, "camera_image_b64": "", "zone": zone}

    # ── FASE 4: WiFi ──
    if current_phase == "wifi":
        lower = message.lower()
        zone = session.get("zone", "")
        if any(w in lower for w in ["ya", "listo", "conect", "ok", "bien", "hecho", "ya lo hice"]):
            session["phase"] = "esperando_imagen"
            session["camera_connected"] = True
            session["msgs"].append({"role": "user", "content": message})
            frame_bytes = orchestrator.grid.get_last_frame_bytes()
            if frame_bytes:
                try:
                    resized = resize_image(frame_bytes, max_size=640)
                    frame_b64 = base64.b64encode(resized).decode()
                except:
                    frame_b64 = base64.b64encode(frame_bytes).decode()
                session["camera_image_b64"] = frame_b64
                session["image_shown"] = True
                image_description = await describe_image(frame_b64)
                session["image_description"] = image_description
                is_poor = is_poor_quality(image_description)
                if is_poor:
                    session["phase"] = "ajustando_camara"
                else:
                    session["phase"] = "mostrando_imagen"
                sys_prompt = build_system(
                    f"✅ ¡Ya la vi {first_name}! Analiza la imagen y ayuda al usuario a configurar la mejor ubicacion para su camara.\n\n"
                    f"DESCRIPCION DE LA IMAGEN: '{image_description}'\n\n"
                    f"El usuario quiere la camara en: '{zone}'\n"
                    f"Tipo de negocio: {biz_type}\n\n"
                    f"Instrucciones:\n"
                    f"1. Saluda con entusiasmo y describe lo que ves en la imagen de forma descriptiva y conversacional\n"
                    f"2. Da tu OPINION sobre la ubicacion: ¿Es el lugar correcto? ¿Esta bien enfocada y nitida?\n"
                    f"3. Si ves algun problema (reflejos, angulo malo, zona cortada, obstaculos), SUGIERE un ajuste especifico con tono amigable (no imponer)\n"
                    f"4. Si todo esta bien, confirmalo con entusiasmo\n"
                    f"5. Tono: cercano, dominicano, ayudante — siempre sugerir y preguntar, nunca imponer\n"
                    f"6. Max 3-4 lineas. Estilo: 'Veo un mostrador con dos secciones. La cámara está bien posicionada para vigilar la caja. ¿Es este el área que quieres cubrir?' o 'Veo la caja registradora. Creo que si mueves la cámara un poco hacia atrás capturarías mejor toda la zona. ¿Qué te parece?'"
                )
                msgs = build_messages(sys_prompt)
                if frame_b64 and msgs and len(msgs) > 1:
                    last_msg = msgs[-1]
                    if last_msg.get("role") == "user":
                        last_msg["content"] = [
                            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{frame_b64}"}},
                            {"type": "text", "text": "Analiza esta imagen y dime si la ubicacion de la cámara es correcta, si está bien enfocada, y si necesita algún ajuste. La zona es: " + zone}
                        ]
                answer = await call_qwen(msgs, max_tokens=200)
                session["msgs"].append({"role": "assistant", "content": answer})
                eva_sessions[user_id] = session
                return {"success": True, "response": answer, "ready_to_confirm": False, "camera_image_b64": frame_b64, "zone": zone, "rules": session["rules"]}
            answer = f"Aun no me llega la imagen de tu camara, {first_name}. Verifica que este encendida y conectada al WiFi. Dime 'revisa' en unos segundos."
            session["msgs"].append({"role": "assistant", "content": answer})
            eva_sessions[user_id] = session
            return {"success": True, "response": answer, "ready_to_confirm": False, "camera_image_b64": "", "zone": zone}
        session["msgs"].append({"role": "user", "content": message})
        answer = f"Bien, el ojo está encendido pero aún no puedo conectarme a él para vigilar tu negocio. Necesito que le des internet. Ve al WiFi de tu celular. Busca la red OjoIA-XXXX."
        session["msgs"].append({"role": "assistant", "content": answer})
        eva_sessions[user_id] = session
        return {"success": True, "response": answer, "ready_to_confirm": False, "camera_image_b64": "", "zone": zone}

    # ── FASE 5: ESPERANDO IMAGEN ──
    if current_phase == "esperando_imagen":
        session["msgs"].append({"role": "user", "content": message})
        zone = session.get("zone", "")
        frame_bytes = orchestrator.grid.get_last_frame_bytes()
        has_frame = bool(frame_bytes)
        if not has_frame:
            answer = f"Aun no me llega la imagen de tu camara, {first_name}. Verifica que este encendida y conectada al WiFi. Dime 'revisa' en unos segundos."
            session["msgs"].append({"role": "assistant", "content": answer})
            eva_sessions[user_id] = session
            return {"success": True, "response": answer, "ready_to_confirm": False, "camera_image_b64": "", "zone": zone, "rules": session["rules"]}
        try:
            resized = resize_image(frame_bytes, max_size=640)
            frame_b64 = base64.b64encode(resized).decode()
        except:
            frame_b64 = base64.b64encode(frame_bytes).decode()
        session["camera_image_b64"] = frame_b64
        session["image_shown"] = True
        session["camera_connected"] = True
        image_description = await describe_image(frame_b64)
        session["image_description"] = image_description
        is_poor = is_poor_quality(image_description)
        if is_poor:
            session["phase"] = "ajustando_camara"
        else:
            session["phase"] = "mostrando_imagen"
        sys_prompt = build_system(
            f"✅ ¡Ya la vi {first_name}! Analiza la imagen y ayuda al usuario a configurar la mejor ubicacion para su camara.\n\n"
            f"DESCRIPCION DE LA IMAGEN: '{image_description}'\n\n"
            f"El usuario quiere la camara en: '{zone}'\n"
            f"Tipo de negocio: {biz_type}\n\n"
            f"Instrucciones:\n"
            f"1. Saluda con entusiasmo y describe lo que ves en la imagen de forma descriptiva y conversacional\n"
            f"2. Da tu OPINION sobre la ubicacion: ¿Es el lugar correcto? ¿Esta bien enfocada y nitida?\n"
            f"3. Si ves algun problema (reflejos, angulo malo, zona cortada, obstaculos), SUGIERE un ajuste especifico con tono amigable (no imponer)\n"
            f"4. Si todo esta bien, confirmalo con entusiasmo\n"
            f"5. Tono: cercano, dominicano, ayudante — siempre sugerir y preguntar, nunca imponer\n"
            f"6. Max 3-4 lineas. Estilo: 'Veo un mostrador con dos secciones. La cámara está bien posicionada para vigilar la caja. ¿Es este el área que quieres cubrir?' o 'Veo la caja registradora. Creo que si mueves la cámara un poco hacia atrás capturarías mejor toda la zona. ¿Qué te parece?'"
        )
        msgs = build_messages(sys_prompt)
        if frame_b64 and msgs and len(msgs) > 1:
            last_msg = msgs[-1]
            if last_msg.get("role") == "user":
                last_msg["content"] = [
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{frame_b64}"}},
                    {"type": "text", "text": "Analiza esta imagen y dime si la ubicacion de la cámara es correcta, si está bien enfocada, y si necesitas algún ajuste. La zona es: " + zone}
                ]
        answer = await call_qwen(msgs, max_tokens=200)
        session["msgs"].append({"role": "assistant", "content": answer})
        eva_sessions[user_id] = session
        return {
            "success": True,
            "response": answer,
            "ready_to_confirm": False,
            "camera_saved": False,
            "camera_image_b64": frame_b64,
            "zone": zone,
            "rules": session["rules"]
        }

    # ── FASE 5b: AJUSTANDO CAMARA (usuario dice que ya ajusto) ──
    if current_phase == "ajustando_camara":
        lower = message.lower()
        zone = session.get("zone", "")
        # Volver a verificar imagen
        frame_bytes = orchestrator.grid.get_last_frame_bytes()
        has_frame_now = bool(frame_bytes)
        if has_frame_now:
            try:
                resized = resize_image(frame_bytes, max_size=512)
                frame_b64 = base64.b64encode(resized).decode()
            except:
                frame_b64 = base64.b64encode(frame_bytes).decode()
            session["camera_image_b64"] = frame_b64
            session["camera_connected"] = True
            image_description = await describe_image(frame_b64)
            session["image_description"] = image_description
            is_poor = is_poor_quality(image_description)
            session["msgs"].append({"role": "user", "content": message})
            if is_poor:
                # Sigue con problemas
                sys_prompt = build_system(
                    f"{first_name} dice que ajusto pero la imagen aun tiene problemas: '{image_description}'. "
                    f"Da instrucciones MAS ESPECIFICAS. Se paciente pero firme."
                )
                msgs = build_messages(sys_prompt)
                if frame_b64 and msgs and len(msgs) > 1:
                    last_msg = msgs[-1]
                    if last_msg.get("role") == "user":
                        last_msg["content"] = [
                            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{frame_b64}"}},
                            {"type": "text", "text": last_msg.get("content", "")}
                        ]
                answer = await call_qwen(msgs)
                session["msgs"].append({"role": "assistant", "content": answer})
                eva_sessions[user_id] = session
                return {"success": True, "response": answer, "ready_to_confirm": False, "camera_saved": False, "camera_image_b64": frame_b64, "zone": zone, "rules": session["rules"]}
            else:
                # Imagen mejoró
                session["phase"] = "mostrando_imagen"
                sys_prompt = build_system(
                    f"{first_name} ajusto la camara y ahora la imagen se ve mejor: '{image_description}'. "
                    f"Dile que se ve mucho mejor ahora. PREGUNTA: 'Este es el lugar correcto que quieres vigilar?'"
                )
                msgs = build_messages(sys_prompt)
                if frame_b64 and msgs and len(msgs) > 1:
                    last_msg = msgs[-1]
                    if last_msg.get("role") == "user":
                        last_msg["content"] = [
                            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{frame_b64}"}},
                            {"type": "text", "text": last_msg.get("content", "")}
                        ]
                answer = await call_qwen(msgs)
                session["msgs"].append({"role": "assistant", "content": answer})
                eva_sessions[user_id] = session
                return {"success": True, "response": answer, "ready_to_confirm": False, "camera_saved": False, "camera_image_b64": frame_b64, "zone": zone, "rules": session["rules"]}
        else:
            # Aun no hay frame despues de ajustar
            session["msgs"].append({"role": "user", "content": message})
            answer = await call_qwen(build_messages(build_system(
                f"{first_name} dice que ajusto la camara pero aun NO hay imagen. "
                f"Pidele que verifique que la camara siga encendida y conectada al WiFi. "
                f"Dile que diga 'revisa' cuando quiera intentar de nuevo."
            )))
            session["msgs"].append({"role": "assistant", "content": answer})
            eva_sessions[user_id] = session
            return {"success": True, "response": answer, "ready_to_confirm": False, "camera_saved": False, "camera_image_b64": "", "zone": zone, "rules": session["rules"]}

    # ── FASE 6: MOSTRANDO IMAGEN (confirmar zona → negociación de negocio) ──
    if current_phase == "mostrando_imagen":
        lower = message.lower()
        confirm_keywords = ["exacto", "eso es", "correcto", "si eso", "ahi", "ese", "claro", "si", "perfecto", "ahi esta", "esta bien", "la quiero", "ahi la quiero", "dejala", "asi esta bien"]
        zone = session.get("zone", "")
        if any(w in lower for w in confirm_keywords):
            session["phase"] = "zona_confirmada"
            session["msgs"].append({"role": "user", "content": message})
            schedule_open = schedule.get('open', '08:00')
            schedule_close = schedule.get('close', '22:00')
            sys_prompt = (
                f"El usuario {first_name} acaba de confirmar que la posición de la cámara es correcta. "
                f"NO ajustes la cámara. NO preguntes más sobre la posición. "
                f"Ahora inicia la NEGOCIACIÓN DE REGLAS como en el ejemplo de Juan:\n\n"
                f"1. Primero confirma el horario del negocio: 'Mira {first_name}, trabajo de {schedule_open} a {schedule_close}, ¿verdad? "
                f"Fuera de ese horario soy una guardia que no duerme — cualquier persona que vea te aviso de inmediato. ¿De acuerdo?'\n\n"
                f"2. Luego pregunta por su mayor preocupacion: 'Ahora dime — ¿qué es lo que más te preocupa en esta área cuando no estás?'\n\n"
                f"3. IMPORTANTE: NO hables más de la posición de la cámara. El usuario ya confirmó que está bien. "
                f"Enfócate ÚNICAMENTE en preguntar sobre sus preocupaciones para proponer reglas de vigilancia.\n\n"
                f"Tono: cercano, dominicano, ayudante."
            )
            msgs = build_messages(sys_prompt)
            if session["camera_image_b64"] and msgs and len(msgs) > 1:
                last_msg = msgs[-1]
                if last_msg.get("role") == "user":
                    last_msg["content"] = [
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{session['camera_image_b64']}"}},
                        {"type": "text", "text": f"El usuario confirmó la posición. NO ajustes la cámara. Inicia la negociación de reglas. Horario: {schedule_open}-{schedule_close}."}
                    ]
            answer = await call_qwen(msgs, max_tokens=250)
            session["msgs"].append({"role": "assistant", "content": answer})
            eva_sessions[user_id] = session
            return {"success": True, "response": answer, "ready_to_confirm": False, "camera_image_b64": session["camera_image_b64"], "zone": zone, "rules": session["rules"]}
        else:
            session["zone"] = message.strip()
            zone = session["zone"]
            session["msgs"].append({"role": "user", "content": message})
            answer = await call_qwen(build_messages(build_system(
                f"{first_name} corrigio la zona a: '{zone}'. "
                f"Confirma el cambio y vuelve a preguntar si ahora es el lugar correcto que quiere vigilar."
            )))
            session["msgs"].append({"role": "assistant", "content": answer})
            eva_sessions[user_id] = session
            return {"success": True, "response": answer, "ready_to_confirm": False, "camera_image_b64": session["camera_image_b64"], "zone": zone, "rules": session["rules"]}

    # ── FASE 7: ZONA CONFIRMADA (proponer reglas basadas en preocupación del usuario) ──
    if current_phase == "zona_confirmada":
        if "business_info" not in session:
            session["business_info"] = []
        session["business_info"].append(message)
        business_info = " ".join(session["business_info"])
        zone = session.get("zone", "")

        rules_count = len(session.get("rules", []))

        if rules_count >= 3:
            session["phase"] = "confirmando"
            sys_prompt = build_system(
                f"{first_name} ya tiene 3 reglas guardadas: {session['rules']}. "
                f"Ahora muestra un RESUMEN completo: zona={zone}, reglas={session['rules']}, "
                f"horario={schedule.get('open', '08:00')}-{schedule.get('close', '22:00')}. "
                f"Pide confirmacion final. Si confirma, genera [CAMERA_CONFIG]."
            )
            msgs = build_messages(sys_prompt)
            answer = await call_qwen(msgs, max_tokens=300)
            session["msgs"].append({"role": "assistant", "content": answer})
            eva_sessions[user_id] = session
            return {"success": True, "response": answer, "ready_to_confirm": False, "camera_saved": False, "camera_image_b64": "", "zone": zone, "rules": session["rules"]}

        session["phase"] = "proponiendo_regla"

        msg_lower = message.lower()
        rule_proposal = ""

        if any(w in msg_lower for w in ["factura", "facturación", "registro", "registrar"]):
            rule_proposal = f"Ese es un problema muy común en los {biz_type}s {first_name}. Te propongo esta regla: cada transacción debe registrarce en la caja registradora antes de entregar el producto. ¿Puedes asegurarte de que siempre se use la caja para cada venta?"
        elif any(w in msg_lower for w in ["dinero", "bolsillo", "robar", "hurtar", "meter"]):
            rule_proposal = f"Entiendo tu preocupación {first_name}. Te propongo esta regla: alerta si detecto a alguien metiendo las manos en los bolsillos o bolsas después de cobrar. No es infalible pero al menos los asusta saber que hay una cámara atenta. ¿Te parece bien?"
        elif any(w in msg_lower for w in ["despachar", "lado", "mostrador", "caja"]):
            rule_proposal = f"Para controlar mejor las transacciones, te propongo esta regla: todo el despacho se hace solo por el lado derecho del mostrador. Esto me permite ver cada transacción mejor. ¿Puedes adaptar el mostrador para que sea solo por ahí?"
        elif any(w in msg_lower for w in ["producto", "mercancía", "inventario"]):
            rule_proposal = f"Te propongo esta regla: alerta si detecto que un producto sale del mostrador sin que se registre en la caja. Así puedo identificar posibles hurtos. ¿Te parece bien?"
        else:
            rule_proposal = f"Basándome en lo que me dices sobre '{message}', te propongo esta regla: vigilancia enfocada en todas las transacciones del {zone} para detectar cualquier irregularidad. ¿Puedes darme más detalles sobre cómo quisieras que vigile esta área?"

        session["msgs"].append({"role": "user", "content": message})
        session["msgs"].append({"role": "assistant", "content": rule_proposal})
        eva_sessions[user_id] = session
        return {
            "success": True,
            "response": rule_proposal,
            "ready_to_confirm": False,
            "camera_saved": False,
            "camera_image_b64": "",
            "zone": zone,
            "rules": session["rules"]
        }

    # ── FASE 8: PROPONIENDO REGLA (guardar reglas, preguntar siguiente) ──
    if current_phase == "proponiendo_regla":
        lower = message.lower()
        zone = session.get("zone", [])

        repeat_keywords = ["ya te dije", "eso mismo", "lo mismo", "eso ya te lo dije", "te acabo de decir"]
        is_repeat = any(w in lower for w in repeat_keywords)

        if is_repeat:
            last_concern = session["business_info"][-1] if session.get("business_info") else ""
            sys_prompt = (
                f"{first_name} te está diciendo que ya le expresó su preocupación. NO preguntes de nuevo. "
                f"La preocupación que ya te dijo es: '{last_concern}'\n\n"
                f"PROPÓN UNA REGLA AHORA MISMO basada en esa preocupación + lo que ves en la imagen. "
                f"Formato: 'Te propongo esta regla: [regla]. ¿Puedes [acción]?'\n"
                f"Tono: cercano, dominicano. Reconoce que ya te lo dijo: 'Tienes razón, eso ya me lo explicaste. "
                f"Basándome en lo que me dices, te propongo esta regla: ...'"
            )
            msgs = build_messages(sys_prompt)
            if session["camera_image_b64"] and msgs and len(msgs) > 1:
                last_msg = msgs[-1]
                if last_msg.get("role") == "user":
                    last_msg["content"] = [
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{session['camera_image_b64']}"}},
                        {"type": "text", "text": f"El usuario repite su preocupación anterior: {last_concern}. PROPÓN UNA REGLA AHORA."}
                    ]
            answer = await call_qwen(msgs, max_tokens=250)
            session["msgs"].append({"role": "assistant", "content": answer})
            eva_sessions[user_id] = session
            return {"success": True, "response": answer, "ready_to_confirm": False, "camera_saved": False, "camera_image_b64": "", "zone": zone, "rules": session["rules"]}

        concern_keywords = ["me preocupa", "quiero que", "tambien", "ademas", "otra cosa", "y que", "que se", "que no"]
        strong_accept = ["perfecto", "exacto", "correcto", "dale", "vamos", "apruebo", "confirmo", "acepto", "suena bien", "me gusta", "si", "si puedo", "claro que si", "por supuesto", "buena idea", "hagamoslo", "va bien", "ok", "de acuerdo", "esta bien", "esta bien asi"]
        is_concern = any(w in lower for w in concern_keywords)
        is_accept = any(w in lower for w in strong_accept)
        is_reject = any(w in lower for w in ["no", "cambia", "modifica", "diferente", "mejor", "prefiero", "no me gusta"])

        if is_concern and not is_accept:
            if "concerns" not in session:
                session["concerns"] = []
            session["concerns"].append(message)
            session["msgs"].append({"role": "user", "content": message})
            rules_count = len(session.get("rules", []))
            sys_prompt = (
                f"{first_name} expresó una nueva preocupación: '{message}'. "
                f"Preocupaciones acumuladas: {session.get('concerns', [])}. "
                f"Imagen: '{session.get('image_description', '')}'. "
                f"Tipo de negocio: {biz_type}. "
                f"Reglas actuales: {rules_count}/3.\n\n"
                f"PROPÓN UNA REGLA ESPECÍFICA y OBSERVABLE. "
                f"Formato: 'Te propongo esta regla: [regla]. ¿Puedes [acción]?'\n"
                f"NO preguntes de nuevo qué le preocupa, YA te lo dijo. Propón la regla directamente."
            )
            msgs = build_messages(sys_prompt)
            if session["camera_image_b64"] and msgs and len(msgs) > 1:
                last_msg = msgs[-1]
                if last_msg.get("role") == "user":
                    last_msg["content"] = [
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{session['camera_image_b64']}"}},
                        {"type": "text", "text": f"Nueva preocupación: {message}. Propón UNA regla observable directamente."}
                    ]
            answer = await call_qwen(msgs, max_tokens=250)
            session["msgs"].append({"role": "assistant", "content": answer})
            eva_sessions[user_id] = session
            return {"success": True, "response": answer, "ready_to_confirm": False, "camera_saved": False, "camera_image_b64": "", "zone": zone, "rules": session["rules"]}

        elif is_reject:
            session["msgs"].append({"role": "user", "content": message})
            sys_prompt = build_system(
                f"{first_name} rechazó o quiere modificar la regla (mensaje: '{message}'). "
                f"Pidele que te diga cómo quiere que sea la regla y ajusta según su respuesta. "
                f"Se flexible y colaborativo."
            )
            msgs = build_messages(sys_prompt)
            answer = await call_qwen(msgs)
            session["msgs"].append({"role": "assistant", "content": answer})
            eva_sessions[user_id] = session
            return {"success": True, "response": answer, "ready_to_confirm": False, "camera_saved": False, "camera_image_b64": "", "zone": zone, "rules": session["rules"]}

            if "rules" not in session:
                session["rules"] = []
            rule_num = len(session["rules"]) + 1
            session["rules"].append(f"regla_{rule_num}")
            rules_count = len(session["rules"])
            session["msgs"].append({"role": "user", "content": message})
            if rules_count >= 3:
                session["phase"] = "confirmando"
                sys_prompt = build_system(
                    f"Regla {rule_num} guardada. Ya tienen {rules_count}/3 reglas. "
                    f"Ahora muestra un RESUMEN completo: zona={zone}, reglas={session['rules']}, horario={schedule}. "
                    f"Pide confirmacion final. Si confirma, genera [CAMERA_CONFIG] con los datos reales. "
                    f"Formato EXACTO: [CAMERA_CONFIG]{{...}}[/CAMERA_CONFIG]"
                )
            else:
                sys_prompt = build_system(
                    f"Regla {rule_num} guardada. Les faltan {3 - rules_count} reglas (maximo 3). "
                    f"Pregunta si quiere agregar otra regla. Pidele que te cuente que mas le preocupa."
                )
            msgs = build_messages(sys_prompt)
            answer = await call_qwen(msgs)
            session["msgs"].append({"role": "assistant", "content": answer})
            eva_sessions[user_id] = session
            return {"success": True, "response": answer, "ready_to_confirm": False, "camera_saved": False, "camera_image_b64": "", "zone": zone, "rules": session["rules"]}

        elif is_reject:
            session["msgs"].append({"role": "user", "content": message})
            sys_prompt = build_system(
                f"{first_name} rechazo o quiere modificar la regla (mensaje: '{message}'). "
                f"Pidele que te diga como quiere que sea la regla y ajusta segun su respuesta. "
                f"Se flexible y colaborativo."
            )
            msgs = build_messages(sys_prompt)
            answer = await call_qwen(msgs)
            session["msgs"].append({"role": "assistant", "content": answer})
            eva_sessions[user_id] = session
            return {"success": True, "response": answer, "ready_to_confirm": False, "camera_saved": False, "camera_image_b64": "", "zone": zone, "rules": session["rules"]}

        else:
            session["msgs"].append({"role": "user", "content": message})
            sys_prompt = build_system(
                f"{first_name} respondio algo ambiguo (mensaje: '{message}'). "
                f"Preguntale directamente: 'Te parece bien la regla que te propuse o quieres que la ajuste?'"
            )
            msgs = build_messages(sys_prompt)
            answer = await call_qwen(msgs)
            session["msgs"].append({"role": "assistant", "content": answer})
            eva_sessions[user_id] = session
            return {"success": True, "response": answer, "ready_to_confirm": False, "camera_saved": False, "camera_image_b64": "", "zone": zone, "rules": session["rules"]}

    # ── FASE 9: CONFIRMANDO ──
    if current_phase == "confirmando":
        lower = message.lower()
        final_confirm = ["si", "si", "acepto", "apruebo", "confirmo", "listo", "dale", "vamos", "esta bien", "esta bien", "de acuerdo", "perfecto", "aprobado"]
        zone = session.get("zone", "")
        session["msgs"].append({"role": "user", "content": message})
        if any(w in lower for w in final_confirm):
            sys_prompt = build_system(
                f"{first_name} confirmo la configuracion. "
                f"Genera el bloque [CAMERA_CONFIG] con los datos REALES: "
                f"camera_id={session['camera_id'] or 'AUTO'}, zone={zone}, rules={session['rules']}, "
                f"schedule={schedule}, business_info={' '.join(session['business_info'])}. "
                f"El scanner_question debe ser una pregunta visual especifica en INGLES para Moondream. "
                f"El system_prompt debe ser contexto completo en INGLES para Qwen. "
                f"Formato EXACTO: [CAMERA_CONFIG]{{...}}[/CAMERA_CONFIG]"
            )
        else:
            sys_prompt = build_system(
                f"{first_name} respondio: '{message}'. "
                f"Muestra de nuevo el resumen completo y pide confirmacion explicita. "
                f"Si confirma, genera [CAMERA_CONFIG]."
            )
        msgs = build_messages(sys_prompt)
        answer = await call_qwen(msgs, max_tokens=600)
        session["msgs"].append({"role": "assistant", "content": answer})

        # Parsear y guardar [CAMERA_CONFIG]
        camera_saved = False
        if "[CAMERA_CONFIG]" in answer:
            match = re.search(r'\[CAMERA_CONFIG\](.*?)\[/CAMERA_CONFIG\]', answer, re.DOTALL)
            if match:
                try:
                    camera_config = json.loads(match.group(1).strip())
                    camera_id = camera_config.get("camera_id", "unknown")
                    if camera_id == "AUTO":
                        camera_id = session.get("camera_id") or f"cam_{int(time.time())}"
                    camera_config["camera_id"] = camera_id
                    storage_path = get_user_storage_path(user_id, "founder")
                    camera_dir = storage_path / "cameras" / camera_id
                    camera_dir.mkdir(parents=True, exist_ok=True)
                    with open(camera_dir / "camera.json", "w") as f:
                        json.dump(camera_config, f, indent=2)
                    uf = find_user_json(user_id)
                    if uf and uf.exists():
                        with open(uf) as f:
                            user_data = json.load(f)
                    else:
                        user_data = {"user_id": user_id, "cameras": []}
                    user_data.setdefault("cameras", [])
                    existing = [c for c in user_data["cameras"] if c.get("camera_id") != camera_id]
                    existing.append({
                        "camera_id": camera_id,
                        "name": camera_config.get("name", camera_id),
                        "zone": camera_config.get("zone", "")
                    })
                    user_data["cameras"] = existing
                    uf = find_user_json(user_id)
                    storage_path = get_user_storage_path(user_id, "founder")
                    if uf and uf.exists():
                        uf.parent.mkdir(parents=True, exist_ok=True)
                        with open(uf, "w") as f:
                            json.dump(user_data, f, indent=2)
                    compat_dir = STORAGE_ROOT / "users" / user_id
                    compat_dir.mkdir(parents=True, exist_ok=True)
                    with open(compat_dir / "user.json", "w") as f:
                        json.dump(user_data, f, indent=2)
                    camera_saved = True
                    session["configured"] = True
                    session["camera_id"] = camera_id
                    session["phase"] = "completado"
                    logger.info(f"Camera saved: {camera_id} for user {user_id}")
                except json.JSONDecodeError as e:
                    logger.error(f"Failed to parse CAMERA_CONFIG JSON: {e}")
                except Exception as e:
                    logger.error(f"Failed to save camera: {e}")
            answer = re.sub(r'\[CAMERA_CONFIG\].*?\[/CAMERA_CONFIG\]', '', answer, flags=re.DOTALL).strip()
            if not answer or len(answer) < 20:
                answer = f"Listo {first_name}! Tu camara esta configurada y vigilando."
        eva_sessions[user_id] = session
        return {
            "success": True,
            "response": answer,
            "ready_to_confirm": camera_saved,
            "camera_saved": camera_saved,
            "zone": zone,
            "rules": session["rules"]
        }

    # ── FASE DEFAULT ──
    session["msgs"].append({"role": "user", "content": message})
    zone = session.get("zone", "")
    sys_prompt = build_system(
        f"{first_name} dijo: '{message}'. "
        f"Continua la configuracion de forma natural segun el contexto actual (fase: {current_phase}). "
        f"Si ya tienes suficiente informacion, muestra un resumen y pide confirmacion final generando [CAMERA_CONFIG]."
    )
    msgs = build_messages(sys_prompt)
    answer = await call_qwen(msgs)
    session["msgs"].append({"role": "assistant", "content": answer})
    eva_sessions[user_id] = session
    return {"success": True, "response": answer, "ready_to_confirm": False, "camera_image_b64": "", "zone": zone, "rules": session["rules"]}

# ── Config Confirm (fallback) ──
@app.post("/config/confirm")
async def config_confirm(request: dict):
    user_id = request.get("user_id", "")
    if user_id in eva_sessions:
        session = eva_sessions[user_id]
        session["configured"] = True
        session["phase"] = "completado"
        eva_sessions[user_id] = session
    return {"success": True}

# ── Estado de Camara para Eva ──
@app.get("/config/camera_status")
async def config_camera_status(user_id: str):
    session = eva_sessions.get(user_id, {})
    frame_bytes = orchestrator.grid.get_last_frame_bytes()
    image_b64 = ""
    if frame_bytes:
        try:
            resized = resize_image(frame_bytes, max_size=512)
            image_b64 = base64.b64encode(resized).decode()
        except:
            image_b64 = base64.b64encode(frame_bytes).decode()
    return {
        "camera_connected": session.get("camera_connected", False),
        "camera_id": session.get("camera_id", ""),
        "has_frame": bool(frame_bytes),
        "image_b64": image_b64,
        "zone": session.get("zone", ""),
        "rules_count": len(session.get("rules", [])),
        "configured": session.get("configured", False),
        "phase": session.get("phase", "unknown")
    }

# ── Reset de Sesion Eva ──
@app.post("/config/reset")
async def config_reset(request: dict):
    user_id = request.get("user_id", "")
    eva_sessions.pop(user_id, None)
    return {"success": True, "message": "Sesion reiniciada"}

# ── Session state ──
@app.get("/config/session")
async def config_session(user_id: str):
    s = eva_sessions.get(user_id, {})
    return {
        "active": bool(s),
        "camera_id": s.get("camera_id", ""),
        "camera_image_b64": s.get("camera_image_b64", ""),
        "zone": s.get("zone", ""),
        "rules": s.get("rules", []),
        "phase": s.get("phase", ""),
        "msgs_count": len(s.get("msgs", []))
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

# ── Admin Endpoints ──
@app.get("/admin/cameras")
async def admin_list_cameras():
    cfg = get_disk_config()
    now = time.time()
    cameras = []
    last_seen = {}
    for f in orchestrator.grid.frames:
        lid = f.get("camera_id", "")
        ts = f.get("timestamp", 0)
        if lid and (lid not in last_seen or ts > last_seen[lid]):
            last_seen[lid] = ts
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
                cid = cam.get("camera_id", "")
                ts = last_seen.get(cid, 0)
                online = (now - ts) < 300 if ts else False
                cameras.append({
                    "camera_id": cid,
                    "name": cam.get("name", cid),
                    "zone": cam.get("zone", ""),
                    "user_id": uid.name,
                    "business_name": udata.get("business_name", ""),
                    "status": "online" if online else "offline",
                    "active": cam.get("active", True),
                    "last_seen": datetime.fromtimestamp(ts).isoformat() if ts else None
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
        "active_cameras": len(orchestrator.grid.get_grid_info()["camera_ids"]),
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
    for disk in cfg.get("disks", []):
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
                        users.append(json.load(f))
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
        return {
            "user_id": user_id,
            "name": user_data.get("name", "-"),
            "email": user_data.get("email", "-"),
            "business_name": user_data.get("business_name", ""),
            "business_type": user_data.get("business_type", ""),
            "plan": user_data.get("plan", "founder"),
            "status": user_data.get("status", "active"),
            "created_at": user_data.get("created_at", "-"),
            "cameras": user_data.get("cameras", [])
        }
    return {
        "user_id": user_id, "name": "Test User", "email": "test@example.com",
        "business_name": "Test Business", "plan": "founder", "status": "active",
        "cameras": []
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
                    result.append({
                        "user_id": uid.name,
                        "name": ud.get("name", ""),
                        "plan": ud.get("plan", "founder"),
                        "business_name": ud.get("business_name", "")
                    })
                except:
                    pass
    return {"users": result}

@app.get("/admin/storage/{user_id}")
async def admin_get_user_storage(user_id: str):
    cfg = get_disk_config()
    plan = "founder"
    user_file = find_user_json(user_id)
    if user_file and user_file.exists():
        with open(user_file) as f:
            ud = json.load(f)
        plan = ud.get("plan", "founder")
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
        "plan": plan
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
    frame_count = orchestrator.grid.get_frame_count()
    return {
        "queue_length": frame_count,
        "processing": 0, "done": 0, "error": 0,
        "queue_size_mb": 0, "oldest_item": None,
        "last_processed": None, "worker_running": True,
        "pending_frames": [], "grid_frames": frame_count,
        "grid_ready": frame_count >= 16
    }

@app.post("/admin/queue/clear")
async def admin_clear_queue():
    return {"success": True}

@app.get("/admin/eva-config")
async def admin_eva_config():
    try:
        with open(EVA_CONFIG_FILE) as f:
            cfg = json.load(f)
    except:
        cfg = {"prompt": ""}
    return {"prompt": cfg.get("prompt", ""), "docs": cfg.get("docs", [])}

@app.post("/admin/system/prompts")
async def admin_save_prompt(request: dict):
    try:
        with open(EVA_CONFIG_FILE) as f:
            cfg = json.load(f)
    except:
        cfg = {}
    cfg["prompt"] = request.get("prompt", cfg.get("prompt", ""))
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
    grid_info = orchestrator.grid.get_grid_info()
    return {"grid_status": grid_info, "timestamp": datetime.now().isoformat()}

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

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8005)
