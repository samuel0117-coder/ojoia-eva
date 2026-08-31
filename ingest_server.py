#!/usr/bin/env python3
"""ingest_server.py — C3: servicio de ingest separado de la API Eva.

Un proceso FastAPI mínimo que SOLO atiende /ingest/* y /frames/ingest:
- Auth por cámara (X-Camera-Key, A4) y rate limit (C4)
- Guarda el frame, corre YOLO (con micro-batch C6), encola al bus Redis (C2)
- NO carga eva_v2, ni scheduler de reportes, ni chat — si el chat/API crashea,
  las cámaras siguen entrando y encolando.

La API principal (api-eva, puerto 8005) sigue aceptando /ingest también por
compatibilidad; en producción Cloudflare puede enrutar /ingest/* → :8006.

Los workers YOLO/Qwen siguen en api-eva (consumen de Redis Streams). Este
servicio NO arranca workers para no duplicar consumers.

Run: python3 ingest_server.py  (puerto 8013)
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import uvicorn
from fastapi import FastAPI, Request, UploadFile, File, Form
from typing import Optional

# Reutiliza _process_ingest y helpers de api_eva SIN arrancar su app ni workers.
import api_eva

app = FastAPI(title="OjoIA Ingest", version="1.0")


@app.on_event("startup")
async def _startup():
    # Conectar solo el bus (Redis). NO workers, NO scheduler.
    await api_eva.frame_bus.start()
    api_eva.logger.info(f"🚌 ingest_server listo, bus={api_eva.frame_bus.mode}")


@app.on_event("shutdown")
async def _shutdown():
    await api_eva.frame_bus.close()


@app.get("/health")
async def health():
    st = await api_eva.frame_bus.stats()
    return {"status": "ok", "service": "ingest", "frame_queue": st}


@app.post("/ingest/frame")
@app.post("/frames/ingest")
async def ingest_frame(request: Request, camera_id: str = Form(None),
                       user_id: str = Form(None), image: UploadFile = File(...)):
    return await api_eva._process_ingest(request, camera_id, user_id, image)


@app.post("/ingest/photo")
@app.post("/ingest/snapshot")
async def ingest_photo(request: Request, filename: str = Form(None),
                       camera_id: str = Form(None), user_id: str = Form(None),
                       image: UploadFile = File(...)):
    return await api_eva._process_ingest(request, camera_id, user_id, image)


@app.post("/ingest/raw")
async def ingest_raw(request: Request):
    # Compat: api_eva.ingest_raw lee el body crudo y arma UploadFile.
    return await api_eva.ingest_raw(request)


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8013, log_level="warning")
