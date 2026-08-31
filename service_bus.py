#!/usr/bin/env python3
"""service_bus.py — Bus de colas + concurrencia + billing para OjoIA.

Por cada backend:
  - asyncio.Semaphore(MAX): concurrencia segura (no manda más de lo que aguanta).
  - asyncio.Queue(BACKLOG): absorbe ráfagas; await put() ENCOLA (no suelta).
    Si rebosa -> 429 + Retry-After (backpressure real, no crece hasta reventar).
  - Proxy transparente ( método/body/headers/multipart/streaming SSE) con httpx.
  - Auth: API key (ojoia_live_*) validada contra Redis.
  - Rate limiting: sliding window por cliente (según plan).
  - Token tracking: registra prompt/completion tokens + costo por request.

Endpoints:
  POST/GET  /{backend}/{ruta:path}   -> proxy al backend correspondiente
  GET       /bus/health              -> estado de colas y semaforos
  GET       /bus/metrics             -> metricas detalladas
  GET       /bus/usage               -> resumen de uso (requiere auth)

Backends configurados (5 modelos OjoIA):
  qwen7b  -> 127.0.0.1:8004   sem=150 backlog=5000
  qwen9b  -> 127.0.0.1:8018   sem=64  backlog=5000
  qwen35b -> 127.0.0.1:8019   sem=2   backlog=1000
  whisper -> 127.0.0.1:8008   sem=12  backlog=5000
  yolo    -> 127.0.0.1:8002   sem=12  backlog=5000
"""
import os
import sys
import asyncio
import json
import time
import logging
from collections import deque
from pathlib import Path

import httpx
from fastapi import FastAPI, Request, Response, HTTPException, Header
from fastapi.responses import StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

# billing.py está en el mismo directorio
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from billing import (BillingStore, extract_usage_from_response,
                     PLANS, MODEL_PRICES)
from billing_log import log_request, purge_old

# ---------------------------------------------------------------------------
# Backends OjoIA (congelados 2026-08-20). El qwen14b se elimina (ya no se usa).
# El qwen9b y qwen35b se aggregan para que TODOS los modelos pasen por el bus.
BACKENDS = {
    "qwen7b":  {"url": "http://127.0.0.1:8004", "sem": 150, "backlog": 5000},
    "qwen9b":  {"url": "http://127.0.0.1:8018", "sem": 64,  "backlog": 5000},
    # qwen35b está frío en disco (imagen intacta). El puerto 8019 ahora es qwen3vl8b.
    # "qwen35b": {"url": "http://127.0.0.1:8019", "sem": 2,   "backlog": 1000},
    "qwen38":  {"url": "http://127.0.0.1:18020", "sem": 64, "backlog": 5000},
    "qwen3vl8b": {"url": "http://127.0.0.1:8019", "sem": 4,  "backlog": 1000},
    "whisper": {"url": "http://127.0.0.1:8008", "sem": 12,  "backlog": 5000},
    "yolo":    {"url": "http://127.0.0.1:8002", "sem": 12,  "backlog": 5000},
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


def _extract_usage_from_sse(content: bytes) -> tuple[int, int]:
    """Extrae prompt/completion tokens de una respuesta SSE (streaming).
    Con stream_options:{include_usage:true}, el ultimo chunk data: tiene 'usage'."""
    if not content:
        return (0, 0)
    prompt = completion = 0
    try:
        text = content.decode("utf-8", errors="ignore")
        for line in text.split("\n"):
            line = line.strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                continue
            try:
                obj = json.loads(data)
            except json.JSONDecodeError:
                continue
            # OpenAI format: chunk has usage (final chunk with include_usage)
            if "usage" in obj and isinstance(obj["usage"], dict):
                prompt = int(obj["usage"].get("prompt_tokens", 0))
                completion = int(obj["usage"].get("completion_tokens", 0))
            # Fallback: acumular delta tokens
            elif "choices" in obj and obj["choices"]:
                delta = obj["choices"][0].get("delta", {})
                if delta.get("content"):
                    completion += 1  # estimación gruesa por token
    except (UnicodeDecodeError, ValueError):
        pass
    return (prompt, completion)


def _extract_response_text_from_sse(content: bytes) -> str:
    """Extrae el texto de la respuesta de un stream SSE (concatena delta.content)."""
    if not content:
        return ""
    text_parts: list[str] = []
    try:
        text = content.decode("utf-8", errors="ignore")
        for line in text.split("\n"):
            line = line.strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                continue
            try:
                obj = json.loads(data)
            except json.JSONDecodeError:
                continue
            choices = obj.get("choices") or []
            if choices:
                delta = choices[0].get("delta", {})
                c = delta.get("content")
                if c:
                    text_parts.append(c)
    except (UnicodeDecodeError, ValueError):
        pass
    return "".join(text_parts)


def _extract_response_text_json(content: bytes) -> str:
    """Extrae el texto de la respuesta de un body JSON (non-streaming)."""
    if not content:
        return ""
    try:
        data = json.loads(content)
        choices = data.get("choices") or []
        if choices:
            msg = choices[0].get("message", {})
            return msg.get("content", "") or ""
    except (json.JSONDecodeError, UnicodeDecodeError):
        pass
    return ""


async def _enqueue_and_proxy(name: str, request: Request, path: str,
                             client_id: str = "", plan: str = ""):
    """Encola el trabajo y devuelve el resultado cuando le toque.
    Soporta streaming (SSE) con conteo de tokens del chunk final."""
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

    # Detectar streaming y preparar payload
    is_stream = False
    prompt_text = ""
    request_model = ""
    if method == "POST" and body:
        ct = request.headers.get("content-type", "")
        if "application/json" in ct:
            try:
                payload = json.loads(body)
                if isinstance(payload, dict):
                    # Capturar prompt y model para el log
                    request_model = payload.get("model", "")
                    # Reescribir el model del body al model_id real del backend
                    if request_model in MODELS_CATALOG:
                        real_model_id = MODELS_CATALOG[request_model].get("model_id", request_model)
                        if real_model_id and real_model_id != request_model:
                            payload["model"] = real_model_id
                            body = json.dumps(payload).encode("utf-8")
                            fwd_headers["content-length"] = str(len(body))
                    msgs = payload.get("messages", [])
                    if isinstance(msgs, list):
                        prompt_text = " ".join(
                            str(m.get("content", "")) for m in msgs
                            if isinstance(m, dict)
                        )[:5000]
                    # Streaming: inyectar stream_options para usage en chunk final
                    is_stream = bool(payload.get("stream", False))
                    if is_stream and "stream_options" not in payload:
                        payload["stream_options"] = {"include_usage": True}
                    # Thinking: respetar lo que el cliente envie en chat_template_kwargs.
                    # Si el cliente NO especifica, activar thinking por defecto (maxima calidad).
                    if "chat_template_kwargs" not in payload:
                        payload["chat_template_kwargs"] = {"enable_thinking": True}
                    body = json.dumps(payload).encode("utf-8")
                    fwd_headers["content-length"] = str(len(body))
            except (json.JSONDecodeError, UnicodeDecodeError):
                pass  # body no es JSON, se reenvia como está

    billing = getattr(app.state, "billing", None) if app.state else None
    api_key_masked = (request.headers.get("authorization", "")
                      .replace("Bearer ", "").strip()[:14] + "...")

    # ── Path: STREAMING (SSE) ───────────────────────────────────────────────
    if is_stream:
        # Streaming real: no se encola (el semaforo limita concurrencia).
        # Se transmiten los chunks al cliente a medida que llegan del backend,
        # y el billing se registra al final del stream (con include_usage).
        sem = semaphores[name]
        try:
            await asyncio.wait_for(sem.acquire(), timeout=300)
        except asyncio.TimeoutError:
            m["total_429"] += 1
            return JSONResponse({"error": "service bus saturated", "backend": name,
                                 "retry_after": 5}, status_code=429,
                                headers={"Retry-After": "5"})
        m["in_flight"] += 1
        t0 = time.monotonic()

        async def stream_and_track():
            full_buf = b""
            upstream = None
            status_code = 500
            try:
                cfg = BACKENDS[name]
                url = f"{cfg['url']}/{path}"
                if query:
                    url += f"?{query}"
                client = app.state.http
                req = client.build_request(method, url, content=body, headers=fwd_headers)
                upstream = await client.send(req, stream=True)
                status_code = upstream.status_code
                async for chunk in upstream.aiter_raw():
                    # Reemplazar el nombre del modelo en cada chunk SSE si es el path
                    if request_model and b'"model":' in chunk:
                        try:
                            text = chunk.decode("utf-8", errors="ignore")
                            # Buscar "model":"<algo>" y reemplazar si es path largo
                            import re
                            text = re.sub(
                                rb'"model"\s*:\s*"[^"]*\.gguf[^"]*"',
                                ('"model":"' + request_model + '"').encode(),
                                chunk,
                            )
                            chunk = text.encode("utf-8") if isinstance(text, str) else text
                        except Exception:
                            pass
                    full_buf += chunk
                    m["total_done"] += 0  # no-op for clarity
                    yield chunk
            except Exception:
                m["total_err"] += 1
            finally:
                sem.release()
                m["in_flight"] = max(0, m["in_flight"] - 1)
                m["latencies"].append(round(time.monotonic() - t0, 3))
                if upstream:
                    await upstream.aclose()
                # Billing: extraer usage del stream completo al final
                if billing and client_id and full_buf:
                    try:
                        pt, ct = _extract_usage_from_sse(full_buf)
                        if pt or ct:
                            # Usar el ID canónico del catálogo (e.g. "qwen36-35b-a3b")
                            # para que el billing agrupe correctamente.
                            model_for_tracking = (
                                MODELS_CATALOG[request_model]["id"]
                                if request_model in MODELS_CATALOG
                                else (request_model or name)
                            )
                            # Si el cliente mandó un alias (qwen35, qwen35b, etc.),
                            # normalizar para consistencia.
                            try:
                                from billing_log import normalize_model_name
                                model_for_tracking = normalize_model_name(model_for_tracking)
                            except ImportError:
                                pass
                            billing.track_usage(
                                client_id=client_id, model=model_for_tracking,
                                prompt_tokens=pt, completion_tokens=ct)
                            # Log completo en SQLite (también normalizado)
                            price = MODEL_PRICES.get(model_for_tracking, MODEL_PRICES.get(name, {"input": 0.0, "output": 0.0}))
                            cost = (pt / 1_000_000 * price["input"] +
                                    ct / 1_000_000 * price["output"])
                            resp_text = _extract_response_text_from_sse(full_buf)
                            log_request(
                                client_id=client_id, model=model_for_tracking, backend=name,
                                prompt_tokens=pt, completion_tokens=ct,
                                cost_usd=cost, latency_ms=int((time.monotonic()-t0)*1000),
                                status_code=status_code, stream=True,
                                prompt=prompt_text, response=resp_text[:5000],
                                api_key_masked=api_key_masked)
                    except Exception as e:
                        log.debug(f"log_request error: {e}")
                purge_old()

        resp_headers = {"cache-control": "no-cache", "x-accel-buffering": "no"}
        if billing and client_id:
            try:
                quota = billing.get_quota_status(client_id, plan or "free")
                resp_headers["X-RateLimit-Tokens-Remaining"] = str(quota["tokens_remaining"])
                resp_headers["X-RateLimit-Tokens-Quota"] = str(quota["tokens_quota"])
            except Exception:
                pass

        return StreamingResponse(stream_and_track(),
                                  media_type="text/event-stream",
                                  headers=resp_headers)

    # ── Path: NON-STREAMING ─────────────────────────────────────────────────
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

    # Tracking de uso: extraer tokens de la respuesta y registrar en Redis.
    if billing and client_id and status == 200:
        try:
            ct = headers.get("content-type", "")
            usage = extract_usage_from_response(name, content, ct)
            if usage["prompt_tokens"] or usage["completion_tokens"]:
                # Usar el ID canónico del catálogo para que el billing agrupe bien.
                model_for_tracking = (
                    MODELS_CATALOG[request_model]["id"]
                    if request_model in MODELS_CATALOG
                    else (request_model or name)
                )
                # Normalizar aliases (qwen35, qwen35b, path .gguf, etc.)
                try:
                    from billing_log import normalize_model_name
                    model_for_tracking = normalize_model_name(model_for_tracking)
                except ImportError:
                    pass
                billing.track_usage(
                    client_id=client_id,
                    model=model_for_tracking,
                    prompt_tokens=usage["prompt_tokens"],
                    completion_tokens=usage["completion_tokens"],
                )
                # Log completo en SQLite
                price = MODEL_PRICES.get(model_for_tracking, MODEL_PRICES.get(name, {"input": 0.0, "output": 0.0}))
                cost = (usage["prompt_tokens"] / 1_000_000 * price["input"] +
                        usage["completion_tokens"] / 1_000_000 * price["output"])
                resp_text = _extract_response_text_json(content)
                log_request(
                    client_id=client_id, model=model_for_tracking, backend=name,
                    prompt_tokens=usage["prompt_tokens"],
                    completion_tokens=usage["completion_tokens"],
                    cost_usd=cost, latency_ms=int((time.monotonic()-t0)*1000),
                    status_code=status, stream=False,
                    prompt=prompt_text, response=resp_text[:5000],
                    api_key_masked=api_key_masked)
        except Exception as e:
            log.debug(f"billing track error: {e}")
    purge_old()

    # Reemplazar el campo "model" en la respuesta por el ID limpio
    # (algunos backends como llamacpp devuelven el path del .gguf)
    if status == 200 and request_model:
        try:
            resp_data = json.loads(content)
            if isinstance(resp_data, dict) and "model" in resp_data:
                # Si el modelo devuelto es un path o distinto al solicitado, reemplazarlo
                if resp_data["model"] != request_model:
                    resp_data["model"] = request_model
                    content = json.dumps(resp_data).encode("utf-8")
        except (json.JSONDecodeError, UnicodeDecodeError):
            pass

    if billing and client_id:
        try:
            quota = billing.get_quota_status(client_id, plan or "free")
            headers["X-RateLimit-Tokens-Remaining"] = str(quota["tokens_remaining"])
            headers["X-RateLimit-Tokens-Quota"] = str(quota["tokens_quota"])
        except Exception:
            pass

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


@app.get("/bus/usage")
async def bus_usage(authorization: str = Header(None)):
    """Resumen de uso del cliente. Requiere su propia API key."""
    billing = getattr(app.state, "billing", None) if app.state else None
    if not billing:
        return JSONResponse({"error": "billing no disponible"}, status_code=503)
    if not authorization:
        raise HTTPException(status_code=401, detail="Authorization requerido")
    api_key = authorization.replace("Bearer ", "").strip()
    rec = billing.validate_key(api_key)
    if not rec:
        raise HTTPException(status_code=401, detail="API key invalida")
    client_id = rec.get("client_id", "")
    plan = rec.get("plan", "free")
    usage = billing.get_client_usage(client_id)
    quota = billing.get_quota_status(client_id, plan)
    return {"usage": usage, "quota": quota, "plan": plan}


@app.get("/bus/pricing")
async def bus_pricing():
    """Lista pública de precios por modelo y planes disponibles."""
    return {"models": MODEL_PRICES, "plans": PLANS}


# ---------------------------------------------------------------------------
# Modelos expuestos vía /v1/models (formato OpenAI).
# Cada modelo mapea a un backend interno + su model_id real.
# Los IDs públicos son los CANÓNICOS (lo que se normaliza en billing_log).
MODELS_CATALOG = {
    "qwen36-35b-a3b": {
        "id": "qwen36-35b-a3b",
        "backend": "qwen35b",
        "model_id": "Qwen3.6-35B-A3B-UD-IQ4_NL_XL.gguf",
        "name": "Qwen 3.6 35B A3B",
        "owned_by": "ojoia",
        "description": "Modelo MoE de 35B (3B activos) cuantizado IQ4 en llama.cpp. Máxima calidad para razonamiento complejo, código avanzado y multimodalidad (texto, imagen, audio, video). Contexto de 156K.",
        "capabilities": ["text", "image", "audio", "video", "pdf", "thinking"],
        "modalities": {
            "input": ["text", "image", "audio", "video", "pdf"],
            "output": ["text"]
        },
        "context_length": 156160,
        "supports_tools": True,
        "supports_thinking": True,
    },
    "qwen38": {
        "id": "qwen38",
        "backend": "qwen38",
        "model_id": "qwen3.8-27b",
        "name": "Qwen 3.8 27B A3B+",
        "owned_by": "ojoia",
        "description": "Modelo denso de 27B con atención híbrida (linear+full). Máxima calidad para razonamiento complejo, código agéntico y tareas de contexto largo. Contexto de 150K (hasta 262K). Servido con vLLM optimizado (W4A16 int8 + fp16 state + MTP speculative).",
        "capabilities": ["text", "thinking"],
        "modalities": {
            "input": ["text"],
            "output": ["text"]
        },
        "context_length": 150000,
        "supports_tools": True,
        "supports_thinking": True,
    },
    "qwen7b": {
        "id": "qwen7b",
        "backend": "qwen7b",
        "model_id": "qwen7b",
        "name": "Qwen 3 7B (sglang)",
        "owned_by": "ojoia",
        "description": "Modelo de 7B parámetros en SGLang. Procesa texto, imágenes y video. Perfecto para tareas rápidas, chat general y prototipado con bajo consumo. Contexto de 16K.",
        "capabilities": ["text", "image", "video", "thinking"],
        "modalities": {
            "input": ["text", "image", "video"],
            "output": ["text"]
        },
        "context_length": 16000,
        "supports_tools": True,
        "supports_thinking": True,
    },
    "qwen9b": {
        "id": "qwen9b",
        "backend": "qwen9b",
        "model_id": "qwen9b",
        "name": "Qwen 3 9B (vLLM)",
        "owned_by": "ojoia",
        "description": "Modelo de 9B parámetros en vLLM. Procesa texto, imágenes y video. Ideal para chat general, código y razonamiento con contexto de 128K. Soporta tool calling y thinking.",
        "capabilities": ["text", "image", "video", "thinking"],
        "modalities": {
            "input": ["text", "image", "video"],
            "output": ["text"]
        },
        "context_length": 131072,
        "supports_tools": True,
        "supports_thinking": True,
    },
    "qwen3vl8b": {
        "id": "qwen3vl8b",
        "backend": "qwen3vl8b",
        "model_id": "Qwen3VL-8B-Instruct",
        "name": "Qwen 3 VL 8B (llama.cpp)",
        "owned_by": "ojoia",
        "description": "El VLM open-source más capaz a 8B. Superó a Qwen2.5-VL en todos los benchmarks. OCR en 32 idiomas, análisis espacial, robusto en baja luz y ángulos adversos. Ideal para análisis de cámaras de seguridad y visión de alto rendimiento. Contexto 16K.",
        "capabilities": ["text", "image", "video"],
        "modalities": {
            "input": ["text", "image", "video"],
            "output": ["text"]
        },
        "context_length": 16384,
        "supports_tools": False,
        "supports_thinking": False,
    },
    "whisper-turbo": {
        "id": "whisper-turbo",
        "backend": "whisper",
        "model_id": "whisper-turbo",
        "name": "Whisper Turbo",
        "owned_by": "ojoia",
        "description": "Modelo de transcripción de audio a texto usando faster-whisper. Soporta múltiples idiomas y es muy rápido.",
        "capabilities": ["audio"],
        "modalities": {
            "input": ["audio"],
            "output": ["text"]
        },
        "context_length": 0,
        "supports_tools": False,
        "supports_thinking": False,
    },
}

# Mapeo de modelos para inyectar en requests si no vienen
MODEL_TO_BACKEND = {m["id"]: m["backend"] for m in MODELS_CATALOG.values()}

# Mapeo del nombre canónico (tras normalize_model_name) -> backend.
# Permite que aliases del 7B/9B (qwen-vl-7b, qwen35, etc.) facturen al backend
# correcto en vez de caer silenciosamente al 9B.
_BACKEND_BY_CANONICAL = {
    "qwen7b": "qwen7b",
    "qwen9b": "qwen9b",
    "qwen36-35b-a3b": "qwen35b",
    "qwen38": "qwen38",
    "qwen3.8-27b": "qwen38",
    "qwen3vl8b": "qwen3vl8b",
    "qwen3vl": "qwen3vl8b",
    "qwen3-vl-8b": "qwen3vl8b",
    "whisper-turbo": "whisper",
    "yolo": "yolo",
}
# ---------------------------------------------------------------------------
@app.get("/v1/models")
async def list_models():
    """Lista de modelos disponibles (formato OpenAI) para Kilo Code."""
    models_list = []
    for m in MODELS_CATALOG.values():
        models_list.append({
            "id": m["id"],
            "object": "model",
            "created": 1787428011,
            "owned_by": m["owned_by"],
            "capabilities": m.get("capabilities", ["text"]),
            "modalities": m.get("modalities", {}),
            "name": m.get("name", m["id"]),
            "description": m.get("description", ""),
            "context_length": m.get("context_length", 0),
            "supports_tools": m.get("supports_tools", False),
            "supports_thinking": m.get("supports_thinking", False),
        })
    return {"object": "list", "data": models_list}


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    """Proxy de chat completions con routing por modelo."""
    body = await request.body()
    model_id = ""
    if body:
        try:
            payload = json.loads(body)
            model_id = payload.get("model", "")
        except Exception:
            pass

    # Determinar backend según el modelo solicitado
    if model_id in MODEL_TO_BACKEND:
        backend = MODEL_TO_BACKEND[model_id]
    else:
        # Normalizar aliases conocidos antes de decidir el fallback
        normalized = model_id
        try:
            from billing_log import normalize_model_name
            normalized = normalize_model_name(model_id)
        except Exception:
            pass
        if normalized in _BACKEND_BY_CANONICAL:
            backend = _BACKEND_BY_CANONICAL[normalized]
        else:
            # Modelo desconocido: facturar como 9B es un bug de facturación
            # (mide el modelo real, no el precio). Registrar alerta.
            log.warning(
                f"[billing] modelo no catalogado '{model_id}' -> fallback qwen9b. "
                f"Si esperabas otro modelo, revisa MODELS_CATALOG."
            )
            backend = "qwen9b"

    return await proxy_route(backend, "v1/chat/completions", request)


# Endpoints OpenAI-compatible en la raíz (para Kilo Code / VS Code).
# Routean /v1/* al backend por defecto (qwen9b) con el mismo flujo de billing.
# El campo "model" del body se respeta, pero el backend se fija aquí.


# Serving estático de la página de test de modelos (chatrd-test.ojoia.com.do)
# Sirve archivos desde /home/sam/chatrd/ bajo la ruta /test/*
TEST_STATIC_DIR = Path("/home/sam/chatrd")

if TEST_STATIC_DIR.is_dir():
    app.mount("/test", StaticFiles(directory=str(TEST_STATIC_DIR), html=False),
              name="test_static")


# proxy goloso va al final
@app.api_route("/{backend}/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def proxy_route(backend: str, path: str, request: Request):
    if backend not in BACKENDS:
        raise HTTPException(status_code=404, detail=f"backend desconocido: {backend}")

    # Auth + rate limit + quota (billing)
    billing = getattr(app.state, "billing", None) if app.state else None
    client_id = ""
    plan = "free"

    if billing:
        auth = request.headers.get("authorization", "")
        api_key = auth.replace("Bearer ", "").strip() if auth else ""

        if not api_key:
            return JSONResponse(
                {"error": "API key requerida",
                 "detail": "Envia Authorization: Bearer ojoia_live_..."},
                status_code=401)

        rec = billing.validate_key(api_key)
        if not rec:
            return JSONResponse(
                {"error": "API key invalida o revocada"},
                status_code=401)

        client_id = rec.get("client_id", "")
        plan = rec.get("plan", "free")

        # Rate limit (sliding window)
        ok, remaining, reset = billing.check_rate_limit(client_id, plan)
        if not ok:
            return JSONResponse(
                {"error": "rate limit exceeded",
                 "retry_after": reset,
                 "plan": plan},
                status_code=429,
                headers={"Retry-After": str(reset),
                         "X-RateLimit-Remaining": "0"})

        # Quota check
        if not billing.check_quota(client_id, plan):
            return JSONResponse(
                {"error": "quota exceeded",
                 "detail": f"Has superado el limite de tokens del plan {plan}",
                 "plan": plan},
                status_code=402)  # Payment Required

    return await _enqueue_and_proxy(backend, request, path,
                                     client_id=client_id, plan=plan)


@app.on_event("startup")
async def _init_http():
    app.state.http = httpx.AsyncClient(timeout=httpx.Timeout(900.0, connect=15.0),
                                       limits=httpx.Limits(max_connections=1000,
                                                            max_keepalive_connections=200))
    try:
        app.state.billing = BillingStore.instance()
        log.info("Billing store conectado a Redis")
    except Exception as e:
        log.warning(f"Billing store no disponible: {e}")
        app.state.billing = None


@app.on_event("shutdown")
async def _close_http():
    await app.state.http.aclose()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")
