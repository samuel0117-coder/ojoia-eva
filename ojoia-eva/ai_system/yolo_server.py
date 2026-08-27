#!/usr/bin/env python3
"""Servidor YOLOv8 para detección de objetos - umbral bajo para detectar más"""
from fastapi import FastAPI, UploadFile, File
from ultralytics import YOLO
from PIL import Image
import io, torch, logging, asyncio

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("yolo_server")

app = FastAPI(title="YOLO Detection API")
model = None

@app.on_event("startup")
async def load_model():
    global model
    log.info("Loading YOLOv8s model...")
    model = YOLO("yolov8s.pt")
    model.to("cpu")
    log.info(f"Model loaded successfully")

@app.post("/detect")
async def detect(image: UploadFile = File(...), confidence: float = 0.15):
    """Detectar objetos en imagen - umbral 0.15 para detectar más objetos"""
    if model is None:
        return {"detections": [], "count": 0, "error": "model not loaded"}
    
    img_bytes = await image.read()
    img = Image.open(io.BytesIO(img_bytes)).convert('RGB')
    
    results = model(img, conf=confidence, verbose=False)
    
    detections = []
    for r in results:
        for box in r.boxes:
            detections.append({
                "class": model.names[int(box.cls)],
                "confidence": float(box.conf),
                "bbox": [float(x) for x in box.xyxy[0]]
            })
    
    return {"detections": detections, "count": len(detections)}

@app.get("/health")
async def health():
    return {
        "yolo": "healthy" if model else "loading",
        "model": "yolov8s.pt",
        "loaded": model is not None,
        "device": "cpu"
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8002)
