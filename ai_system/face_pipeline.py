import numpy as np
import cv2
import json
import time
import logging
from pathlib import Path
from typing import Optional, Dict, List, Any

logger = logging.getLogger(__name__)

STORAGE_ROOT = Path("/home/sam/storage")
FACES_DIR = STORAGE_ROOT / "identity" / "faces"

_model = None
_app = None


def _load_model():
    global _model, _app
    if _model is not None:
        return _model, _app
    try:
        from insightface.app import FaceAnalysis
        _app = FaceAnalysis(name='buffalo_l', providers=['CUDAExecutionProvider', 'CPUExecutionProvider'])
        _app.prepare(ctx_id=0, det_size=(640, 640))
        _model = _app
        logger.info("InsightFace buffalo_l loaded")
        return _model, _app
    except Exception as e:
        logger.error(f"InsightFace load failed: {e}")
        return None, None


def extract_face_embedding(image_path: str) -> Optional[np.ndarray]:
    model, app = _load_model()
    if model is None:
        return None
    img = cv2.imread(image_path)
    if img is None:
        return None
    faces = model.get(img)
    if not faces:
        return None
    best = max(faces, key=lambda f: (f.bbox[2] - f.bbox[0]) * (f.bbox[3] - f.bbox[1]))
    return best.embedding


def extract_face_from_frame(frame_bytes: bytes) -> Optional[np.ndarray]:
    model, app = _load_model()
    if model is None:
        return None
    try:
        arr = np.frombuffer(frame_bytes, dtype=np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if img is None:
            return None
        faces = model.get(img)
        if not faces:
            return None
        best = max(faces, key=lambda f: (f.bbox[2] - f.bbox[0]) * (f.bbox[3] - f.bbox[1]))
        return best.embedding
    except Exception as e:
        logger.error(f"Frame face extraction error: {e}")
        return None


def crop_face_from_frame(frame_bytes: bytes, output_path: str) -> bool:
    model, app = _load_model()
    if model is None:
        return False
    try:
        arr = np.frombuffer(frame_bytes, dtype=np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if img is None:
            return False
        faces = model.get(img)
        if not faces:
            return False
        best = max(faces, key=lambda f: (f.bbox[2] - f.bbox[0]) * (f.bbox[3] - f.bbox[1]))
        x1, y1, x2, y2 = map(int, best.bbox)
        h, w = img.shape[:2]
        x1, y1 = max(0, x1), max(0, y1)
        x2, y2 = min(w, x2), min(h, y2)
        crop = img[y1:y2, x1:x2]
        if crop.size == 0:
            return False
        cv2.imwrite(output_path, crop)
        return True
    except Exception as e:
        logger.error(f"Face crop error: {e}")
        return False


def compare_embeddings(emb1: np.ndarray, emb2: np.ndarray) -> float:
    emb1_n = emb1 / (np.linalg.norm(emb1) + 1e-8)
    emb2_n = emb2 / (np.linalg.norm(emb2) + 1e-8)
    sim = np.dot(emb1_n, emb2_n)
    return float(max(0.0, min(1.0, (sim + 1) / 2)))


def identify_face(embedding: np.ndarray, user_id: str, threshold: float = 0.45) -> List[Dict[str, Any]]:
    results = []
    if not FACES_DIR.exists():
        return results
    for person_dir in FACES_DIR.iterdir():
        if not person_dir.is_dir():
            continue
        meta_path = person_dir / "meta.json"
        emb_path = person_dir / "face_embed.npy"
        if not meta_path.exists() or not emb_path.exists():
            continue
        try:
            meta = json.loads(meta_path.read_text())
        except Exception:
            continue
        if meta.get("user_id") != user_id:
            continue
        try:
            stored_emb = np.load(str(emb_path))
            score = compare_embeddings(embedding, stored_emb)
            if score >= threshold:
                results.append({
                    "person_id": meta.get("person_id", person_dir.name),
                    "person_name": meta.get("person_name", "desconocido"),
                    "confidence": round(score, 3),
                    "known": True,
                })
        except Exception as e:
            logger.error(f"Compare error with {person_dir.name}: {e}")
    results.sort(key=lambda x: x["confidence"], reverse=True)
    return results


def register_face(user_id: str, person_name: str, image_path: str) -> Optional[dict]:
    FACES_DIR.mkdir(parents=True, exist_ok=True)
    person_id = f"{person_name.lower().replace(' ', '_')}_{np.random.randint(10000, 99999)}"
    person_dir = FACES_DIR / person_id
    person_dir.mkdir(parents=True, exist_ok=True)
    embedding = extract_face_embedding(image_path)
    if embedding is None:
        return None
    emb_path = person_dir / "face_embed.npy"
    np.save(str(emb_path), embedding)
    meta = {
        "person_id": person_id,
        "person_name": person_name,
        "user_id": user_id,
        "registered_at": int(time.time()),
        "embedding_path": str(emb_path),
    }
    meta_path = person_dir / "meta.json"
    with open(meta_path, "w") as f:
        json.dump(meta, f, indent=2, ensure_ascii=False)
    img_dest = person_dir / "face_registered.jpg"
    import shutil
    shutil.copy2(image_path, str(img_dest))
    logger.info(f"Registered face: {person_name} ({person_id}) for user {user_id}")
    return meta


def list_employees(user_id: str) -> List[Dict[str, Any]]:
    employees = []
    if not FACES_DIR.exists():
        return employees
    for person_dir in FACES_DIR.iterdir():
        if not person_dir.is_dir():
            continue
        meta_path = person_dir / "meta.json"
        if not meta_path.exists():
            continue
        try:
            meta = json.loads(meta_path.read_text())
            if meta.get("user_id") == user_id:
                employees.append({
                    "person_id": meta.get("person_id"),
                    "person_name": meta.get("person_name"),
                    "registered_at": meta.get("registered_at"),
                })
        except Exception:
            continue
    return employees


def identify_from_frame(frame_bytes: bytes, user_id: str, threshold: float = 0.45) -> List[Dict[str, Any]]:
    embedding = extract_face_from_frame(frame_bytes)
    if embedding is None:
        return []
    return identify_face(embedding, user_id, threshold)
