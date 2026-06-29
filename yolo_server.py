#!/usr/bin/env python3
"""Servidor YOLO-pose para detección de personas con tracker simple."""
from fastapi import FastAPI, UploadFile, File
from ultralytics import YOLO
from PIL import Image
import io
import math
import time
import torch
import logging
import asyncio
import os
os.environ["CUDA_VISIBLE_DEVICES"] = "1"

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("yolo_server")

app = FastAPI(title="YOLO Pose Detection API")
model = None
yolo_lock = asyncio.Semaphore(1)
trackers = {}


class SimpleTracker:
    def __init__(self, max_misses=3, ttl=10.0, iou_threshold=0.20):
        self.tracks = []
        self.next_id = 1
        self.max_misses = max_misses
        self.ttl = ttl
        self.iou_threshold = iou_threshold

    @staticmethod
    def iou(a, b):
        ax1, ay1, ax2, ay2 = a
        bx1, by1, bx2, by2 = b
        ix1, iy1 = max(ax1, bx1), max(ay1, by1)
        ix2, iy2 = min(ax2, bx2), min(ay2, by2)
        iw, ih = max(0.0, ix2 - ix1), max(0.0, iy2 - iy1)
        inter = iw * ih
        area_a = max(0.0, ax2 - ax1) * max(0.0, ay2 - ay1)
        area_b = max(0.0, bx2 - bx1) * max(0.0, by2 - by1)
        union = area_a + area_b - inter
        return inter / union if union > 0 else 0.0

    def update(self, detections, camera_id="default", now=None):
        now = now or time.time()
        active = []
        for tr in self.tracks:
            if now - tr["last_seen"] > self.ttl:
                continue
            tr["misses"] += 1
            if tr["misses"] <= self.max_misses:
                active.append(tr)
        self.tracks = active

        matched = set()
        output = []
        for det in detections:
            bbox = det.get("bbox") or []
            if len(bbox) != 4:
                continue
            best_idx = -1
            best_score = -1.0
            for idx, tr in enumerate(self.tracks):
                if idx in matched:
                    continue
                score = self.iou(bbox, tr["bbox"])
                if score > best_score:
                    best_score = score
                    best_idx = idx
            if best_idx >= 0 and best_score >= self.iou_threshold:
                tr = self.tracks[best_idx]
                matched.add(best_idx)
                tr["misses"] = 0
                tr["last_seen"] = now
                tr["hits"] += 1
                tr["last_bbox"] = list(bbox)
                alpha = float(os.getenv("TRACK_SMOOTHING", "0.35"))
                tr["bbox"] = [
                    tr["bbox"][0] * (1 - alpha) + bbox[0] * alpha,
                    tr["bbox"][1] * (1 - alpha) + bbox[1] * alpha,
                    tr["bbox"][2] * (1 - alpha) + bbox[2] * alpha,
                    tr["bbox"][3] * (1 - alpha) + bbox[3] * alpha,
                ]
                det["track_id"] = tr["id"]
                det["track_hits"] = tr["hits"]
                det["track_stable"] = tr["hits"] >= 2
                output.append(det)
            else:
                new_id = self.next_id
                self.next_id += 1
                self.tracks.append({
                    "id": new_id,
                    "bbox": list(bbox),
                    "last_bbox": list(bbox),
                    "center": [(bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2],
                    "hits": 1,
                    "misses": 0,
                    "last_seen": now,
                })
                det["track_id"] = new_id
                det["track_hits"] = 1
                det["track_stable"] = False
                output.append(det)
        return output


def _tensor_list(value):
    try:
        if value is None:
            return []
        if hasattr(value, "detach"):
            value = value.detach().cpu()
        return value.tolist()
    except Exception:
        return []


def _bbox_area(bbox):
    if not bbox or len(bbox) != 4:
        return 0
    return max(0.0, bbox[2] - bbox[0]) * max(0.0, bbox[3] - bbox[1])


def _keypoint_visible(kp, bbox):
    if not kp or len(kp) < 2:
        return False
    x, y = float(kp[0]), float(kp[1])
    x1, y1, x2, y2 = bbox
    pad = max(8.0, min(x2 - x1, y2 - y1) * 0.25)
    return x1 - pad <= x <= x2 + pad and y1 - pad <= y <= y2 + pad


def _pose_metrics(keypoints, bbox):
    if not keypoints:
        return {"score": 0.0, "visible": 0, "vertical_span": 0.0, "shoulders": 0.0, "hips": 0.0, "has_pose": False}
    kps = keypoints[:17]
    visible = [kp for kp in kps if _keypoint_visible(kp, bbox)]
    if not visible:
        return {"score": 0.0, "visible": 0, "vertical_span": 0.0, "shoulders": 0.0, "hips": 0.0, "has_pose": False}
    xs = [float(kp[0]) for kp in visible]
    ys = [float(kp[1]) for kp in visible]
    x1, y1, x2, y2 = bbox
    bw, bh = max(1.0, x2 - x1), max(1.0, y2 - y1)
    vertical_span = (max(ys) - min(ys)) / bh if bh else 0.0

    def dist(a, b):
        if not a or not b:
            return 0.0
        return math.hypot(float(a[0]) - float(b[0]), float(a[1]) - float(b[1]))

    shoulders = dist(kps[5] if len(kps) > 5 else None, kps[6] if len(kps) > 6 else None) / bw if bw else 0.0
    hips = dist(kps[11] if len(kps) > 11 else None, kps[12] if len(kps) > 12 else None) / bw if bw else 0.0
    upper_count = sum(1 for idx in (0, 5, 6, 7, 8) if len(kps) > idx and _keypoint_visible(kps[idx], bbox))
    lower_count = sum(1 for idx in (11, 12, 13, 14, 15, 16) if len(kps) > idx and _keypoint_visible(kps[idx], bbox))
    score = min(1.0, (len(visible) / 8.0) * 0.55 + (vertical_span * 0.25) + (shoulders * 0.10) + (hips * 0.10))
    if upper_count >= 3:
        score = min(1.0, score + 0.10)
    if lower_count >= 3:
        score = min(1.0, score + 0.08)
    return {
        "score": round(score, 3),
        "visible": len(visible),
        "vertical_span": round(vertical_span, 3),
        "shoulders": round(shoulders, 3),
        "hips": round(hips, 3),
        "has_pose": len(visible) >= 4 or (upper_count >= 3 and vertical_span >= 0.35),
    }


def _valid_person(det):
    cls = str(det.get("class", "")).lower()
    if cls != "person":
        return True
    conf = float(det.get("confidence", 0.0))
    pose = det.get("pose") or {}
    min_pose = float(os.getenv("YOLO_MIN_POSE_SCORE", "0.35"))
    min_conf = float(os.getenv("YOLO_PERSON_CONF", "0.35"))
    if conf < min_conf:
        return False
    if pose.get("has_pose") and pose.get("score", 0) >= min_pose:
        return True
    if pose.get("visible", 0) >= 6 and pose.get("vertical_span", 0) >= 0.35:
        return True
    return False


def _get_tracker(camera_id):
    tracker = trackers.get(camera_id)
    if not tracker:
        tracker = SimpleTracker()
        trackers[camera_id] = tracker
    return tracker


@app.on_event("startup")
async def load_model():
    global model
    model_name = os.getenv("YOLO_MODEL", "yolov8s-pose.pt")
    log.info(f"Loading {model_name} model for CUDA (GPU 1)...")
    torch.set_num_threads(2)
    model = YOLO(model_name, verbose=False)
    model.to("cuda")
    model.eval()
    log.info(f"Model loaded successfully on CUDA (GPU 1)")


@app.post("/detect")
async def detect(image: UploadFile = File(...), confidence: float = 0.25, camera_id: str = "default"):
    if model is None:
        return {"detections": [], "count": 0, "error": "model not loaded"}

    img_bytes = await image.read()
    img = Image.open(io.BytesIO(img_bytes)).convert('RGB')
    imgsz = int(os.getenv("YOLO_IMGSZ", "416"))
    effective_conf = min(float(confidence), float(os.getenv("YOLO_PERSON_CONF", "0.35")))

    async with yolo_lock:
        results = model(img, imgsz=imgsz, conf=effective_conf, verbose=False)

    raw_detections = []
    for r in results:
        boxes = r.boxes
        keypoints = getattr(r, "keypoints", None)
        kp_list = _tensor_list(getattr(keypoints, "xy", None) if keypoints is not None else None)
        for idx, box in enumerate(boxes):
            cls_id = int(box.cls)
            cls = str(model.names.get(cls_id, model.names[cls_id] if hasattr(model.names, "__getitem__") else cls_id))
            bbox = [float(x) for x in box.xyxy[0]]
            conf_val = float(box.conf[0])
            kps = kp_list[idx] if idx < len(kp_list) else []
            pose = _pose_metrics(kps, bbox)
            raw_detections.append({
                "class": cls,
                "confidence": round(conf_val, 3),
                "bbox": [round(x, 2) for x in bbox],
                "keypoints": kps,
                "pose": pose,
            })

    filtered = [d for d in raw_detections if _valid_person(d)]
    tracked = _get_tracker(camera_id).update(filtered, camera_id=camera_id)
    person_detections = [d for d in tracked if str(d.get("class", "")).lower() == "person"]
    stable_persons = []
    for d in person_detections:
        pose_score = (d.get("pose") or {}).get("score", 0.0)
        conf = d.get("confidence", 0.0)
        if d.get("track_stable") or conf >= 0.70 or (pose_score >= 0.65 and conf >= 0.55):
            stable_persons.append(d)
    return {
        "detections": person_detections,  # Todas las personas (no solo estables)
        "all_detections": tracked,
        "raw_detections": raw_detections,
        "count": len(person_detections),  # Count real de personas detectadas
        "stable_count": len(stable_persons),
        "model": os.getenv("YOLO_MODEL", "yolov8s-pose.pt"),
        "pose_model": True,
        "tracker": "simple_iou",
        "effective_confidence": effective_conf,
    }


@app.get("/health")
async def health():
    return {
        "yolo": "healthy" if model else "loading",
        "model": os.getenv("YOLO_MODEL", "yolov8s-pose.pt"),
        "loaded": model is not None,
        "device": "cuda",
        "threads": torch.get_num_threads(),
        "imgsz": int(os.getenv("YOLO_IMGSZ", "416")),
        "default_confidence": 0.25,
        "person_confidence": float(os.getenv("YOLO_PERSON_CONF", "0.35")),
        "pose_model": True,
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8002)
