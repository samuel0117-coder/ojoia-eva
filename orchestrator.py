#!/usr/bin/env python3
"""Orquestador simple para Qwen Vision"""
import asyncio
import base64
import httpx
import json
import os
import time
import logging
import datetime
import re
from typing import Optional, List, Dict, Any
from gateway_resize import resize_image, image_to_base64, create_grid_image
from eva.camera_builder import normalize_camera_vigilance_config, build_witness_prompt
from face_pipeline import identify_from_frame, extract_face_from_frame
import threading

logger = logging.getLogger(__name__)

STORAGE_ROOT = "/home/sam/storage"
DISKS_CONFIG_FILE = f"{STORAGE_ROOT}/disks_config.json"


def get_disk_config() -> dict:
    """Lee la configuración de discos desde disco."""
    try:
        with open(DISKS_CONFIG_FILE) as f:
            cfg = json.load(f)
        if "disks" in cfg:
            return cfg
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return {"disks": [{"mount": STORAGE_ROOT, "user_folder": "users"}], "plans": {}}


def get_user_storage_path(user_id: str, plan: str = "founder") -> str:
    """
    Resuelve la ruta de almacenamiento del usuario consultando la config de discos.
    Usa el disco con más espacio libre por defecto.
    """
    cfg = get_disk_config()
    disks = cfg.get("disks", [])
    plans = cfg.get("plans", {})

    target_plan = plans.get(plan, plans.get("founder", {}))
    priority_disk = target_plan.get("priority_disk", "")

    selected = None
    if priority_disk:
        for d in disks:
            if d.get("mount") == priority_disk:
                selected = d
                break

    if not selected:
        max_free = -1
        for d in disks:
            free = d.get("free_gb", 0) or 0
            if free > max_free:
                max_free = free
                selected = d

    if not selected and disks:
        selected = disks[0]

    if not selected:
        selected = {"mount": STORAGE_ROOT, "user_folder": "users"}

    return f"{selected['mount']}/{selected['user_folder'].strip('/')}/{user_id}"

def get_camera_config(user_id: str, camera_id: str) -> dict:
    """Lee la configuración de una cámara desde camera.json"""
    camera_file = f"{STORAGE_ROOT}/users/{user_id}/cameras/{camera_id}/camera.json"
    if os.path.exists(camera_file):
        try:
            with open(camera_file) as f:
                return normalize_camera_vigilance_config(json.load(f))
        except:
            pass
    return {"vigilance_prompt": "", "rules": [], "zone": "principal"}

def save_event_to_disk(user_id: str, camera_id: str, event_type: str,
                        frame_bytes: bytes, description: str, metadata: dict = None):
    """Guarda evento en disco en la carpeta de la cámara correspondiente"""
    ts = int(time.time())

    # Resolver ruta según config de discos
    try:
        user_file = f"{STORAGE_ROOT}/users/{user_id}/user.json"
        plan = "free"
        if os.path.exists(user_file):
            with open(user_file) as f:
                ud = json.load(f)
            plan = ud.get("plan", "free")
        storage_path = get_user_storage_path(user_id, plan)
    except Exception:
        storage_path = f"{STORAGE_ROOT}/users/{user_id}"

    # Crear ruta con subcarpeta de cámara
    event_dir = f"{storage_path}/cameras/{camera_id}/events"
    os.makedirs(event_dir, exist_ok=True)

    event = {
        "event_id": f"evt_{ts}_{camera_id}",
        "user_id": user_id,
        "camera_id": camera_id,
        "event_type": event_type,
        "description": description,
        "timestamp": ts,
        "metadata": metadata or {}
    }

    # Guardar metadata JSON
    with open(f"{event_dir}/{event['event_id']}.json", "w") as f:
        json.dump(event, f, indent=2)

    # Guardar frame como imagen
    with open(f"{event_dir}/{event['event_id']}.jpg", "wb") as f:
        f.write(frame_bytes)

    return event["event_id"]


def _write_event_frames(event_dir: str, event_id: str, frame_bytes: bytes, metadata: dict):
    """Guarda frames individuales del grid para el carrusel tipo video."""
    package_dir = os.path.join(event_dir, event_id)
    frames_dir = os.path.join(package_dir, "frames")
    os.makedirs(frames_dir, exist_ok=True)

    frame_items = []
    grid_frames = metadata.get("grid_frames") if isinstance(metadata, dict) else []
    if isinstance(grid_frames, list):
        for i, frame in enumerate(grid_frames[:64]):
            if not frame:
                continue
            if isinstance(frame, bytes):
                data = frame
            elif isinstance(frame, dict):
                data = frame.get("image_bytes") or frame.get("bytes")
            else:
                data = None
            if not data:
                continue
            frame_name = f"frame_{i:03d}.jpg"
            with open(os.path.join(frames_dir, frame_name), "wb") as f:
                f.write(data)
            frame_items.append({
                "index": i,
                "file": frame_name,
                "size": len(data),
                "datetime": frame.get("datetime") if isinstance(frame, dict) else "",
            })

    grid_b64 = metadata.get("grid_b64") if isinstance(metadata, dict) else ""
    if grid_b64:
        try:
            with open(os.path.join(package_dir, "grid.jpg"), "wb") as f:
                f.write(base64.b64decode(grid_b64))
        except Exception:
            pass

    with open(os.path.join(package_dir, f"{event_id}.jpg"), "wb") as f:
        f.write(frame_bytes or b"")

    return frame_items


def save_event_to_disk_v2(user_id, camera_id, event_type,
                           frame_bytes, summary, qwen_json, metadata=None):
    """Guarda evento v2 con JSON rico (diario del vigilante)."""
    metadata = metadata or {}
    ts = int(time.time())
    dt_str = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(ts))
    try:
        user_file = f"{STORAGE_ROOT}/users/{user_id}/user.json"
        plan = "free"
        if os.path.exists(user_file):
            with open(user_file) as f:
                plan = json.load(f).get("plan", "free")
        storage_path = get_user_storage_path(user_id, plan)
    except Exception:
        storage_path = f"{STORAGE_ROOT}/users/{user_id}"
    event_dir = f"{storage_path}/cameras/{camera_id}/events"
    os.makedirs(event_dir, exist_ok=True)
    event_id = f"evt_{ts}_{camera_id}"
    event = {
        "event_id": event_id,
        "user_id": user_id,
        "camera_id": camera_id,
        "event_type": event_type,
        "timestamp": ts,
        "datetime": dt_str,
        "description": summary,
        "summary": summary,
        "qwen_json": qwen_json if isinstance(qwen_json, dict) else {},
        "metadata": metadata or {},
    }
    frame_items = _write_event_frames(event_dir, event_id, frame_bytes, metadata)
    if frame_items:
        metadata["frames"] = frame_items
        metadata["frames_count"] = len(frame_items)
        metadata["clip_type"] = "event_package"
    event_for_json = {
        "event_id": event_id,
        "user_id": user_id,
        "camera_id": camera_id,
        "event_type": event_type,
        "timestamp": ts,
        "datetime": dt_str,
        "description": summary,
        "summary": summary,
        "qwen_json": qwen_json if isinstance(qwen_json, dict) else {},
        "metadata": {k: v for k, v in (metadata or {}).items() if k not in ("grid_frames", "grid_b64")},
    }
    with open(f"{event_dir}/{event_id}.json", "w") as f:
        json.dump(event_for_json, f, indent=2, ensure_ascii=False)
    with open(f"{event_dir}/{event_id}.jpg", "wb") as f:
        f.write(frame_bytes)
    return event_id


def update_camera_metrics(user_id: str, camera_id: str, event_type: str = "normal"):
    """Actualizar métricas de cámara después de cada evento."""
    try:
        cam_file = f"{STORAGE_ROOT}/users/{user_id}/cameras/{camera_id}/camera.json"
        if not os.path.exists(cam_file):
            return
        with open(cam_file) as f:
            cam = json.load(f)
        if "metrics" not in cam:
            cam["metrics"] = {"total_events": 0, "total_alerts": 0, "total_false_positives": 0, "rules": {}, "needs_review": False}
        cam["metrics"]["total_events"] = cam["metrics"].get("total_events", 0) + 1
        if event_type in ("alert", "violation", "vigilance_alert"):
            cam["metrics"]["total_alerts"] = cam["metrics"].get("total_alerts", 0) + 1
        with open(cam_file, "w") as f:
            json.dump(cam, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logging.error(f"Error actualizando métricas: {e}")


def register_false_alarm(user_id: str, camera_id: str):
    """Registrar falsa alarma — incrementar contador y marcar si necesita revisión."""
    try:
        cam_file = f"{STORAGE_ROOT}/users/{user_id}/cameras/{camera_id}/camera.json"
        if not os.path.exists(cam_file):
            return
        with open(cam_file) as f:
            cam = json.load(f)
        if "metrics" not in cam:
            cam["metrics"] = {"total_events": 0, "total_alerts": 0, "total_false_positives": 0, "rules": {}, "needs_review": False}
        cam["metrics"]["total_false_positives"] = cam["metrics"].get("total_false_positives", 0) + 1
        # Si tiene 3+ falsas alarmas, marcar para revisión
        if cam["metrics"]["total_false_positives"] >= 3:
            cam["metrics"]["needs_review"] = True
        # También incrementar contador por regla (la última regla que disparó)
        rules = cam.get("rules", [])
        if rules:
            rule_idx = len(rules) - 1  # Asumir que la última regla disparó
            rules_metrics = cam["metrics"].get("rules", {})
            key = f"rule_{rule_idx}"
            if key not in rules_metrics:
                rules_metrics[key] = {"false_positives": 0}
            rules_metrics[key]["false_positives"] = rules_metrics[key].get("false_positives", 0) + 1
            cam["metrics"]["rules"] = rules_metrics
        with open(cam_file, "w") as f:
            json.dump(cam, f, indent=2, ensure_ascii=False)
        logging.info(f"⚠️ Falsa alarma registrada para {camera_id}. Total: {cam['metrics']['total_false_positives']}")
    except Exception as e:
        logging.error(f"Error registrando falsa alarma: {e}")



def _send_night_fcm(cam_cfg, user_id, camera_id, frame_bytes, event_id, persons):
    try:
        import requests as _req
        from google.oauth2 import service_account
        import google.auth.transport.requests as _greq
        uf = f"{STORAGE_ROOT}/users/{user_id}/user.json"
        tokens = []
        if os.path.exists(uf):
            with open(uf) as f:
                tokens = json.load(f).get("fcm_tokens", [])
        if not tokens:
            return
        creds = service_account.Credentials.from_service_account_file(
            "/home/sam/ai_system/firebase-key.json",
            scopes=["https://www.googleapis.com/auth/firebase.messaging"],
        )
        creds.refresh(_greq.Request())
        now_str = time.strftime("%H:%M", time.localtime())
        zone = cam_cfg.get("zone", camera_id)
        title = f"\ud83d\udea9 ALERTA NOCTURNA — {zone}"
        body = f"{now_str}: {persons} persona(s) detectada(s)"
        business_name = ""
        try:
            with open(uf) as _uf:
                business_name = json.load(_uf).get("business_name", "")
        except Exception:
            pass
        link = f"https://ojoia.com.do/#events?event={event_id}" if event_id else "https://ojoia.com.do/#events"
        b64 = image_to_base64(frame_bytes) if frame_bytes else None
        for tok in tok in tokens:
            payload = {
                "message": {
                    "token": tok,
                    "notification": {"title": title, "body": body},
                    "android": {
                        "priority": "high",
                        "notification": {
                            "channel_id": "security_critical",
                            "notification_priority": "PRIORITY_MAX",
                            "sound": "critical_alert",
                            "visibility": "public",
                        },
                    },
                    "apns": {
                        "headers": {"apns-priority": "10"},
                        "payload": {"aps": {"sound": "critical_alert.caf", "badge": 1, "category": "SECURITY_ALERT"}},
                    },
                    "data": {
                        "event_id": event_id or "",
                        "camera_id": camera_id,
                        "priority": "critical",
                        "type": "night_alert",
                        "business_name": business_name,
                    },
                }
            }
            if b64:
                payload["message"]["data"]["image"] = b64
            _resp = _req.post(
                "https://fcm.googleapis.com/v1/projects/ojoia-67216/messages:send",
                headers={"Authorization": f"Bearer {creds.token}", "Content-Type": "application/json"},
                json=payload,
                timeout=10,
            )
            if _resp.status_code != 200:
                logging.warning(f"Night FCM error {_resp.status_code}: {_resp.text[:200]}")
    except Exception as e:
        logging.error(f"_send_night_fcm error: {e}")


async def send_fcm_notification(title: str, body: str, token: str = None,
                                    user_id: str = None, image_b64: str = None, link: str = "https://ojoia.com.do/#events"):
    """Enviar notificación push via Firebase Cloud Messaging (API HTTP v1 con OAuth2)."""
    import logging as _log
    try:
        from google.oauth2 import service_account
        import google.auth.transport.requests
        import requests as _req
        import json as _json
        
        # Buscar tokens FCM del usuario en user.json
        tokens = []
        if token:
            tokens.append(token)
        elif user_id:
            _uf = f"{STORAGE_ROOT}/users/{user_id}/user.json"
            if os.path.exists(_uf):
                with open(_uf) as _f:
                    _ud = _json.load(_f)
                tokens = _ud.get("fcm_tokens", [])
        
        if not tokens:
            _log.info(f"FCM: Sin tokens para user={user_id}")
            return False
        
        # Obtener access token OAuth2 con scope de FCM
        _creds = service_account.Credentials.from_service_account_file(
            "/home/sam/ai_system/firebase-key.json",
            scopes=["https://www.googleapis.com/auth/firebase.messaging"]
        )
        _creds.refresh(google.auth.transport.requests.Request())
        _access_token = _creds.token
        
        _headers = {
            "Authorization": f"Bearer {_access_token}",
            "Content-Type": "application/json"
        }
        
        sent = 0
        for tok in tokens:
            try:
                _payload = {
                    "message": {
                        "token": tok,
                        "notification": {"title": title, "body": body},
                            "data": {
                            "type": "violation",
                            "url": link,
                            "event_id": link.split('alert=')[-1].split('&')[0] if 'alert=' in link else "",
                            "camera_id": link.split('camera=')[-1].split('&')[0] if 'camera=' in link else "",
                            "title": title,
                            "body": body,
                            "tag": "violation"
                        },

                        "webpush": {
                            "notification": {
                                "title": title,
                                "body": body,
                                "icon": "/img/icon-192.png",
                                "badge": "/img/icon-192.png",
                                "require_interaction": True,
                                "tag": "violation"
                            },
                            "fcm_options": {"link": link}
                        }
                    }
                }
                _log.info(f"FCM payload: title='{title}' body='{body[:80]}' link={link}")
                _resp = _req.post(
                    "https://fcm.googleapis.com/v1/projects/ojoia-67216/messages:send",
                    json=_payload, headers=_headers, timeout=10
                )
                if _resp.status_code == 200:
                    _log.info(f"FCM enviado: {_resp.json().get('name', 'ok')}")
                    sent += 1
                else:
                    _log.warning(f"FCM error {_resp.status_code}: {_resp.text[:200]}")
            except Exception as _e:
                _log.warning(f"FCM token error: {_e}")
        
        _log.info(f"FCM: {sent}/{len(tokens)} enviadas")
        return sent > 0

    except Exception as e:
        _log.error(f"FCM error: {e}")
        return False



async def _process_night_grid(self, frames, cam_cfg, user_id, camera_id):
    roi = _get_night_roi(cam_cfg)
    tracker = NightTracker(persist_frames=int(cam_cfg.get("persist_frames", 2)))
    frame0 = next((f for f in frames if f.get("image_bytes")), {}) or {}
    img = frame0.get("image_bytes", b"")
    detections = _detect_persons_night(img, roi)
    confirmed = tracker.update(detections) if detections else []
    if not confirmed:
        return {
            "frames_processed": len(frames),
            "violation": False,
            "night_mode": True,
            "persons_detected": 0,
            "schedule": cam_cfg.get("schedule", {}),
        }
    evt_id = _save_night_event(user_id, camera_id, img, confirmed, cam_cfg)
    update_camera_metrics(user_id, camera_id, event_type="alert")
    return {
        "frames_processed": len(frames),
        "violation": True,
        "event_id": evt_id,
        "night_mode": True,
        "persons_detected": len(confirmed),
        "schedule": cam_cfg.get("schedule", {}),
    }


def _detect_persons_night(img_bytes, roi):
    persons = []
    try:
        import cv2
        import numpy as np
        np_arr = np.frombuffer(img_bytes, np.uint8)
        frame = cv2.imdecode(np_arr, cv2.IMREAD_COLOR)
        if frame is None or frame.size == 0:
            return []
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        gray = cv2.GaussianBlur(gray, (5, 5), 0)
        diff = cv2.absdiff(gray, gray)
        _, motion = cv2.threshold(diff, 25, 255, cv2.THRESH_BINARY)
        if cv2.countNonZero(motion) < 200:
            return []
        try:
            from ultralytics import YOLO
            yolo = YOLO("/home/sam/ai_system/models/yolo/yolov8n.pt")
            for r in yolo.predict(source=frame, conf=0.25, iou=0.4, classes=[0], verbose=False):
                for b in r.boxes:
                    x1, y1, x2, y2 = map(int, b.xyxy[0].tolist())
                    area = max(0, x2 - x1) * max(0, y2 - y1)
                    if area < 500:
                        continue
                    bbox = [x1, y1, x2, y2]
                    if not _person_in_roi(bbox, roi):
                        continue
                    persons.append({"bbox": bbox, "area": area, "conf": float(b.conf[0])})
                if persons:
                    break
        except Exception as e:
            logging.error(f"Night YOLO: {e}")
    except Exception as e:
        logging.error(f"Night detect: {e}")
    return persons


def _save_night_event(user_id, camera_id, img, confirmed, cam_cfg):
    ts = int(time.time())
    evt_dir = f"/home/sam/storage/users/{user_id}/cameras/{camera_id}/events"
    os.makedirs(evt_dir, exist_ok=True)
    evt_id = f"evt_night_{ts}_{camera_id}"
    zone = cam_cfg.get("zone", camera_id)
    desc = f"Persona detectada en modo nocturno (zona {zone}, {len(confirmed)} confirmado(s))"
    evt = {
        "event_id": evt_id,
        "user_id": user_id,
        "camera_id": camera_id,
        "event_type": "night_alert",
        "description": desc,
        "timestamp": ts,
        "metadata": {
            "mode": "night",
            "persons": len(confirmed),
            "roi": cam_cfg.get("night_roi", []),
            "schedule": cam_cfg.get("schedule", {}),
        },
    }
    with open(f"{evt_dir}/{evt_id}.json", "w") as f:
        json.dump(evt, f, indent=2)
    with open(f"{evt_dir}/{evt_id}.jpg", "wb") as f:
        f.write(img or b"")
    return evt_id

class FrameGrid:
    """16-frame grid for Qwen analysis"""
    
    def __init__(self, max_frames: int = 16):
        self.max_frames = max_frames
        self.frames: List[Dict[str, Any]] = []
        self.last_frame_bytes: bytes = b''
        self.last_camera_id: str = ""
        self.last_yolo_count: int = 0
        self.last_yolo_detections: list = []
        self.lock = threading.Lock()
        self.last_analysis_ts = 0
        self.analysis_callback = None
    
    def add_frame(self, image_bytes: bytes, camera_id: str, user_id: str, yolo_count: int = 0,
                  yolo_classes: list = None, yolo_detections: list = None, mode: str = "normal",
                  vigilance_prompt: str = None, vigilance_rules: str = None) -> bool:
        """Add frame to grid. Returns True when grid is full and ready for analysis.
        
        Args:
            vigilance_prompt: Prompt for Qwen analysis (now stored per frame)
            vigilance_rules: Rules for attention detection
        """
        yolo_classes = yolo_classes or []
        yolo_detections = yolo_detections or []
        if yolo_count <= 0:
            with self.lock:
                self.last_frame_bytes = image_bytes
                self.last_camera_id = camera_id
            return False

        with self.lock:
            self.frames.append({
                "image_bytes": image_bytes,
                "camera_id": camera_id,
                "user_id": user_id,
                "yolo_count": yolo_count,
                "yolo_classes": yolo_classes,
                "yolo_detections": yolo_detections,
                "mode": mode,
                "timestamp": time.time(),
                "vigilance_prompt": vigilance_prompt,
                "vigilance_rules": vigilance_rules
            })
            is_full = len(self.frames) >= 16  # Changed from 8 to 16 for 4x4 grid
            self.last_frame_bytes = image_bytes
            self.last_camera_id = camera_id
            self.last_yolo_count = yolo_count
            self.last_yolo_detections = yolo_detections
        return is_full

    def get_and_reset(self) -> List[Dict[str, Any]]:
        """Get current frames and reset grid."""
        with self.lock:
            frames = self.frames.copy()
            self.frames = []
            return frames
    
    def get_frame_count(self) -> int:
        with self.lock:
            return len(self.frames)
    
    def get_last_frame_bytes(self) -> bytes:
        """Get the most recently received frame"""
        with self.lock:
            return self.last_frame_bytes
    
    def get_last_camera_id(self) -> str:
        """Get the camera_id of the most recently received frame"""
        with self.lock:
            return self.last_camera_id
    
    def get_last_yolo_count(self) -> int:
        """Get the yolo_count of the most recently received frame"""
        with self.lock:
            return self.last_yolo_count
    
    def get_last_yolo_detections(self) -> list:
        """Get the yolo_detections of the most recently received frame"""
        with self.lock:
            return self.last_yolo_detections.copy() if self.last_yolo_detections else []
    
    def get_grid_image(self) -> bytes:
        """Get a 4x4 grid image of all frames"""
        with self.lock:
            image_bytes = [f["image_bytes"] for f in self.frames]
            return create_grid_image(image_bytes)
    
    def get_grid_info(self) -> Dict[str, Any]:
        """Get current grid state without modifying it"""
        with self.lock:
            return {
                "frame_count": len(self.frames),
                "camera_ids": list(set(f["camera_id"] for f in self.frames))
            }



# ─── Night Mode Configuration ───
NIGHT_YOLO_CONF = 0.25
NIGHT_YOLO_IOU = 0.4
NIGHT_YOLO_CLASSES = [0]  # Only person class (COCO class 0)
NIGHT_MIN_PERSON_AREA = 500  # Minimum bbox area in pixels
NIGHT_PERSIST_FRAMES = 2  # Require detection in N consecutive frames
NIGHT_MOTION_THRESH = 25  # Motion detection threshold

# Default night ROI (normalized coordinates 0-1) - center area, exclude edges
DEFAULT_NIGHT_ROI = [
    [0.15, 0.15], [0.85, 0.15], [0.85, 0.85], [0.15, 0.85]
]


def _is_night_mode(cam_cfg: dict) -> bool:
    """Check if camera is in night mode based on schedule."""
    try:
        schedule = cam_cfg.get("schedule", {})
        open_time = schedule.get("open", "07:00")
        close_time = schedule.get("close", "19:00")
        now = datetime.datetime.now()
        now_min = now.hour * 60 + now.minute
        open_h, open_m = map(int, open_time.split(":"))
        close_h, close_m = map(int, close_time.split(":"))
        open_min = open_h * 60 + open_m
        close_min = close_h * 60 + close_m
        now_val = now.hour * 60 + now.minute
        if open_min < close_min:
            return now_val < open_min or now_val > close_min
        else:
            return now_val >= open_min or now_val < close_min
    except Exception:
        return False


def _get_night_roi(cam_cfg: dict) -> list:
    """Get night ROI from camera config or use default."""
    return cam_cfg.get("night_roi", DEFAULT_NIGHT_ROI)


def _bbox_in_roi(bbox: list, roi: list) -> bool:
    """Check if bbox center is inside ROI polygon."""
    if not bbox or len(bbox) != 4:
        return False
    cx = (bbox[0] + bbox[2]) / 2
    cy = (bbox[1] + bbox[3]) / 2
    # Point-in-polygon test (ray casting)
    x, y = cx, cy
    inside = False
    n = len(roi)
    for i in range(n):
        j = (i + 1) % n
        xi, yi = roi[i]
        xj, yj = roi[j]
        if ((yi > y) != (yj > y)) and (x < (xj - xi) * (cy - yi) / (yj - yi) + xi):
            inside = not inside
    return inside


def _person_in_roi(person_bbox: list, roi: list) -> bool:
    """Check if person bbox center is in ROI."""
    return _bbox_in_roi(person_bbox, roi)


class NightTracker:
    """Track person detections across frames for persistence."""
    
    def __init__(self, persist_frames: int = 2):
        self.persist_frames = persist_frames
        self.tracks: Dict[int, dict] = {}  # track_id -> {bbox, frames_seen, last_seen}
        self.next_id = 0
        self.last_frame_time = 0
    
    def update(self, persons: list, frame_time: float) -> list:
        """Update tracks with new detections. Return confirmed persons."""
        confirmed = []
        matched_ids = set()
        
        for person in persons:
            bbox = person.get("bbox", [])
            if not bbox:
                continue
            cx = (bbox[0] + bbox[2]) / 2
            cy = (bbox[1] + bbox[3]) / 2
            
            # Find matching track
            best_id = None
            best_dist = float('inf')
            for tid, track in self.tracks.items():
                tx, ty = track["center"]
                dist = ((cx - tx) ** 2 + (cy - ty) ** 2) ** 0.5
                if dist < 100 and dist < best_dist:  # 100px max movement
                    best_dist = dist
                    best_id = tid
            
            if best_id is not None:
                # Update existing track
                track = self.tracks[best_id]
                track["bbox"] = person.get("bbox", [])
                track["center"] = (cx, cy)
                track["frames_seen"] += 1
                track["last_seen"] = time.time()
                track["conf"] = person.get("confidence", 0)
                matched_ids.add(best_id)
                if track["frames_seen"] >= 2:  # Confirmed after 2 frames
                    confirmed.append({**person, "track_id": best_id, "frames_seen": track["frames_seen"]})
            else:
                # New track
                tid = self.next_id
                self.next_id += 1
                self.tracks[tid] = {
                    "bbox": person.get("bbox", []),
                    "center": (cx, cy),
                    "frames_seen": 1,
                    "last_seen": time.time(),
                    "conf": person.get("confidence", 0)
                }
                matched_ids.add(tid)
        
        # Remove old tracks
        current_time = time.time()
        to_remove = [tid for tid, t in self.tracks.items() if current_time - t["last_seen"] > 5]
        for tid in to_remove:
            del self.tracks[tid]
        
        return confirmed


def _parse_qwen_json(content) -> dict:
    import json as json_module
    if isinstance(content, dict):
        return content
    if not isinstance(content, str):
        return {}
    text = content.strip()
    if not text:
        return {}
    # NUEVO: Si la respuesta es texto plano (narrativa), envolverla directamente
    if not text.startswith("{") and not text.startswith("```"):
        return {"resumen": text, "scene": text[:200], "persons": []}
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text).strip()
    text = re.sub(r"\{[\s]*\.\.\.[\s]*\}", "{}", text)
    text = re.sub(r"\[[\s]*\.\.\.[\s]*\]", "[]", text)
    try:
        parsed = json_module.loads(text)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        pass
    match = re.search(r"\{[\s\S]*\}", text)
    if match:
        try:
            parsed = json_module.loads(match.group())
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            pass
    for key in ("resumen", "summary", "description"):
        key_match = re.search(rf'"{key}"\s*:\s*"((?:\\.|[^"\\])*)"', text)
        if key_match:
            try:
                return {key: json_module.loads('"' + key_match.group(1) + '"')}
            except Exception:
                return {key: key_match.group(1)}
    # Fallback: extraer cualquier texto como descripción
    return {"resumen": text[:500], "scene": text[:200], "persons": []}


def _convert_qwen_vision_response(raw: dict) -> dict:
    if not isinstance(raw, dict):
        return raw
    # Si ya tiene resumen, devolverlo tal cual
    if "resumen" in raw or "scene" in raw:
        return raw
    # Formato legacy con cajero/clientes
    if "cajero" in raw or "clientes" in raw:
        persons = []
        cajero = raw.get("cajero", {})
        if isinstance(cajero, dict) and cajero.get("presente"):
            p = {"ubicacion": cajero.get("ubicacion", "")}
            ropa = cajero.get("ropa", "")
            if ropa:
                p["clothing"] = [ropa] if isinstance(ropa, str) else ropa
            accion = cajero.get("accion", "")
            if accion:
                p["acciones"] = [accion] if isinstance(accion, str) else accion
            persons.append(p)
        clientes = raw.get("clientes", {})
        if isinstance(clientes, dict):
            cant = int(clientes.get("cantidad", 0) or 0)
            for _ in range(min(cant, 5)):
                p = {"ubicacion": clientes.get("ubicacion", "")}
                ropa = clientes.get("ropa", "")
                if ropa:
                    p["clothing"] = [ropa] if isinstance(ropa, str) else ropa
                accion = clientes.get("accion", "")
                if accion:
                    p["acciones"] = [accion] if isinstance(accion, str) else accion
                persons.append(p)
        objects = []
        caja = raw.get("caja", {})
        if isinstance(caja, dict):
            if caja.get("estado"):
                objects.append(f"caja {caja['estado']}")
            if caja.get("dinero"):
                objects.append("dinero visible")
            if caja.get("datafono"):
                objects.append("datáfono")
        scene = raw.get("resumen", "")
        result = dict(raw)
        result["persons"] = persons
        result["scene"] = scene
        result["objects"] = objects
        result.setdefault("counts", {})
        result.setdefault("attention_hits", [])
        result.setdefault("transaccion", {})
        return result
    # Si es texto plano sin formato, crear estructura básica
    return {"resumen": raw.get("resumen", ""), "scene": raw.get("scene", ""), "persons": [], "objects": []}


def _normalize_rich_qwen_json(qwen_json: dict, mode: str) -> dict:
    qwen_json = dict(qwen_json or {})
    qwen_json["mode"] = mode
    qwen_json["importancia"] = qwen_json.get("importance") or qwen_json.get("importancia") or "normal"
    qwen_json["importance"] = qwen_json["importancia"]
    if qwen_json["importance"] not in ("normal", "baja", "media", "alta", "critica"):
        qwen_json["importance"] = "normal"
        qwen_json["importancia"] = "normal"
    if not isinstance(qwen_json.get("details"), dict):
        qwen_json["details"] = {}
    details = qwen_json["details"]
    details.setdefault("persons_visible", 0)
    details.setdefault("persons_description", "")
    details.setdefault("clothing_visible", [])
    details.setdefault("actions_visible", [])
    details.setdefault("objects_visible", [])
    details.setdefault("scene_context", "")
    details.setdefault("camera_condition", "visible")
    for key in ("clothing_visible", "actions_visible", "objects_visible", "search_tags", "evidence"):
        if not isinstance(qwen_json.get(key), list):
            qwen_json[key] = []
    if not isinstance(qwen_json.get("anomalias"), list):
        qwen_json["anomalias"] = []
    if not isinstance(qwen_json.get("attention_hits"), list):
        qwen_json["attention_hits"] = []
    if not isinstance(qwen_json.get("counts"), dict):
        qwen_json["counts"] = {}
    if not qwen_json.get("search_tags"):
        tags = []
        for value in details.values():
            if isinstance(value, str) and value:
                tags.append(value)
        qwen_json["search_tags"] = tags
    return qwen_json


def _build_summary_from_rich_qwen(qwen_json: dict, zone: str, attention_detected: bool) -> str:
    details = qwen_json.get("details", {}) if isinstance(qwen_json.get("details"), dict) else {}
    persons = details.get("persons_description") or ""
    actions = details.get("actions_visible") or []
    objects = details.get("objects_visible") or []
    scene = details.get("scene_context") or ""
    vision = qwen_json.get("vision", {}) if isinstance(qwen_json.get("vision"), dict) else {}
    v_cliente = vision.get("cliente") or qwen_json.get("cliente")
    v_empleado = vision.get("empleado") or qwen_json.get("empleado")
    v_resumen = vision.get("summary", "") or qwen_json.get("summary", "")
    v_persons = vision.get("persons", []) or qwen_json.get("persons", [])
    if not isinstance(v_persons, list):
        v_persons = []
    v_scene = vision.get("scene", "") or qwen_json.get("scene", "")
    v_objects = vision.get("objects", []) or qwen_json.get("objects", [])
    if not isinstance(v_objects, list):
        v_objects = []
    parts = []
    if v_cliente and isinstance(v_cliente, dict) and v_cliente.get("presente"):
        desc = "Cliente"
        if v_cliente.get("descripcion"):
            desc += f" — {v_cliente['descripcion']}"
            if v_cliente.get("accion") and v_cliente["accion"] not in v_cliente.get("descripcion", ""):
                desc += f", {v_cliente['accion']}"
        elif v_cliente.get("accion"):
            desc += f" — {v_cliente['accion']}"
        if v_cliente.get("pago"):
            desc += f" | Pago: {v_cliente['pago']}"
            if v_cliente.get("cantidad_billetes"):
                desc += f" ({v_cliente['cantidad_billetes']} billete(s))"
            if v_cliente.get("denominacion"):
                desc += f" de {v_cliente['denominacion']}"
        if v_cliente.get("platos"):
            desc += f" | Pedido: {v_cliente['platos']} plato(s)"
        parts.append(desc)
    if v_empleado and isinstance(v_empleado, dict) and v_empleado.get("presente"):
        desc = "Empleado (cajero)"
        if v_empleado.get("descripcion"):
            desc += f" — {v_empleado['descripcion']}"
            if v_empleado.get("accion") and v_empleado["accion"] not in v_empleado.get("descripcion", ""):
                desc += f", {v_empleado['accion']}"
        elif v_empleado.get("accion"):
            desc += f" — {v_empleado['accion']}"
        actions_list = []
        if v_empleado.get("cajon_abierto"):
            actions_list.append("abrió cajón")
        if v_empleado.get("entrego_cambio"):
            actions_list.append("entregó cambio")
        if v_empleado.get("uso_datafono"):
            actions_list.append("usó datáfono")
        if v_empleado.get("entrego_platos"):
            actions_list.append(f"entregó {v_empleado['entrego_platos']} plato(s)")
        if actions_list:
            desc += f" | {', '.join(actions_list)}"
        parts.append(desc)
    if v_persons and not v_cliente and not v_empleado:
        for p in v_persons:
            role = p.get("role", "persona")
            clothing = p.get("clothing", "")
            action = p.get("action", p.get("acciones", ""))
            interaction = p.get("interaction", "")
            desc = role.capitalize()
            if clothing:
                desc += f" ({clothing})" if isinstance(clothing, str) else f" ({', '.join(str(c) for c in clothing)})"
            if action and isinstance(action, list):
                desc += " — " + ", ".join(str(a) for a in action)
            elif action and isinstance(action, str):
                desc += f" — {action.strip()}"
            if interaction and isinstance(interaction, str) and interaction.strip():
                desc += f", {interaction.strip()}"
            parts.append(desc)
    elif persons:
        parts.append(str(persons))
    if v_scene and len(v_scene) > 30:
        parts.append(v_scene)
    elif v_resumen and v_resumen != v_scene:
        parts.append(v_resumen)
    v_objects_filtered = [str(o) for o in v_objects if str(o).lower() not in ("silla", "mesa", "pared", "techo", "lámpara", "cable")]
    if v_objects_filtered:
        parts.append("Objetos: " + ", ".join(v_objects_filtered))
    elif isinstance(objects, list) and objects:
        parts.append("objetos visibles: " + ", ".join(str(o) for o in objects[:5]))
    if parts:
        prefix = "Observación relevante" if attention_detected else "Actividad normal"
        return f"{prefix} en {zone}: " + ". ".join(parts) + "."
    if v_resumen:
        return v_resumen
    if v_scene:
        return v_scene
    if persons:
        return str(persons)
    return "Actividad normal" if not attention_detected else "Observación relevante"


def _description_detail_parts(qj: dict, evt: dict) -> list:
    detail_parts = []
    qj_details = qj.get("details") if isinstance(qj.get("details"), dict) else {}
    vision = qj.get("vision", {}) if isinstance(qj.get("vision"), dict) else {}
    # Buscar en vision o raíz (nuevo formato)
    v_cliente = vision.get("cliente") or qj.get("cliente")
    v_empleado = vision.get("empleado") or qj.get("empleado")
    v_persons = vision.get("persons", []) or qj.get("persons", [])
    if not isinstance(v_persons, list):
        v_persons = []
    v_scene = vision.get("scene", "") or qj.get("scene", "")
    v_resumen = vision.get("summary", "") or qj.get("summary", "")
    v_objects = vision.get("objects", []) or qj.get("objects", [])
    if not isinstance(v_objects, list):
        v_objects = []
    technical_words = {"presencia detectada", "persona detectada", "en el grid", "Qwen no distinguió", "YOLO"}
    def _clean_detail_text(text):
        text = str(text or "")
        for bad in technical_words:
            text = text.replace(bad, "")
        text = re.sub(r"\s+", " ", text).strip(" ;.")
        return text if text and not any(bad in text.lower() for bad in ["grid", "yolo", "qwen"]) else ""
    # Formato nuevo: cliente + empleado + resumen
    if v_cliente and isinstance(v_cliente, dict) and v_cliente.get("presente"):
        desc = "Cliente"
        if v_cliente.get("descripcion"):
            desc += f" — {v_cliente['descripcion']}"
            if v_cliente.get("accion") and v_cliente["accion"] not in v_cliente.get("descripcion", ""):
                desc += f", {v_cliente['accion']}"
        elif v_cliente.get("accion"):
            desc += f" — {v_cliente['accion']}"
        if v_cliente.get("pago"):
            desc += f" | Pago: {v_cliente['pago']}"
            if v_cliente.get("cantidad_billetes"):
                desc += f" ({v_cliente['cantidad_billetes']} billete(s))"
            if v_cliente.get("denominacion"):
                desc += f" de {v_cliente['denominacion']}"
        if v_cliente.get("platos"):
            desc += f" | Pedido: {v_cliente['platos']} plato(s)"
        detail_parts.append(desc)
    if v_empleado and isinstance(v_empleado, dict) and v_empleado.get("presente"):
        desc = "Empleado (cajero)"
        if v_empleado.get("descripcion"):
            desc += f" — {v_empleado['descripcion']}"
            if v_empleado.get("accion") and v_empleado["accion"] not in v_empleado.get("descripcion", ""):
                desc += f", {v_empleado['accion']}"
        elif v_empleado.get("accion"):
            desc += f" — {v_empleado['accion']}"
        actions_list = []
        if v_empleado.get("cajon_abierto"):
            actions_list.append("abrió cajón")
        if v_empleado.get("entrego_cambio"):
            actions_list.append("entregó cambio")
        if v_empleado.get("uso_datafono"):
            actions_list.append("usó datáfono")
        if v_empleado.get("entrego_platos"):
            actions_list.append(f"entregó {v_empleado['entrego_platos']} plato(s)")
        if actions_list:
            desc += f" | {', '.join(actions_list)}"
        detail_parts.append(desc)
    # Formato anterior (persons)
    if v_persons and not v_cliente and not v_empleado:
        for p in v_persons:
            role = p.get("role", "persona")
            clothing = p.get("clothing", "")
            action = p.get("action", p.get("acciones", ""))
            interaction = p.get("interaction", "")
            parts = [role.capitalize()]
            if clothing and isinstance(clothing, str) and clothing.strip():
                parts.append(f"({clothing})")
            elif clothing and isinstance(clothing, list):
                parts.append(f"({', '.join(str(c) for c in clothing)})")
            if action and isinstance(action, list):
                parts.append(" — " + ", ".join(str(a) for a in action))
            elif action and isinstance(action, str):
                parts.append(" — " + action.strip())
            if interaction and isinstance(interaction, str) and interaction.strip():
                inter = interaction.strip()
                if inter.lower() not in ("con cliente", "con cajero", "con el cliente", "con el cajero"):
                    parts.append(inter)
            detail_parts.append(" ".join(parts))
    if v_resumen and v_resumen != v_scene:
        detail_parts.append(v_resumen)
    elif v_scene:
        detail_parts.append(v_scene)
    elif qj_details.get("scene_context"):
        cleaned = _clean_detail_text(qj_details["scene_context"])
        if cleaned and "Zona" not in cleaned:
            detail_parts.append(cleaned)
    transaction = vision.get("transaction", {}) or qj.get("transaction", {})
    if isinstance(transaction, dict) and transaction.get("active"):
        t_type = transaction.get("type", "")
        t_details = transaction.get("details", "")
        if t_type or t_details:
            t_str = f"Transacción: {t_type}" if t_type else "Transacción activa"
            if t_details:
                t_str += f" — {t_details}"
            detail_parts.append(t_str)
    if qj.get("search_tags"):
        filtered_tags = [t for t in qj["search_tags"] if str(t).lower() not in ["visible", "presencia detectada", "persona detectada"]]
        if filtered_tags:
            detail_parts.append(", ".join(str(t) for t in filtered_tags[:5]))
    return detail_parts
    if qj.get("search_tags"):
        filtered_tags = [t for t in qj["search_tags"] if str(t).lower() not in ["visible", "presencia detectada", "persona detectada"]]
        if filtered_tags:
            detail_parts.append(", ".join(str(t) for t in filtered_tags[:5]))
    return detail_parts


def _is_generic_qwen_summary(summary: str) -> bool:
     s = str(summary or "").lower()
     generic_markers = [
         "escena tranquila", "escena repetitiva", "sin actividad sospechosa",
         "ninguna actividad sospechosa", "sin personas", "ninguna persona",
         "personas adicionales", "sin actividad", "actividad normal",
         "no se observan", "no hay actividad", "con ninguna",
         "no se detectó una anomalía", "no se detecto una anomalia",
         "no se detectó", "no se detecto", "sin anomalías", "sin anomalias",
         "todo normal", "todo está normal", "todo esta normal",
         "sin novedad", "sin novedades", "no hay novedades",
         "se observa una persona en", "se observa una persona en la zona",
     ]
     return any(m in s for m in generic_markers)


def _enrich_qwen_json_from_metadata(qwen_json: dict, metadata: dict, zone: str, after_hours: bool, is_after_hours: bool) -> dict:
    metadata = metadata or {}
    qwen_json = dict(qwen_json or {})
    if not isinstance(qwen_json.get("details"), dict):
        qwen_json["details"] = {}
    details = qwen_json["details"]
    yolo_classes = metadata.get("yolo_classes") or []
    total_yolo = int(metadata.get("total_yolo_objects") or 0 or 0)
    person_count = sum(1 for c in yolo_classes if str(c).lower() == "person") if isinstance(yolo_classes, list) else 0
    if not person_count and total_yolo > 0:
        person_count = max(1, min(total_yolo, 16))
    # Solo llenar fallback si Qwen no dio datos de vision
    vision = qwen_json.get("vision", {}) if isinstance(qwen_json.get("vision"), dict) else {}
    vision_persons = vision.get("persons", []) or qwen_json.get("persons", [])
    if not isinstance(vision_persons, list):
        vision_persons = []
    vision_cliente = vision.get("cliente") or qwen_json.get("cliente")
    vision_empleado = vision.get("empleado") or qwen_json.get("empleado")
    vision_resumen = vision.get("summary", "") or qwen_json.get("summary", "")
    vision_attention_hits = qwen_json.get("attention_hits", [])
    vision_counts = qwen_json.get("counts", {})
    vision_has_data = len(vision_persons) > 0 or (vision_cliente and isinstance(vision_cliente, dict) and vision_cliente.get("presente")) or (vision_empleado and isinstance(vision_empleado, dict) and vision_empleado.get("presente")) or (len(str(vision_resumen)) > 0)

    if person_count > 0:
        current = int(details.get("persons_visible") or 0 or 0)
        details["persons_visible"] = max(current, person_count)
        # Solo llenar fallback si Qwen no dio datos de vision
        if not vision_has_data:
            if not details.get("persons_description"):
                details["persons_description"] = f"Se observó {person_count} persona(s) en la zona."
            details["actions_visible"] = []
            details["objects_visible"] = []
            # NO sobrescribir summary si ya viene de Qwen
            existing_summary = str(qwen_json.get("summary") or "")
            if not existing_summary or _is_generic_qwen_summary(existing_summary):
                qwen_json["summary"] = f"Se observa una persona en {zone}; no se detectó una anomalía concreta."
        # Si Qwen sí dio datos, construir summary desde vision real
        else:
            # Limpiar details vacíos
            if not details.get("actions_visible"):
                details["actions_visible"] = []
            if not details.get("objects_visible"):
                details["objects_visible"] = []
            if not details.get("clothing_visible"):
                details["clothing_visible"] = []
            if not details.get("scene_context"):
                details["scene_context"] = f"Zona {zone}, dentro de horario."
            # Construir summary desde vision real (nuevo formato: cliente/empleado/summary)
            existing_summary = str(qwen_json.get("summary") or "")
            if not existing_summary or _is_generic_qwen_summary(existing_summary):
                vision = qwen_json.get("vision", {})
                v_cliente = vision.get("cliente") or qwen_json.get("cliente")
                v_empleado = vision.get("empleado") or qwen_json.get("empleado")
                v_resumen = ""
                if isinstance(vision, dict):
                    v_resumen = str(vision.get("summary", "") or "")
                if not v_resumen:
                    v_resumen = str(qwen_json.get("summary", "") or "")
                v_persons = vision.get("persons", []) if isinstance(vision, dict) else []
                if not isinstance(v_persons, list):
                    v_persons = []
                v_scene = vision.get("scene", "") if isinstance(vision, dict) else ""
                v_objects = vision.get("objects", []) if isinstance(vision, dict) else []
                if not isinstance(v_objects, list):
                    v_objects = []
                parts = []
                if v_cliente and isinstance(v_cliente, dict) and v_cliente.get("presente"):
                    desc = "Cliente"
                    if v_cliente.get("descripcion"):
                        desc += f" — {v_cliente['descripcion']}"
                    if v_cliente.get("accion") and v_cliente["accion"] not in v_cliente.get("descripcion", ""):
                        desc += f", {v_cliente['accion']}"
                    if v_cliente.get("pago"):
                        pago_str = f" | Pago: {v_cliente['pago']}"
                        if v_cliente.get("cantidad_billetes"):
                            pago_str += f" ({v_cliente['cantidad_billetes']} billete(s))"
                        if v_cliente.get("denominacion"):
                            pago_str += f" de {v_cliente['denominacion']}"
                        desc += pago_str
                    if v_cliente.get("platos"):
                        desc += f" | Pedido: {v_cliente['platos']} plato(s)"
                    parts.append(desc)
                if v_empleado and isinstance(v_empleado, dict) and v_empleado.get("presente"):
                    desc = "Empleado (cajero)"
                    if v_empleado.get("descripcion"):
                        desc += f" — {v_empleado['descripcion']}"
                    if v_empleado.get("accion") and v_empleado["accion"] not in v_empleado.get("descripcion", ""):
                        desc += f", {v_empleado['accion']}"
                    actions = []
                    if v_empleado.get("cajon_abierto"):
                        actions.append("abrió cajón")
                    if v_empleado.get("entrego_cambio"):
                        actions.append("entregó cambio")
                    if v_empleado.get("uso_datafono"):
                        actions.append("usó datáfono")
                    if v_empleado.get("entrego_platos"):
                        actions.append(f"entregó {v_empleado['entrego_platos']} platos")
                    if actions:
                        desc += f" | {', '.join(actions)}"
                    parts.append(desc)
                else:
                    for p in v_persons:
                        role = p.get("role", "persona")
                        clothing = p.get("clothing", "")
                        action = p.get("action", p.get("acciones", ""))
                        interaction = p.get("interaction", "")
                        desc = role.capitalize()
                        if clothing:
                            desc += f" ({clothing})" if isinstance(clothing, str) else f" ({chr(44).join(str(c) for c in clothing)})"
                        if action:
                            desc += f" — {action}"
                        if interaction:
                            parts.append(desc)
                if v_resumen and len(v_resumen) > 20:
                    parts.append(v_resumen)
                elif v_scene:
                    parts.append(v_scene)
                v_objects_filtered = [str(o) for o in v_objects if str(o).lower() not in ("silla", "mesa", "pared", "techo", "lámpara", "cable")]
                if v_objects_filtered:
                    parts.append("Objetos: " + ", ".join(v_objects_filtered))
                if parts:
                    qwen_json["summary"] = ". ".join(parts) + "."
                else:
                    qwen_json["summary"] = f"Actividad normal en {zone}."
    if not details.get("scene_context"):
        horario = "fuera de horario" if after_hours else "dentro de horario"
        details["scene_context"] = f"Zona {zone}, {horario}."
    if not details.get("camera_condition"):
        details["camera_condition"] = "visible"
    if not details.get("clothing_visible"):
        details["clothing_visible"] = []
    if not isinstance(qwen_json.get("search_tags"), list) or not qwen_json.get("search_tags"):
        tags = []
        if after_hours:
            tags.append("fuera de horario")
        else:
            tags.append("dentro de horario")
        tags.append(zone)
        for tag in details.get("scene_context", "").split():
            clean_tag = re.sub(r"[^a-zA-ZáéíóúÁÉÍÓÚñÑ0-9]+", "", tag).lower()
            if len(clean_tag) >= 4 and clean_tag not in [t.lower() for t in tags]:
                tags.append(clean_tag)
        qwen_json["search_tags"] = tags[:12]
    if not isinstance(qwen_json.get("evidence"), list) or not qwen_json.get("evidence"):
        if not qwen_json.get("violation") and not qwen_json.get("attention_hits"):
            qwen_json["evidence"] = ["Sin observaciones relevantes en la zona."]
    qwen_json.setdefault("attention_hits", [])
    qwen_json.setdefault("counts", {})
    return qwen_json


# ═══════════════════════════════════════════════════════════════════════════
# ETAPA 2: Rule Engine local — compara visión contra reglas
# ═══════════════════════════════════════════════════════════════════════════

def _apply_rules(vision: dict, cam_cfg: dict, zone: str, is_after_hours: bool, mode: str) -> dict:
    """Wrapper hacia _detect_attention_hits para compatibilidad."""
    attention_phrases = []
    owner_notes = []
    vigilance = cam_cfg.get("vigilance", {}) if isinstance(cam_cfg.get("vigilance"), dict) else {}
    attention_phrases = vigilance.get("attention_phrases", []) or cam_cfg.get("attention_phrases", []) or []
    owner_notes = vigilance.get("owner_notes", []) or cam_cfg.get("owner_notes", []) or []
    return _detect_attention_hits(vision, attention_phrases, owner_notes, zone, is_after_hours, mode)


def _max_severity(a: str, b: str) -> str:
    order = {"baja": 0, "media": 1, "alta": 2, "critica": 3}
    return a if order.get(a, 0) >= order.get(b, 0) else b


def _detect_attention_hits(vision: dict, attention_phrases: list, owner_notes: list,
                            zone: str, is_after_hours: bool, mode: str) -> dict:
    """Detecta si el relato de Qwen contiene frases de atención configuradas.

    NO juzga, NO decide violaciones. Solo observa si lo que el dueño quería
    vigilar fue visiblemente mencionado en la narrativa de Qwen.

    Retorna coincidencias observacionales para que el sistema decida si notifica.
    """
    checks = {}
    anomalias = []
    importance = "normal"

    relato_text = json.dumps(vision, ensure_ascii=False).lower()
    resumen = vision.get("resumen", "") if isinstance(vision, dict) else ""
    attention_hits_raw = vision.get("attention_hits", []) if isinstance(vision, dict) else []

    hits = []

    # 1. Verificar si Qwen reportó attention_hits explícitamente
    if isinstance(attention_hits_raw, list):
        for hit in attention_hits_raw:
            if isinstance(hit, dict) and hit.get("frase"):
                hits.append({
                    "frase": hit["frase"],
                    "momento": hit.get("momento", ""),
                    "source": "qwen_explicit"
                })

    # 2. Verificar phrases de atención (respaldo por keywords)
    if attention_phrases:
        for phrase in attention_phrases:
            phrase_lower = phrase.lower()
            if phrase_lower in relato_text:
                if not any(h["frase"].lower() == phrase_lower for h in hits):
                    hits.append({
                        "frase": phrase,
                        "momento": "",
                        "source": "keyword_match"
                    })

    # 3. Evaluar notas del dueño (contexto) — ¿falso positivo conocido?
    false_positive_notes = []
    for note in (owner_notes or []):
        note_lower = note.lower()
        if any(kw in note_lower for kw in ["es normal", "no es falta", "falso positivo", "no menciones", "no lo menciones"]):
            for hit in hits:
                if any(word in hit["frase"].lower() for word in note_lower.split() if len(word) > 5):
                    false_positive_notes.append({"nota": note, "hit": hit["frase"]})

    # 4. Fuera de horario siempre es observación relevante (no juicio)
    checks["fuera_de_horario"] = is_after_hours
    if is_after_hours and mode == "normal":
        persons_count = 0
        if isinstance(vision, dict):
            persons_count = len(vision.get("personas", []))
        if persons_count > 0 and not hits:
            hits.append({
                "frase": "presencia_fuera_de_horario",
                "momento": "fuera de horario laboral",
                "source": "system_after_hours"
            })

    # 5. Construir anomalías observacionales (NO acusatorias)
    for hit in hits:
        skip = False
        for fp in false_positive_notes:
            if hit["frase"] == fp["hit"]:
                skip = True
                break
        if skip:
            continue
        anomalias.append({
            "tipo": "attention_hit",
            "descripcion": f"Se observó: {hit['frase']}" + (f" ({hit['momento']})" if hit.get("momento") else ""),
            "observacion": True,
            "severidad": "observacion",
            "source": hit.get("source", "unknown")
        })

    violation = len(anomalias) > 0
    if violation:
        importance = "alta"

    summary = _build_vision_summary(vision, zone, violation, anomalias)

    return {
        "checks": checks,
        "violation": violation,
        "importance": importance,
        "importancia": importance,
        "anomalias": anomalias,
        "attention_hits": [h["frase"] for h in hits],
        "false_positives_detected": len(false_positive_notes),
        "summary": summary,
        "evidence": [a["descripcion"] for a in anomalias] if anomalias else ["Sin observaciones relevantes"],
    }


def _is_scene_unchanged(current_frames: list, previous_frames: list, threshold: float = 0.95) -> bool:
    """Detecta si la escena no cambió significativamente entre grids.

    Compara número de personas, posiciones YOLO y conteo de objetos.
    Si la escena es esencialmente la misma, no se llama a Qwen.
    Retorna True si se debe skippear el análisis de Qwen.
    """
    if not current_frames or not previous_frames:
        return False

    current_person_count = sum(1 for f in current_frames for c in (f.get("yolo_classes") or []) if str(c).lower() == "person")
    prev_person_count = sum(1 for f in previous_frames for c in (f.get("yolo_classes") or []) if str(c).lower() == "person")

    if current_person_count != prev_person_count:
        return False

    current_total = sum(f.get("yolo_count", 0) for f in current_frames)
    prev_total = sum(f.get("yolo_count", 0) for f in previous_frames)

    if current_total == 0 and prev_total == 0:
        return True

    if prev_total > 0:
        ratio = min(current_total, prev_total) / max(current_total, prev_total)
        if ratio < threshold:
            return False

    if current_person_count == 0 and prev_person_count == 0:
        current_classes = set()
        prev_classes = set()
        for f in current_frames:
            current_classes.update(c.lower() for c in (f.get("yolo_classes") or []))
        for f in previous_frames:
            prev_classes.update(c.lower() for c in (f.get("yolo_classes") or []))
        if current_classes == prev_classes:
            return True

    return False


def _build_search_tags(vision: dict, rule_result: dict) -> list:
    """Genera tags de búsqueda desde visión + reglas."""
    tags = []
    persons = vision.get("persons", []) if isinstance(vision.get("persons"), list) else []
    objects = vision.get("objects", []) if isinstance(vision.get("objects"), list) else []

    for p in persons:
        if isinstance(p, dict):
            for c in (p.get("clothing", []) if isinstance(p.get("clothing"), list) else []):
                tag = str(c).lower().strip()
                if tag and tag not in tags:
                    tags.append(tag)
            loc = p.get("location", "")
            if loc:
                tag = str(loc).lower().strip()
                if tag and tag not in tags:
                    tags.append(tag)
            for a in (p.get("actions", []) if isinstance(p.get("actions"), list) else []):
                tag = str(a).lower().strip()
                if tag and tag not in tags:
                    tags.append(tag)

    for o in objects:
        tag = str(o).lower().strip()
        if tag and tag not in tags:
            tags.append(tag)

    for a in (rule_result.get("anomalias", []) if isinstance(rule_result.get("anomalias"), list) else []):
        if isinstance(a, dict):
            tag = a.get("tipo", "").lower().strip()
            if tag and tag not in tags:
                tags.append(tag)

    # Filtrar palabras técnicas y objetos irrelevantes
    technical_words = {"presencia detectada", "persona detectada", "visible", "no visible",
                       "silla", "mesa", "cable", "lámpara", "ventilador", "taza de café",
                       "plato", "vaso", "papel", "libro", "bolso", "mochila", "zapato",
                       "pared", "paredes", "techo", "ventana", "puerta", "piso", "cielo"}
    tags = [t for t in tags if t not in technical_words and len(t) > 2]

    return tags[:15]


def _build_vision_summary(vision: dict, zone: str, violation: bool, anomalias: list) -> str:
    """Genera summary en español natural desde datos de visión de Qwen.
    Solo incluye información relevante para el usuario.
    """
    persons = vision.get("persons", []) if isinstance(vision.get("persons"), list) else []
    objects = vision.get("objects", []) if isinstance(vision.get("objects"), list) else []
    scene = vision.get("scene", "")
    attention_hits = vision.get("attention_hits", []) if isinstance(vision, dict) else []

    # Objetos irrelevantes que no aportan valor para seguridad
    irrelevant_objects = {"silla", "mesa", "cable", "lámpara", "ventilador", "taza de café",
                           "tazas", "plato", "platos", "vaso", "vasos", "papel", "papeles",
                           "libro", "libros", "bolso", "bolsos", "mochila", "mochilas",
                           "zapato", "zapatos", "ropa", "percha", "perchas", "estante", "estantes",
                           "pared", "paredes", "techo", "cielo", "ventana", "puerta", "piso",
                           "alfombra", "cortina", "planta", "florero", "adorno", "cuadro", "espejo"}

    if violation or attention_hits:
        if attention_hits:
            hits_str = ", ".join(attention_hits[:3])
            return f"🔍 Observación relevante: {hits_str}"
        if anomalias:
            anom_tipos = [a.get("tipo", "") for a in (anomalias if isinstance(anomalias, list) else []) if isinstance(a, dict)]
            if anom_tipos:
                return f"🔍 Observación relevante: {', '.join(anom_tipos[:2])}"
        return f"🔍 Observación relevante en {zone}"

    parts = []
    for p in persons:
        if isinstance(p, dict):
            person_desc = "Persona"
            loc = p.get("location", "").strip()
            clothing = p.get("clothing", [])
            actions = p.get("actions", [])

            # Ropa (si es descriptiva y no muy larga)
            if clothing and isinstance(clothing, list):
                clothing_str = ", ".join(str(c).strip() for c in clothing[:2] if str(c).strip())
                if clothing_str and 3 < len(clothing_str) < 40:
                    person_desc += f" con {clothing_str}"

            # Acción (más importante que la ubicación)
            if actions and isinstance(actions, list):
                action_str = ", ".join(str(a).strip() for a in actions[:2] if str(a).strip())
                if action_str and len(action_str) > 3:
                    person_desc += f" {action_str}"
            elif loc:
                # Solo incluir ubicación si es relevante
                loc_lower = loc.lower()
                relevant_locations = {"detrás del mostrador", "detrás de caja", "en caja",
                                      "frente a caja", "en el mostrador", "detrás del counter"}
                if any(rl in loc_lower for rl in relevant_locations):
                    person_desc += f" {loc}"

            parts.append(person_desc)

    # Solo incluir objetos relevantes para seguridad
    # Filtrar objetos que claramente NO son relevantes
    irrelevant_keywords = {"silla", "mesa", "cable", "lámpara", "ventilador", "taza",
                            "plato", "vaso", "papel", "libro", "bolso", "mochila",
                            "zapato", "percha", "estante", "pantalla", "monitor",
                            "teclado", "ratón", "mouse", "alfombra", "cortina",
                            "planta", "florero", "adorno", "cuadro", "espejo",
                            "mostrador", "barra", "repisa", "cajón", "cajones",
                            "puerta", "ventana", "pared", "piso", "techo",
                            "lámpara", "foco", "bombilla", "tubo", "tubería"}

    relevant_objects = []
    for o in objects:
        o_str = str(o).strip()
        o_lower = o_str.lower()
        # Excluir si contiene palabras irrelevantes
        if o_str and not any(ik in o_lower for ik in irrelevant_keywords) and len(o_str) > 2:
            relevant_objects.append(o_str)

    if relevant_objects:
        parts.append(f"Objetos: {', '.join(relevant_objects[:3])}")

    # Siempre incluir scene si es descriptiva
    if scene:
        scene_clean = str(scene).strip()
        if "brief scene description" not in scene_clean.lower() and len(scene_clean) > 5:
            parts.append(f"Escena: {scene_clean[:80]}")

    # Incluir observations como contexto
    if vision_observations := vision.get("observations", {}):
        obs_notes = []
        if vision_observations.get("hand_near_pocket"):
            obs_notes.append("mano cerca del bolsillo")
        if vision_observations.get("register_drawer_open"):
            obs_notes.append("cajón abierto")
        if vision_observations.get("money_visible"):
            obs_notes.append("dinero visible")
        if obs_notes:
            parts.append("Observaciones: " + ", ".join(obs_notes[:2]))

    if parts:
        return ". ".join(parts)
    return "Sin novedad en zona"


class QwenOrchestrator:
    """Orquestador simple para Qwen Vision"""
    
    def __init__(self, max_concurrent: int = 4, timeout: float = 15.0):
        self.max_concurrent = max_concurrent
        self.timeout = timeout
        self._semaphore = asyncio.Semaphore(max_concurrent)
        # ── Grid independiente por cámara ──
        # Clave: "{user_id}_{camera_id}" → FrameGrid
        self.grids: Dict[str, FrameGrid] = {}
        self._grid_lock = threading.Lock()
        # Cooldown tracking por cámara: último timestamp de notificación
        self._last_notification_ts: Dict[str, float] = {}
        # Configuración de cooldown (en segundos)
        self._notification_cooldown = 300  # 5 minutos entre notificaciones por cámara
    
    def _get_grid(self, user_id: str, camera_id: str, grid_size: int = 16) -> FrameGrid:
        """Obtener o crear grid para una cámara específica."""
        cam_key = f"{user_id}_{camera_id}"
        grid_size = max(1, min(int(grid_size or 16), 16))
        with self._grid_lock:
            grid = self.grids.get(cam_key)
            if grid is None or grid.max_frames != grid_size:
                self.grids[cam_key] = FrameGrid(max_frames=grid_size)
            return self.grids[cam_key]
    
    def _cleanup_grid(self, user_id: str, camera_id: str):
        """Eliminar grid de una cámara (cuando se desactiva)."""
        cam_key = f"{user_id}_{camera_id}"
        with self._grid_lock:
            self.grids.pop(cam_key, None)
    
    def _get_all_grid_info(self) -> Dict[str, Any]:
        """Información de todos los grids activos (para admin)."""
        with self._grid_lock:
            return {
                cam_key: {
                "frame_count": grid.get_frame_count(),
                "camera_id": grid.get_last_camera_id(),
                "grid_size": grid.max_frames,
                }
                for cam_key, grid in self.grids.items()
            }
    
    # ── Acceso al grid de una cámara específica ──
    
    @property
    def grid(self):
        """Compatibilidad: retorna el primer grid activo (o uno vacío)."""
        with self._grid_lock:
            if self.grids:
                return next(iter(self.grids.values()))
        return FrameGrid(max_frames=16)  # Fallback vacío

    # ═══════════════════════════════════════════════════════════════════════════
    # ETAPA 1: Vision Analyst — Qwen solo describe, no juzga
    # ═══════════════════════════════════════════════════════════════════════════

    async def _call_qwen_vision(
         self, grid_img_b64: str, zone: str, business_name: str,
         business_type: str, schedule_open: str, schedule_close: str,
         mode: str, is_after_hours: bool, total_yolo: int, yolo_stats: dict,
         cam_cfg: dict, frames: list = None, concern: str = "",
         attention_phrases: list = None, owner_notes: list = None
     ) -> dict:
        """Etapa 1: Qwen describe la escena de forma natural para el libro de eventos."""
        
        # Prompt optimizado para descripción narrativa natural (sin formato JSON complejo)
        vision_prompt = (
            f"Analiza estos 16 fotogramas (en formato cuadrícula). "
            f"Realiza una descripción narrativa detallada y natural de lo que ocurre en la escena, "
            f"como si le contaras a alguien lo que está pasando en el video. "
            f"Enfócate en: personas presentes, qué hacen, cómo visten, y cualquier objeto relevante "
            f"(dinero, platos, bolsas, datáfono). "
            f"Si la zona está vacía, indícalo claramente. "
            f"Responde en español, con lenguaje natural, fluido y directo. "
            f"NO uses formatos estructurados, solo una narrativa clara."
        )

        # LOGGING CRUDO - Capa 1: prompt exacto enviado a Qwen
        logger.info(f"[QWEN_PROMPT] {vision_prompt[:500]}")

        # Construir content con frames individuales + grid
        content = []

        # Agregar frames individuales (hasta 4 recientes)
        if frames:
            recent_frames = frames[-4:] if len(frames) >= 4 else frames
            for i, f in enumerate(recent_frames):
                if "image_bytes" in f and f["image_bytes"]:
                    try:
                        frame_b64 = image_to_base64(f["image_bytes"])
                        content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{frame_b64}"}})
                    except Exception:
                        pass

        # Agregar grid
        content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{grid_img_b64}"}})

        # Agregar prompt de texto
        content.append({"type": "text", "text": vision_prompt})

        payload = {
            "model": "qwen",
            "messages": [{"role": "user", "content": content}],
            "max_tokens": 500
        }

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post("http://localhost:8004/v1/chat/completions", json=payload)
                resp.raise_for_status()
                raw_content = resp.json()["choices"][0]["message"]["content"]
                # LOGGING CRUDO - Capa 2: respuesta cruda de Qwen
                logger.info(f"[QWEN_RAW] {raw_content[:500]}")
                parsed = _parse_qwen_json(raw_content)
                # LOGGING CRUDO - Capa 3: JSON parseado
                logger.info(f"[QWEN_PARSED] {json.dumps(parsed, ensure_ascii=False)[:500]}")
                return parsed
        except Exception as e:
            logger.error(f"Vision Analyst error: {e}")
            return {}

    async def submit(
        self, 
        image_bytes: bytes, 
        prompt: str, 
        priority: int = 10
    ) -> Optional[str]:
        """Envía imagen a Qwen con limitación de concurrencia"""
        async with self._semaphore:
            return await self._call_qwen(image_bytes, prompt)
    
    async def process_grid(self, prompt: str = None, vigilance_prompt: str = None,
                                vigilance_rules: str = None, use_grid_image: bool = True,
                                user_id: str = "default", camera_id: str = "unknown",
                                mode: str = "normal", grid_size: int = 16) -> Dict[str, Any]:
            """Process full grid of 16 frames with Qwen.
    
            Arquitectura 2 etapas:
            Etapa 1: Qwen Vision Analyst — solo describe (personas, ropa, acciones, objetos)
            Etapa 2: Attention Hit Detection — detecta si lo observado coincide con frases de atención
            """
            grid = self._get_grid(user_id, camera_id, grid_size=grid_size)
            frames = grid.get_and_reset()
    
            if not frames:
                return {"error": "No frames in grid"}
    
            # Extraer vigilance_prompt y vigilance_rules del primer frame (enviados desde api_eva.py)
            if vigilance_prompt is None and frames:
                vigilance_prompt = frames[0].get("vigilance_prompt")
            if vigilance_rules is None and frames:
                vigilance_rules = frames[0].get("vigilance_rules")
    
            # ── Normalizar configuración y modo ──
            cam_cfg = normalize_camera_vigilance_config(get_camera_config(user_id, camera_id))
            mode = mode or "normal"
            if mode not in ("normal", "sentinel"):
                mode = "normal"
            _sched = cam_cfg.get("schedule", {}) or {}
            _schedule_open = _sched.get("open", "08:00")
            _schedule_close = _sched.get("close", "22:00")
            _now_val = datetime.datetime.now().hour * 60 + datetime.datetime.now().minute
            try:
                is_after_hours = _now_val < (int(_schedule_open.split(":")[0]) * 60 + int(_schedule_open.split(":")[1])) or \
                                  _now_val > (int(_schedule_close.split(":")[0]) * 60 + int(_schedule_close.split(":")[1]))
            except Exception:
                is_after_hours = False
    
            # ── YOLO stats ───────────────────────────────────────────────────────

            # Extract tracking summary for person tracking across frames
            tracking_summary = self._extract_tracking_summary(frames)
            total_yolo_objects = sum(f.get("yolo_count", 0) for f in frames)
            zone = cam_cfg.get("zone", camera_id)
    
            business_name = ""
            business_type = ""
            concern = ""
            try:
                uf2 = f"{STORAGE_ROOT}/users/{user_id}/user.json"
                if os.path.exists(uf2):
                    with open(uf2) as _uf2:
                        ud = json.load(_uf2)
                        business_name = ud.get("business_name", "")
                        business_type = ud.get("business_type", "")
                        concern = ud.get("main_concerns", [""])[0] if isinstance(ud.get("main_concerns"), list) else ""
            except Exception:
                pass
    
            # ── Construir analysis_prompt ────────────────────────────────────────
            after_note = "\nATENCIÓN: Ahora mismo es FUERA DE HORARIO laboral." if is_after_hours else ""
            yolo_stats = {
                "total_yolo_objects": total_yolo_objects,
                "classes": sorted(set(cls for f in frames for cls in (f.get("yolo_classes") or []))),
                "count_by_frame": [f.get("yolo_count", 0) for f in frames],
            }
    
            use_grid_image = True
    
            grid_result = None
            grid_img = None
            vision_json = {}
    
            # ── Extraer attention_phrases y owner_notes de cam_cfg ──────────────
            attention_phrases = cam_cfg.get("attention_phrases", []) or []
            owner_notes = cam_cfg.get("owner_notes", []) or []
            if not attention_phrases:
                vigilance = cam_cfg.get("vigilance", {}) if isinstance(cam_cfg.get("vigilance"), dict) else {}
                attention_phases = vigilance.get("attention_phrases", []) or []
                owner_notes = vigilance.get("owner_notes", []) or []
    
            if use_grid_image and len(frames) > 1:
                grid_img = create_grid_image([f["image_bytes"] for f in frames], max_size=224)
                logger.info(f"[GRID] Grid created: {len(grid_img)} bytes, frames={len(frames)}")
                try:
                    grid_b64 = image_to_base64(grid_img)
                    vision_json = await self._call_qwen_vision(
                         grid_b64, zone, business_name, business_type,
                         _schedule_open, _schedule_close, mode, is_after_hours,
                         total_yolo_objects, yolo_stats, cam_cfg, frames=frames, concern=concern,
                         attention_phrases=attention_phrases, owner_notes=owner_notes
                     )
                    vision_json = _convert_qwen_vision_response(vision_json)
                    logger.info(f"[VISION] Qwen response: persons={len(vision_json.get('persons',[]))} scene={vision_json.get('scene','')[:50]}")
                except Exception as e:
                    logger.error(f"[VISION] Error: {e}", exc_info=True)
                    grid_result = f"Error analizando grid: {str(e)}"
                    vision_json = {}
            else:
                logger.info(f"[GRID] Skipping Qwen: use_grid={use_grid_image} frames={len(frames)}")
    
            # ── Etapa 2: Attention Hit Detection (no reglas, solo observación) ──
            rule_result = _detect_attention_hits(vision_json, attention_phrases, owner_notes, zone, is_after_hours, mode)
    
            # ── Armar qwen_json final ─────────────────────────────────────────────
            qwen_json = {
                "vision": vision_json,
                "rule_checks": rule_result["checks"],
                "attention_hits": rule_result.get("attention_hits", []),
                "false_positives_detected": rule_result.get("false_positives_detected", 0),
                "importance": rule_result["importance"],
                "importancia": rule_result["importance"],
                "summary": rule_result["summary"],
                "anomalias": rule_result["anomalias"],
                "evidence": rule_result["evidence"],
                "search_tags": _build_search_tags(vision_json, rule_result),
                "mode": mode,
            }
            qwen_json = _normalize_rich_qwen_json(qwen_json, mode)
            qwen_json = _enrich_qwen_json_from_metadata(qwen_json, {
                "frames_count": len(frames),
                "total_yolo_objects": total_yolo_objects,
                "yolo_classes": sorted(set(cls for f in frames for cls in (f.get("yolo_classes") or []))),
                "yolo_count_by_frame": [f.get("yolo_count", 0) for f in frames],
                "business_name": business_name,
                "schedule": f"{_schedule_open}-{_schedule_close}",
                "after_hours": is_after_hours,
                "mode": mode,
            }, zone, is_after_hours, is_after_hours)
    
            attention_detected = rule_result["violation"]
            attention_hits = rule_result.get("attention_hits", [])
    
            # ── Cooldown de notificación por cámara ────────────────────────────
            cam_key = f"{user_id}_{camera_id}"
            last_notif = self._last_notification_ts.get(cam_key, 0)
            cooldown_ok = (time.time() - last_notif) > self._notification_cooldown
    
            # ── Datos del evento ─────────────────────────────────────────────────
            user_id = frames[0]["user_id"] if frames else user_id
            camera_id = frames[0]["camera_id"] if frames else "unknown"
            event_type = "attention" if attention_detected else "normal"
            summary = qwen_json.get("summary", "") if isinstance(qwen_json, dict) else ""
            if not summary:
                summary = _build_summary_from_rich_qwen(qwen_json, zone, attention_detected)
                qwen_json["summary"] = summary
            # Extraer resumen del nuevo formato si existe
            if isinstance(qwen_json, dict):
                v_resumen = qwen_json.get("vision", {}).get("resumen", "")
                if v_resumen and len(v_resumen) > len(summary):
                    qwen_json["summary"] = v_resumen
                    summary = v_resumen
    
            event_id = save_event_to_disk_v2(
                user_id=user_id,
                camera_id=camera_id,
                event_type=event_type,
                frame_bytes=frames[0]["image_bytes"] if frames else b'',
                summary=summary,
                qwen_json=qwen_json,
                metadata={
                    "frames_count": len(frames),
                    "total_yolo_objects": total_yolo_objects,
                    "yolo_classes": sorted(set(cls for f in frames for cls in (f.get("yolo_classes") or []))),
                    "yolo_count_by_frame": [f.get("yolo_count", 0) for f in frames],
                    "person_tracking": tracking_summary,
                    "grid_frames": [f.get("image_bytes", b"") for f in frames],
                    "frame_timestamps": [f.get("timestamp") for f in frames],
                    "business_name": business_name,
                    "schedule": f"{_schedule_open}-{_schedule_close}",
                    "after_hours": is_after_hours,
                    "mode": mode,
                    "grid_b64": image_to_base64(grid_img) if grid_img else "",
                    "qwen_details": qwen_json.get("details") if isinstance(qwen_json.get("details"), dict) else {},
                    "qwen_search_tags": qwen_json.get("search_tags") if isinstance(qwen_json.get("search_tags"), list) else [],
                    "attention_hits": attention_hits,
                    "counts": vision_json.get("counts", {}) if isinstance(vision_json, dict) else {},
                }
            )
    
            update_camera_metrics(user_id, camera_id, event_type=event_type)
    
            # ── Notificación push (solo attention hits + cooldown) ──────────────
            if attention_detected and cooldown_ok and attention_hits:
                try:
                    now_str = time.strftime("%H:%M", time.localtime())
                    first_hit = attention_hits[0] if attention_hits else "comportamiento observado"
                    title = f"📷 Algo que quizás quieras revisar — {zone}"
                    body = (f"Nuestro sistema detectó algo que coincide con lo que me pediste vigilar:\n\n"
                            f"🔍 {first_hit}\n\n"
                            f"📝 Contexto: {summary[:100]}\n\n"
                            f"🕐 {now_str} | 📍 {business_name or zone}")
                    event_link = f"https://ojoia.com.do/#eva?alert={event_id}&camera={camera_id}"
                    _fcm_task = asyncio.create_task(send_fcm_notification(
                        title=title, body=body, user_id=user_id,
                        image_b64=image_to_base64(frames[0]["image_bytes"]) if frames else None,
                        link=event_link
                    ))
                    _fcm_task.add_done_callback(
                        lambda t: logging.info(f"FCM sent: {title[:40]}") if not t.exception()
                        else logging.error(f"FCM error: {t.exception()}")
                    )
                    self._last_notification_ts[cam_key] = time.time()
                except Exception as _fcm_err:
                    logging.error(f"FCM error: {_fcm_err}")
            elif attention_detected:
                remaining = self._notification_cooldown - (time.time() - last_notif)
                logging.info(f"Notification suppressed for {cam_key} (cooldown, {remaining:.0f}s)")
    
            return {
                "frames_processed": len(frames),
                "grid_result": grid_result,
                "qwen_json": qwen_json,
                "attention_hits": attention_hits,
                "attention_detected": attention_detected,
                "mode": mode,
                "event_id": event_id,
                "action_taken": "event_saved_and_notification_sent" if (attention_detected and cooldown_ok and attention_hits) else "event_saved",
            }

    async def analyze_grid_and_save_event(self, user_id: str = "default", camera_id: str = "unknown",
                                          vigilance_prompt: str = None, vigilance_rules: str = None,
                                          business_name: str = "", business_type: str = "",
                                          schedule_open: str = "", schedule_close: str = "",
                                          mode: str = "normal", is_after_hours: bool = False) -> dict:
        """Wrapper around process_grid for api_eva.py compatibility."""
        return await self.process_grid(
            user_id=user_id, camera_id=camera_id,
            vigilance_prompt=vigilance_prompt, vigilance_rules=vigilance_rules,
            mode=mode,
        )

    async def _call_qwen(self, image_bytes: bytes, prompt: str) -> str:
        """Llama a Qwen con timeout"""
        resized = resize_image(image_bytes, max_size=512)
        img_b64 = image_to_base64(resized)

        payload = {
            "model": "qwen",
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"}},
                    {"type": "text", "text": prompt}
                ]
            }],
            "max_tokens": 1200
        }

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post("http://localhost:8004/v1/chat/completions", json=payload)
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"]


    def _extract_tracking_summary(self, frames: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Extract tracking summary from frames for person tracking across grid."""
        if not frames:
            return {"unique_persons": 0, "total_detections": 0, "tracks": []}
        track_data = {}
        total_detections = 0
        for frame_idx, frame in enumerate(frames):
            detections = frame.get("yolo_detections", [])
            for det in detections:
                if det.get("class", "").lower() == "person":
                    track_id = det.get("track_id")
                    if track_id is not None:
                        total_detections += 1
                        if track_id not in track_data:
                            track_data[track_id] = {"frame_count": 0, "frames_set": set(), "confidences": []}
                        track_data[track_id]["frame_count"] += 1
                        track_data[track_id]["frames_set"].add(frame_idx)
                        track_data[track_id]["confidences"].append(det.get("confidence", 0.0))
        tracks = []
        for track_id, data in track_data.items():
            frames_sorted = sorted(data["frames_set"])
            avg_conf = sum(data["confidences"]) / len(data["confidences"]) if data["confidences"] else 0.0
            tracks.append({
                "id": int(track_id),
                "frames": data["frame_count"],
                "first_frame": frames_sorted[0] if frames_sorted else 0,
                "last_frame": frames_sorted[-1] if frames_sorted else 0,
                "avg_confidence": round(avg_conf, 3),
                "presence_ratio": round(data["frame_count"] / len(frames), 3)
            })
        tracks.sort(key=lambda t: t["frames"], reverse=True)
        return {"unique_persons": len(track_data), "total_detections": total_detections, "tracks": tracks}


    def add_frame(self, image_bytes: bytes, camera_id: str, user_id: str, yolo_count: int = 0,
                  yolo_classes: list = None, yolo_detections: list = None, vigilance_prompt: str = None,
                  vigilance_rules: str = None, burst_mode: bool = False,
                  mode: str = "normal", grid_size: int = 16) -> Dict[str, Any]:
        """Add frame to the specific grid for this camera. When grid is full, triggers analysis."""
        grid = self._get_grid(user_id, camera_id, grid_size=grid_size)
        is_full = grid.add_frame(image_bytes, camera_id, user_id, yolo_count=yolo_count, yolo_classes=yolo_classes, yolo_detections=yolo_detections, mode=mode,
                             vigilance_prompt=vigilance_prompt, vigilance_rules=vigilance_rules)
        result = {
            "frame_count": grid.get_frame_count(),
            "grid_size": grid.max_frames,
            "grid_full": is_full,
            "ready_for_analysis": is_full,
            "camera_id": camera_id,
            "user_id": user_id,
            "mode": mode,
        }
        if burst_mode and not is_full:
            result["burst_active"] = True
        if is_full:
            pass
        return result


# Instancia global
orchestrator = QwenOrchestrator(max_concurrent=12, timeout=45.0)
