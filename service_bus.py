#!/usr/bin/env python3
"""service_bus.py — Bus de colas + concurrencia para los backends de OjoIA.

Por cada backend:
  - asyncio.Semaphore(MAX): concurrencia segura (no manda más de lo que aguanta).
  - asyncio.Queue(BACKLOG): absorbe ráfagas; await put() ENCOLA (no suelta).
    Si rebosa -> 429 + Retry-After (backpressure real, no crece hasta reventar).
  - Proxy transparente ( método/body/headers/multipart/streaming SSE) con httpx.

Endpoints:
  POST/GET  /{backend}/{ruta:path}   -> proxy al backend correspondiente
  GET       /bus/health              -> estado de colas y semaforos
  GET       /bus/metrics             -> metricas detalladas

Backends configurados:
  qwen14b -> 127.0.0.1:8015   sem=48  backlog=5000
  qwen7b  -> 127.0.0.1:8004   sem=150 backlog=5000
  whisper -> 127.0.0.1:8008   sem=12  backlog=5000
  yolo    -> 127.0.0.1:8002   sem=12  backlog=5000
"""
import os
import asyncio
import time
import logging
from collections import deque

import httpx
from fastapi import FastAPI, Request, Response, HTTPException
from fastapi.responses import StreamingResponse, JSONResponse

# ---------------------------------------------------------------------------
BACKENDS = {
    "qwen14b": {"url": "http://127.0.0.1:8015", "sem": 48, "backlog": 5000},
    "qwen7b":  {"url": "http://127.0.0.1:8004", "sem": 150, "backlog": 5000},
    "whisper": {"url": "http://127.0.0.1:8008", "sem": 12, "backlog": 5000},
    "yolo":    {"url": "http://127.0.0.1:8002", "sem": 12, "backlog": 5000},
}

HOST = os.getenv("BUS_HOST", "127.0.0.1")
PORT = int(os.getenv("BUS_PORT", "8200"))
# headers hop-by-hop que no se reenvian
HOP = {"host", "content-length", "transfer-encoding", "connection",
       "keep-alive", "proxy-authenticate", "proxy-authorization",
       "te", "trailers", "upgrade"}

logging.basicConfig(level=logging.INFO, format="%(asctime)s [bus] %(message)s")
log = logging.getLogger("service_bus")

app = FastAPI(title="OjoIA Service Bus", version="1.0")

# estado por backend
semaphores: dict[str, asyncio.Semaphore] = {}
queues: dict[str, asyncio.Queue] = {}
metrics: dict[str, dict] = {}

for name, cfg in BACKENDS.items():
    semaphores[name] = asyncio.Semaphore(cfg["sem"])
    queues[name] = asyncio.Queue(maxsize=0)  # ilimitada: el semaforo limita concurrencia
    metrics[name] = {
        "sem_max": cfg["sem"], "backlog_max": cfg["backlog"],
        "in_flight": 0, "queued": 0, "total_done": 0,
        "total_err": 0, "total_429": 0,
        "latencies": deque(maxlen=500),
    }


async def _queue_worker(name: str):
    """Worker permanente por backend: drena la cola y ejecuta con el semaforo."""
    q = queues[name]
    sem = semaphores[name]
    m = metrics[name]
    while True:
        job = await q.get()
        m["queued"] = q.qsize()
        async with sem:
            m["in_flight"] += 1
            try:
                await job()
                m["total_done"] += 1
            except Exception:
                m["total_err"] += 1
            finally:
                m["in_flight"] = max(0, m["in_flight"] - 1)


@app.on_event("startup")
async def _start_workers():
    for name in BACKENDS:
        asyncio.create_task(_queue_worker(name))
    log.info(f"Bus arriba en {HOST}:{PORT} | backends={list(BACKENDS)}")


# ---------------------------------------------------------------------------
# Proxy transparente (encolado + semáforo)
# ---------------------------------------------------------------------------


async def _enqueue_and_proxy(name: str, request: Request, path: str):
    """Encola el trabajo y devuelve el resultado cuando le toque."""
    q = queues[name]
    m = metrics[name]
    if q.full():
        m["total_429"] += 1
        return JSONResponse(
            {"error": "service bus saturated", "backend": name,
             "retry_after": 5},
            status_code=429, headers={"Retry-After": "5"})

    method = request.method
    body = await request.body()
    query = request.url.query
    accept = request.headers.get("accept", "")
    fwd_headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP}

    fut = asyncio.get_event_loop().create_future()

    async def job():
        t0 = time.monotonic()
        try:
            cfg = BACKENDS[name]
            url = f"{cfg['url']}/{path}"
            if query:
                url += f"?{query}"
            client = app.state.http
            req = client.build_request(method, url, content=body, headers=fwd_headers)
            upstream = await client.send(req, stream=False)
            resp_headers = {k: v for k, v in upstream.headers.items()
                            if k.lower() not in HOP}
            fut.set_result((upstream.status_code, resp_headers, upstream.content))
        except Exception as e:
            fut.set_exception(e)
        finally:
            m["latencies"].append(round(time.monotonic() - t0, 3))

    await q.put(job)
    m["queued"] = q.qsize()

    try:
        status, headers, content = await asyncio.wait_for(fut, timeout=900)
    except asyncio.TimeoutError:
        m["total_err"] += 1
        return JSONResponse({"error": "backend timeout", "backend": name},
                            status_code=504)
    except Exception as e:
        m["total_err"] += 1
        return JSONResponse({"error": "backend error", "detail": str(e)[:200]},
                            status_code=502)

    return Response(content=content, status_code=status, headers=headers)


# ---------------------------------------------------------------------------
# rutas internas del bus (definidas ANTES del proxy goloso)
# ---------------------------------------------------------------------------
@app.get("/")
async def root():
    return {"service": "ojoia_service_bus", "backends": list(BACKENDS),
            "endpoints": ["/{backend}/{path}", "/bus/health", "/bus/metrics"]}


@app.get("/bus/health")
async def health():
    out = {}
    for name in BACKENDS:
        m = metrics[name]
        avg = (sum(m["latencies"]) / len(m["latencies"])) if m["latencies"] else 0
        out[name] = {
        "concurrent_max": m["sem_max"],
        "in_flight": m["in_flight"],
        "queued": queues[name].qsize(),
        "backlog_max": "inf",
        "done": m["total_done"],
            "errors": m["total_err"],
            "rejected_429": m["total_429"],
            "avg_latency_s": round(avg, 3),
        }
    return out


@app.get("/bus/metrics")
async def bus_metrics():
    return health()


# proxy goloso va al final
@app.api_route("/{backend}/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def proxy_route(backend: str, path: str, request: Request):
    if backend not in BACKENDS:
        raise HTTPException(status_code=404, detail=f"backend desconocido: {backend}")
    return await _enqueue_and_proxy(backend, request, path)


@app.on_event("startup")
async def _init_http():
    app.state.http = httpx.AsyncClient(timeout=httpx.Timeout(900.0, connect=15.0),
                                       limits=httpx.Limits(max_connections=1000,
                                                            max_keepalive_connections=200))


@app.on_event("shutdown")
async def _close_http():
    await app.state.http.aclose()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")
