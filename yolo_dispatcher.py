#!/usr/bin/env python3
"""Dispatcher YOLO: 1 worker interno + micro-batching.

Un solo proceso worker carga EL engine TRT UNA vez (~4G RAM en vez de
N engines x 3.87G). El dispatcher acumula los requests de camaras durante
MICRO_BATCH_MS y los envia como UN batch grande al worker, maximizando
GPU util sin perder calidad (FP16, mismo engine).

Variables de entorno:
- YOLO_WORKERS: numero de workers internos (default 1 = 1 engine, minimo RAM)
- YOLO_WORKER_BASE_PORT: puerto base (default 8100)
- PORT: puerto del dispatcher (default 8002)
- YOLO_GPU: GPU fisica (default 1)
- YOLO_MEM_FRAC: cap VRAM (default 0.13)
- YOLO_GLOBAL_CONCURRENT: inferencias batch simultaneas (default 2)
- YOLO_IMGSZ: tamano inferencia (default 416)
- MICRO_BATCH_MS: ventana de acumulacion de requests (default 15)
- MICRO_BATCH_MAX: maximo imgs por batch (default 16)
"""
import os
import time
import asyncio
import logging
import subprocess
from contextlib import asynccontextmanager

import httpx
import uvicorn
from fastapi import FastAPI, UploadFile, File
from fastapi.responses import JSONResponse

logging.basicConfig(level=logging.INFO, format="%(asctime)s [dispatcher] %(message)s")
log = logging.getLogger("yolo_dispatcher")

WORKERS = int(os.getenv("YOLO_WORKERS", "1"))
BASE_PORT = int(os.getenv("YOLO_WORKER_BASE_PORT", "8100"))
PORT = int(os.getenv("PORT", "8002"))
GPU = os.getenv("YOLO_GPU", "1")
MEM_FRAC = os.getenv("YOLO_MEM_FRAC", "0.13")
CONCURRENT = int(os.getenv("YOLO_GLOBAL_CONCURRENT", "2"))
IMGSZ = os.getenv("YOLO_IMGSZ", "416")
MODEL = os.getenv("YOLO_MODEL")  # None = auto-detectar (.engine si existe)
PERSON_CONF = os.getenv("YOLO_PERSON_CONF", "0.20")
MICRO_BATCH_MS = float(os.getenv("MICRO_BATCH_MS", "15"))
MICRO_BATCH_MAX = int(os.getenv("MICRO_BATCH_MAX", "16"))
PYTHON = os.getenv("PYTHON_BIN", "/home/sam/ai_system/venv/bin/python")
SERVER_PY = os.getenv("YOLO_SERVER_PY", "/opt/ojoia/code/yolo_server.py")

# Semaphore para limitar inferencias batch simultaneas al worker
infer_semaphore = None


def worker_env(port):
    env = os.environ.copy()
    env["CUDA_VISIBLE_DEVICES"] = GPU
    env["PORT"] = str(port)
    env["YOLO_GPU"] = GPU
    env["YOLO_MEM_FRAC"] = MEM_FRAC
    env["YOLO_GLOBAL_CONCURRENT"] = str(CONCURRENT)
    env["YOLO_IMGSZ"] = IMGSZ
    if MODEL:
        env["YOLO_MODEL"] = MODEL
    else:
        env.pop("YOLO_MODEL", None)
    env["YOLO_PERSON_CONF"] = PERSON_CONF
    return env


workers = []
http_client = None


def start_worker(port):
    env = worker_env(port)
    p = subprocess.Popen(
        [PYTHON, "-u", SERVER_PY],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        cwd=os.path.dirname(SERVER_PY),
    )
    return p


def stop_workers():
    for p, port in workers:
        try:
            p.terminate()
            p.wait(timeout=5)
        except Exception as e:
            log.warning(f"worker {port} no termino limpio: {e}")
            try:
                p.kill()
            except Exception:
                pass
    log.info("todos los workers detenidos")


async def wait_workers_ready(timeout=120):
    """Arranca y espera cada worker SECUENCIALMENTE (carga escalonada TRT)."""
    global workers
    t0 = time.time()
    for i in range(WORKERS):
        port = BASE_PORT + i
        p = start_worker(port)
        workers.append((p, port))
        log.info(f"worker {i+1}/{WORKERS} pid={p.pid} port={port} (cargando...)")
        ready = False
        while time.time() - t0 < timeout:
            try:
                async with http_client.stream("GET", f"http://127.0.0.1:{port}/health", timeout=2) as r:
                    if r.status_code == 200:
                        ready = True
                        break
            except Exception:
                pass
            await asyncio.sleep(0.3)
        if not ready:
            raise RuntimeError(f"worker {port} no respondio en {timeout}s")
        log.info(f"worker {port} listo")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global workers, http_client, infer_semaphore
    log.info(f"iniciando dispatcher puerto {PORT}: {WORKERS} worker(s), "
             f"micro-batch {MICRO_BATCH_MS}ms max {MICRO_BATCH_MAX}, conc={CONCURRENT}")
    workers = []
    infer_semaphore = asyncio.Semaphore(CONCURRENT)
    http_client = httpx.AsyncClient(timeout=300.0, limits=httpx.Limits(max_connections=256))
    try:
        await wait_workers_ready()
    except Exception as e:
        log.error(f"fallo arranque workers: {e}")
        stop_workers()
        raise
    yield
    await http_client.aclose()
    stop_workers()


app = FastAPI(title="YOLO Dispatcher", version="2.2", lifespan=lifespan)

# Cola de micro-batching: acumula (future, files, params) y los vacia en batch
_batch_queue = []
_batch_lock = asyncio.Lock()
_batch_flush_task = None


async def _flush_batch():
    """Toma todos los items acumulados y los envia como 1 batch al worker."""
    async with _batch_lock:
        if not _batch_queue:
            return
        items = _batch_queue[:MICRO_BATCH_MAX]
        del _batch_queue[:len(items)]
    if not items:
        return

    # Juntar todos los archivos en 1 request /detect_batch
    files = []
    cam_ids = []
    for it in items:
        for fname, data, ctype in it["files"]:
            files.append(("images", (fname, data, ctype)))
        cam_ids.append(it["camera_id"])

    params = {"confidence": items[0]["confidence"], "camera_ids": ",".join(cam_ids)}
    try:
        async with infer_semaphore:
            r = await http_client.post(
                f"http://127.0.0.1:{BASE_PORT}/detect_batch",
                files=files, params=params, timeout=120,
            )
            data = r.json()
        # Distribuir resultados por camara en el mismo orden
        results = data.get("results", [])
        for it, res in zip(items, results):
            res_payload = {
                "detections": res.get("detections", []),
                "all_detections": res.get("all_detections", []),
                "raw_detections": res.get("raw_detections", []),
                "count": res.get("count", 0),
                "stable_count": res.get("stable_count", 0),
                "camera_id": it["camera_id"],
            }
            it["future"].set_result((200, res_payload))
    except Exception as e:
        for it in items:
            it["future"].set_result((500, {"error": str(e)}))
    finally:
        # Programar proximo flush si queda cola
        if _batch_queue:
            global _batch_flush_task
            _batch_flush_task = asyncio.create_task(_schedule_flush())


async def _schedule_flush():
    await asyncio.sleep(MICRO_BATCH_MS / 1000.0)
    await _flush_batch()


async def enqueue_request(files, camera_id, confidence):
    """Encola un request y espera su resultado (micro-batching)."""
    loop = asyncio.get_event_loop()
    fut = loop.create_future()
    item = {
        "future": fut,
        "files": files,
        "camera_id": camera_id,
        "confidence": confidence,
    }
    async with _batch_lock:
        _batch_queue.append(item)
        should_schedule = (len(_batch_queue) == 1)
        full = (len(_batch_queue) >= MICRO_BATCH_MAX)
    if full:
        await _flush_batch()
    elif should_schedule:
        global _batch_flush_task
        _batch_flush_task = asyncio.create_task(_schedule_flush())
    status, payload = await fut
    return status, payload


@app.get("/health")
async def health():
    # B6-fix: antes el gather de WORKERS health-checks lanzaba excepcion si
    # CUALQUIERA tardaba >2s o fallaba -> 503 falso (el detect seguia
    # funcionando). Ahora: chequeo por-worker con timeout generoso (5s) y
    # gather tolerante (return_exceptions=True); reportamos ok si AL MENOS
    # un worker responde 200. Asi el health-monitor no marca el servicio
    # caido por un health-check transitorio del worker TRT (que tarda en
    # cargar ~10s tras restart).
    try:
        results = await asyncio.gather(*[
            http_client.get(f"http://127.0.0.1:{BASE_PORT + i}/health", timeout=5)
            for i in range(WORKERS)
        ], return_exceptions=True)
        alive = sum(1 for r in results if getattr(r, "status_code", 0) == 200)
        if alive == 0:
            return JSONResponse(
                {"status": "error", "detail": "no workers responding",
                 "workers": WORKERS, "alive": 0},
                status_code=503,
            )
        return {"status": "ok", "workers": WORKERS, "alive": alive,
                "micro_batch_ms": MICRO_BATCH_MS, "micro_batch_max": MICRO_BATCH_MAX}
    except Exception as e:
        return JSONResponse({"status": "error", "detail": str(e)}, status_code=503)


@app.post("/detect")
async def detect(
    image: UploadFile = File(...),
    confidence: float = 0.25,
    camera_id: str = "default"
):
    data = await image.read()
    files = [(image.filename or "img.jpg", data, image.content_type or "image/jpeg")]
    status, payload = await enqueue_request(files, camera_id, confidence)
    return JSONResponse(content=payload, status_code=status)


@app.post("/detect_batch")
async def detect_batch(
    images: list[UploadFile] = File(...),
    confidence: float = 0.25,
    camera_ids: str = ""
):
    # Si llega un batch grande directo, lo dividimos en camaras individuales
    # y los micro-batcheamos (para aprovechar ventana de acumulacion).
    cam_ids = [c.strip() for c in camera_ids.split(",")] if camera_ids else []
    tasks = []
    for idx, img in enumerate(images):
        data = await img.read()
        cid = cam_ids[idx] if idx < len(cam_ids) else f"cam{idx}"
        files = [(img.filename or f"img{idx}.jpg", data, img.content_type or "image/jpeg")]
        tasks.append(enqueue_request(files, cid, confidence))
    results = await asyncio.gather(*tasks)
    # Reensamblar como respuesta batch
    out_results = []
    total_count = 0
    total_stable = 0
    for status, payload in results:
        if status == 200:
            out_results.append({
                "camera_id": payload.get("camera_id"),
                "detections": payload.get("detections", []),
                "all_detections": payload.get("all_detections", []),
                "raw_detections": payload.get("raw_detections", []),
                "count": payload.get("count", 0),
                "stable_count": payload.get("stable_count", 0),
            })
            total_count += payload.get("count", 0)
            total_stable += payload.get("stable_count", 0)
    return JSONResponse(content={
        "results": out_results,
        "batch_size": len(out_results),
        "total_count": total_count,
        "total_stable_count": total_stable,
        "model": "micro-batched",
        "pose_model": True,
        "tracker": "simple_iou",
    })


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=PORT)
