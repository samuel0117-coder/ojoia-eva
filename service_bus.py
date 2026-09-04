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
  qwen7b     -> 127.0.0.1:8004    sem=48  backlog=5000
  qwen38     -> 127.0.0.1:18020   sem=64  backlog=5000
  qwen3vl8b  -> 127.0.0.1:8019    sem=4   backlog=1000
  whisper    -> 127.0.0.1:8008    sem=16  backlog=5000
  yolo       -> 127.0.0.1:8002    sem=12  backlog=5000
  (qwen9b y qwen35b retirados — contenedores eliminados)
"""
import os
import sys
import asyncio
import json
import time
import hashlib
import base64
import binascii
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
    # sem 7b: 48 (antes 150) — sglang capturó CUDA graphs para max-bs 48;
    # más de 48 concurrentes fuerza re-capture en vivo + picos de prefill
    # que desbordaban la VRAM compartida de GPU0 (congelamientos + Exit 137
    # del contenedor en ráfagas de la batería, 2026-09-03).
    "qwen7b":  {"url": "http://127.0.0.1:8004", "sem": 48, "backlog": 5000},
    "qwen38":  {"url": "http://127.0.0.1:18020", "sem": 64, "backlog": 5000},
    "qwen3vl8b": {"url": "http://127.0.0.1:8019", "sem": 4,  "backlog": 1000},
    "whisper": {"url": "http://127.0.0.1:8008", "sem": 16,  "backlog": 5000},
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

from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="OjoIA Service Bus", version="1.0")

# CORS: permitir que la página de chat (cualquier origen) hable con la API.
# La seguridad real la da el Bearer de billing, no el origin.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

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
# FASE 2: Session affinity + clasificación de carga
# ---------------------------------------------------------------------------

# Ventanas de contexto por modelo para decisión de clasificación
_TRIVIAL_MAX_CHARS = 1200        # ~400 tokens: saludos, preguntas cortas
_MEDIA_MAX_CHARS = 24000         # ~8k tokens: consultas específicas, tool-use


def _session_key_from_payload(payload: dict) -> str:
    """Deriva una clave de sesión estable de un payload OpenAI.

    Prioridad: header explícito > hash del system prompt + primer turno.
    El system prompt de un agente es estable entre turnos — hashearlo con
    el primer mensaje del usuario identifica la conversación sin que el
    cliente tenga que mandar nada nuevo.
    """
    try:
        msgs = payload.get("messages") or []
        parts = []
        for m in msgs[:2]:
            if isinstance(m, dict):
                content = m.get("content", "")
                if isinstance(content, list):
                    # multimodal: solo el texto (las imágenes cambian por turno)
                    content = " ".join(
                        p.get("text", "") for p in content
                        if isinstance(p, dict) and p.get("type") == "text")
                parts.append(str(content)[:2000])
        raw = "|".join(parts)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16] if raw else ""
    except Exception:
        return ""


def _estimate_prompt_tokens(payload: dict) -> int:
    """Estimación rápida de tokens del prompt (~3 chars/token, JSON incluido)."""
    try:
        msgs = payload.get("messages") or []
        chars = sum(len(json.dumps(m, default=str)) for m in msgs
                     if isinstance(m, dict))
        tools_chars = len(json.dumps(payload.get("tools") or ""))
        return (chars + tools_chars) // 3
    except Exception:
        return 0


def _classify_task(payload: dict, est_tokens: int) -> str:
    """Clasifica la petición para routing por costo/latencia.

    'trivial'  — chat corto sin tools: el 7B responde igual de bien, 10x más barato.
    'media'    — prompts medianos, primera interacción con imagen: el 8B VL.
    'pesada'   — todo lo demás: agentes, contexto largo, tools, JSON: el 27B.

    Reglas duras (no dependen de heurística de contenido):
      - tools/respuesta estructurada → pesada (el 27B es el único con tools fiables)
      - imagen/video adjunto → media (vision del 8B es la mejor medida)
      - thinking explícito del cliente → pesada
      - prompt muy corto sin tools → trivial
    """
    msgs = payload.get("messages") or []
    has_image = False
    has_tools = bool(payload.get("tools"))
    thinking_on = bool(
        (payload.get("chat_template_kwargs") or {}).get("enable_thinking"))
    for m in msgs:
        c = m.get("content") if isinstance(m, dict) else None
        if isinstance(c, list):
            for part in c:
                if isinstance(part, dict) and part.get("type") in (
                        "image_url", "input_image", "video_url"):
                    has_image = True
                    break
        if has_image:
            break
    if has_tools or thinking_on:
        return "pesada"
    if has_image:
        return "media"
    if est_tokens * 3 <= _TRIVIAL_MAX_CHARS and len(msgs) <= 4:
        return "trivial"
    if est_tokens * 3 <= _MEDIA_MAX_CHARS:
        return "media"
    return "pesada"


# Routing objetivo por clase de tarea (solo se usa si el modelo pedido
# es "auto" o si el cliente manda el header X-Route: auto)
_TASK_ROUTING = {
    "trivial": "qwen7b",
    "media": "qwen3vl8b",
    "pesada": "qwen38",
}


# ── FASE 2.3: Compactación de contexto ────────────────────────────────────
# Umbral: a 60% del contexto del modelo, el historial viejo se resume a un
# mensaje sintético. Evita el crecimiento lineal de costos por turno que
# vimos en la telemetría (contextos de 80-115k creciendo +100 tok/turno).
_COMPACT_KEEP_TAIL = 6              # últimos N turnos conservados íntegros
_COMPACT_SUMMARY_MAX_CHARS = 3000   # techo del resumen del historial viejo


def _compact_messages_if_needed(payload: dict, hard_cap: int) -> tuple[dict, bool]:
    """Compacta el historial si el prompt estimado pasa del 60% del cap.

    Estrategia estructural (sin LLM, determinista, preserva el cache del
    prefijo): system + primer mensaje usuario se mantienen; los turnos del
    medio se reemplazan por UN mensaje de resumen con marcadores de
    conteo; los últimos _COMPACT_KEEP_TAIL turnos van íntegros.

    Retorna (payload_posiblemente_modificado, se_compacto).
    """
    try:
        msgs = payload.get("messages")
        if not isinstance(msgs, list) or len(msgs) < _COMPACT_KEEP_TAIL + 4:
            return payload, False
        est = _estimate_prompt_tokens(payload)
        if est < hard_cap * 0.60:
            return payload, False

        system_msgs = [m for m in msgs if isinstance(m, dict)
                       and m.get("role") == "system"]
        head = [m for m in msgs[:2] if isinstance(m, dict)
                and m.get("role") == "user"]
        tail = [m for m in msgs[-_COMPACT_KEEP_TAIL:] if isinstance(m, dict)]
        head_ids = {id(m) for m in head}
        tail_ids = {id(m) for m in tail}
        middle = [m for m in msgs if isinstance(m, dict)
                  and id(m) not in head_ids and id(m) not in tail_ids
                  and m.get("role") != "system"]

        # Resumen estructural del medio: conteos por rol + primeros chars
        # de cada turno (suficiente para continuidad de contexto)
        summary_lines = []
        for i, msg in enumerate(middle):
            role = msg.get("role", "?")
            c = msg.get("content", "")
            if isinstance(c, list):
                c = " ".join(p.get("text", "") for p in c if isinstance(p, dict))
            c = str(c).replace("\n", " ")[:150]
            summary_lines.append(f"[{i+1}] {role}: {c}")
        summary_txt = "\n".join(summary_lines)[:_COMPACT_SUMMARY_MAX_CHARS]

        compact_msg = {
            "role": "user",
            "content": (
                f"[Contexto previo compactado — {len(middle)} turnos resumidos "
                f"para ahorrar espacio. Contenido por turno:]\n{summary_txt}"),
        }
        new_msgs = system_msgs + head + [compact_msg] + tail
        payload["messages"] = new_msgs
        return payload, True
    except Exception:
        return payload, False




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


def _extract_stream_finish_and_content(content: bytes) -> tuple[str, bool]:
    """Extrae finish_reason del último chunk y si hubo delta.content real.

    Detecta la patología thinking: stream 200 que generó tokens pero TODO
    fue a reasoning_content — el cliente ve respuesta vacía y reintenta
    con el historial acumulado (+178 tok/turno = death-spiral streaming).
    """
    finish = ""
    has_content = False
    if not content:
        return finish, has_content
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
                ch = choices[0]
                fr = ch.get("finish_reason")
                if fr:
                    finish = fr
                delta = ch.get("delta") or {}
                if delta.get("content"):
                    has_content = True
    except (UnicodeDecodeError, ValueError):
        pass
    return finish, has_content


# ── FASE 3.1: Middleware de imágenes ──────────────────────────────────────
# Los VLM cobran por píxeles (Qwen ≈ 1 token por cada ~780-1024 px). Una
# foto de cámara a 4000×3000 = 12Mpx ≈ 15k tokens por imagen, y los
# agentes reenvían el historial completo cada turno. Cap a ~1M px (~1.3k
# tokens) — la calidad VLM apenas se afecta (informe del agente: 960×960
# es el estándar del orchestrator de cámaras).
#
# Reglas críticas:
#  - Área ≤ cap: el b64 pasa INTACTO (byte-idéntico) → el prefix-cache
#    del backend no se invalida. Re-encodear siempre destruiría el cache
#    de conversaciones con imágenes (Kilo reenvía historial).
#  - Re-encode determinista: LANCZOS + quality fijo → mismo input da
#    mismo output SIEMPRE (cache estable entre turnos).
#  - LRU por sha256: la misma imagen reenviada por historial no se
#    re-procesa (cero CPU el 2º turno en adelante).
#  - qwen38: en vez de re-encodear, inyectamos max_pixels nativo
#    (mm_processor_kwargs) — cero CPU, el backend lo respeta por request.

_MAX_IMAGE_PIXELS = 1_000_000      # cap de área (~1024×1024)
_IMAGE_CACHE: dict[str, str] = {}  # sha256(orig_b64) -> b64 reducido
_IMAGE_CACHE_MAX = 64              # entradas LRU

try:
    from PIL import Image
    import io as _io
    _HAS_PIL = True
except ImportError:
    _HAS_PIL = False


def _process_image_part(part: dict) -> dict:
    """Reduce una parte image_url si excede el cap de píxeles. Mutación in-place."""
    try:
        url = (part.get("image_url") or {})
        if isinstance(url, str):
            url = {"url": url}
        data_uri = url.get("url", "")
        if not isinstance(data_uri, str) or not data_uri.startswith("data:image"):
            return part
        header, _, b64 = data_uri.partition(",")
        if not b64:
            return part

        # LRU hit: la misma imagen ya fue reducida antes
        h = hashlib.sha256(b64.encode("ascii", errors="ignore")).hexdigest()
        cached = _IMAGE_CACHE.get(h)
        if cached is not None:
            url["url"] = cached
            part["image_url"] = url
            return part

        # Decodificar cabecera perezosamente: img.size NO carga píxeles
        raw = base64.b64decode(b64, validate=False)
        img = Image.open(_io.BytesIO(raw))
        w, hpx = img.size
        if w * hpx <= _MAX_IMAGE_PIXELS:
            return part  # bajo el cap: intacta (prefix-cache preservado)

        # Reducción determinista
        scale = (_MAX_IMAGE_PIXELS / (w * hpx)) ** 0.5
        new_w = max(1, int(w * scale))
        new_h = max(1, int(hpx * scale))
        img = img.convert("RGB").resize((new_w, new_h), Image.LANCZOS)
        buf = _io.BytesIO()
        img.save(buf, format="JPEG", quality=85, optimize=True)
        new_b64 = base64.b64encode(buf.getvalue()).decode("ascii")
        new_uri = "data:image/jpeg;base64," + new_b64

        # LRU store
        if len(_IMAGE_CACHE) >= _IMAGE_CACHE_MAX:
            _IMAGE_CACHE.pop(next(iter(_IMAGE_CACHE)), None)
        _IMAGE_CACHE[h] = new_uri
        url["url"] = new_uri
        part["image_url"] = url
        return part
    except Exception:
        return part  # ante cualquier duda: pasar intacta


def _transform_images(payload: dict, target_backend: str) -> tuple[dict, int]:
    """Aplica el middleware a todas las imágenes del payload.

    Para qwen38: inyecta mm_processor_kwargs.max_pixels (nativo, cero CPU)
    si el cliente no lo especificó — el backend respeta el cap por request.
    Para los demás: reduce en el bus solo si excede el cap.

    Retorna (payload, n_imágenes_procesadas).
    """
    n = 0
    try:
        if target_backend == "qwen38":
            # Nativo: no tocar el b64, solo inyectar el kwargs
            if isinstance(payload.get("mm_processor_kwargs"), dict):
                payload["mm_processor_kwargs"].setdefault(
                    "max_pixels", _MAX_IMAGE_PIXELS)
            else:
                payload["mm_processor_kwargs"] = {"max_pixels": _MAX_IMAGE_PIXELS}
            return payload, 0

        if not _HAS_PIL:
            return payload, 0
        msgs = payload.get("messages")
        if not isinstance(msgs, list):
            return payload, 0
        for msg in msgs:
            if not isinstance(msg, dict):
                continue
            c = msg.get("content")
            if not isinstance(c, list):
                continue
            for part in c:
                if (isinstance(part, dict)
                        and part.get("type") in ("image_url", "input_image")):
                    _process_image_part(part)
                    n += 1
    except Exception:
        pass
    return payload, n


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

    # ── Guardrails fase 1: cap de prompt + retry budget ─────────────────────
    # 1) CAP de prompt: estimación por chars (1 tok ≈ 3 chars; conservador).
    #    Un prompt gigante reenviado en loop era el patrón del incidente
    #    2026-09-03 ($84 en re-prefills inútiles) — se rechaza con 413.
    # 2) RETRY BUDGET: token-bucket por cliente (Lua atómico en Redis) —
    #    consulta el balance ANTES de encolar; fail-fast si está agotado.
    #    Solo aplica a los backends de LLM (no whisper/yolo).
    _billing = getattr(app.state, "billing", None) if app.state else None
    if (method == "POST" and name in ("qwen38", "qwen7b", "qwen3vl8b")
            and _billing and client_id
            and "application/json" in request.headers.get("content-type", "")):
        prompt_chars = 0
        guard_model = ""
        try:
            pre = json.loads(body)
            if isinstance(pre, dict):
                guard_model = str(pre.get("model", ""))
                # FASE 3.1: middleware de imágenes ANTES de estimar tokens —
                # una imagen de 6Mpx son ~97k tokens estimados por chars;
                # reducida a 1Mpx pasa a ~13k. También inyecta max_pixels
                # nativo para qwen38. Sin esto el guardrail rechazaría
                # payloads de visión legítimos.
                if "chat/completions" in path or path.startswith("v1/chat"):
                    pre, _nimg = _transform_images(pre, name)
                    body = json.dumps(pre).encode("utf-8")
                    fwd_headers["content-length"] = str(len(body))
                if isinstance(pre.get("messages"), list):
                    # La estimación por chars (1 tok ≈ 3 chars) es válida
                    # para texto, pero un b64 de imagen NO son tokens de
                    # texto: el backend lo tokeniza como píxeles (~1 tok
                    # por ~1000px). Excluir data URIs del conteo y sumar
                    # su estimación por píxeles en su lugar.
                    prompt_chars = 0
                    for msg in pre["messages"]:
                        if not isinstance(msg, dict):
                            continue
                        c = msg.get("content")
                        if isinstance(c, list):
                            for part in c:
                                if isinstance(part, dict):
                                    u_ = (part.get("image_url") or {})
                                    if isinstance(u_, dict) and str(
                                            u_.get("url", "")).startswith("data:"):
                                        # imagen: ~1Mpx máx tras middleware
                                        # ≈ 1.3k tokens
                                        prompt_chars += 4000
                                    elif isinstance(part.get("text"), str):
                                        prompt_chars += len(part["text"])
                        elif isinstance(c, str):
                            prompt_chars += len(c)
        except Exception:
            prompt_chars = len(body)
        est_tokens = (prompt_chars // 3) if prompt_chars else (len(body) // 3)
        if est_tokens > 500 or guard_model:
            cat_entry = MODELS_CATALOG.get(guard_model, {})
            max_ctx = cat_entry.get("context_length", 0) or (
                150000 if name == "qwen38" else 16000)
            hard_cap = int(max_ctx * 0.985) if max_ctx else 148000

            # ── FASE 2.3: compactación de contexto (>60% del cap) ──────────
            # Solo si el cliente no la desactiva (X-No-Compact: 1).
            if (est_tokens > hard_cap * 0.60
                    and request.headers.get("x-no-compact", "") != "1"):
                compacted, did = _compact_messages_if_needed(pre, hard_cap)
                if did:
                    log.info(
                        f"[bus] contexto compactado: ~{est_tokens} -> "
                        f"~{_estimate_prompt_tokens(compacted)} tokens "
                        f"({name}, session={_session_key_from_payload(compacted) or '?'})")
                    pre = compacted
                    guard_model = str(pre.get("model", ""))
                    prompt_chars = sum(
                        len(json.dumps(msg, default=str))
                        for msg in pre.get("messages", [])
                        if isinstance(msg, dict))
                    est_tokens = (prompt_chars // 3) if prompt_chars else est_tokens
                    body = json.dumps(pre).encode("utf-8")
                    fwd_headers["content-length"] = str(len(body))

            if est_tokens > hard_cap:
                m["total_429"] += 1
                log.warning(
                    f"[bus] prompt rechazado: ~{est_tokens} tokens estimados > "
                    f"cap {hard_cap} ({name}, client={client_id}). "
                    f"Compacta la conversación.")
                return JSONResponse(
                    {"error": "prompt_too_large",
                     "detail": (f"Tu prompt (~{est_tokens} tokens) excede el "
                                f"límite ({hard_cap}) de {guard_model or name}. "
                                "Compacta la conversación o reduce el historial."),
                     "backend": name, "estimated_tokens": est_tokens,
                     "cap": hard_cap},
                    status_code=413)
            # Retry budget: si el bucket del cliente está agotado, fail-fast.
            # (El consumo real se hace al registrar el resultado del request.)
            allowed, _bal = _billing.check_retry_budget(client_id, est_tokens)
            if not allowed:
                m["total_429"] += 1
                return JSONResponse(
                    {"error": "retry_budget_exhausted",
                     "detail": ("Demasiados reintentos/fallos recientes. "
                                "Espera ~60s o reduce el tamaño del prompt."),
                     "backend": name, "retry_after": 60},
                    status_code=429, headers={"Retry-After": "60"})

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
                    # (middleware de imágenes ya aplicado en el guardrail)
                    # Thinking: respetar lo que el cliente envie en chat_template_kwargs.
                    # Si el cliente NO especifica, desactivar thinking por defecto (maxima velocidad).
                    if "chat_template_kwargs" not in payload:
                        payload["chat_template_kwargs"] = {"enable_thinking": False}
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
                                r'"model"\s*:\s*"[^"]*\.gguf[^"]*"',
                                '"model":"' + request_model + '"',
                                text,
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
                # Retry budget: stream OK (recibió chunks) devuelve 50%,
                # stream roto/vacío consume completo.
                # FASE 2-fix: stream que terminó SIN delta.content (todo fue
                # reasoning) con finish=length = patología thinking en
                # streaming = FALLO para el budget. Sin esto, el agente
                # reintenta con historial creciente (+178 tok/turno) y el
                # bucket lo ve todo como éxitos (bucle ids 831-987).
                if billing and client_id and name in (
                        "qwen38", "qwen7b", "qwen3vl8b"):
                    try:
                        pt, _ct = _extract_usage_from_sse(full_buf)
                        fin, has_content = _extract_stream_finish_and_content(full_buf)
                        if (status_code == 200 and not has_content
                                and fin == "length" and name == "qwen38"):
                            # thinking se comió el budget en streaming:
                            # cuenta como fallo → drena el bucket → el
                            # reintento del cliente recibe 429 en 2-3 turnos
                            log.warning(
                                f"[bus] stream sin content+finish=length en "
                                f"{name} (thinking en streaming) -> budget "
                                f"drenado, cliente: {client_id}")
                            success = False
                        else:
                            success = bool(full_buf) and status_code == 200
                        billing.consume_retry_budget(
                            client_id, pt or 1, success=success)
                    except Exception:
                        pass
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

    # Latencia máxima de un job en cola: 900s heredados del legacy — se
    # mantiene como techo, pero el cap de prompt evita los gigantes que lo
    # consumían. (TO-DO fase 3: bajar a 300s cuando haya datos.)
    try:
        status, headers, content = await asyncio.wait_for(fut, timeout=900)
    except asyncio.TimeoutError:
        m["total_err"] += 1
        return JSONResponse({"error": "backend timeout", "backend": name},
                            status_code=504)
    except Exception as e:
        m["total_err"] += 1
        # Retry budget: fallo de backend consume el bucket completo.
        if billing and client_id and name in ("qwen38", "qwen7b", "qwen3vl8b"):
            try:
                est = len(body) // 3 or 1
                billing.consume_retry_budget(client_id, est, success=False)
            except Exception:
                pass
        return JSONResponse({"error": "backend error", "detail": str(e)[:200]},
                            status_code=502)

    # ── Clasificador de respuestas problemáticas ────────────────────────────
    # 200 + contenido vacío NO es éxito: es la patología Qwen3-thinking
    # (el budget de max_tokens se lo comió el razonamiento). Reintentar el
    # mismo prompt seria el death-spiral; en su lugar, UN solo reintento
    # con thinking desactivado. Si aun asi falla, error tipificado.
    if (status == 200 and name in ("qwen38", "qwen7b")
            and "application/json" in headers.get("content-type", "")):
        try:
            resp_json = json.loads(content)
            choice = (resp_json.get("choices") or [{}])[0]
            finish = choice.get("finish_reason", "")
            empty_content = not (choice.get("message") or {}).get("content")
            # La patología real (incidente 2026-09-03): content vacío porque
            # el thinking se comió el max_tokens. Reintento thinking-off.
            if empty_content and finish == "length":
                log.info(
                    f"[bus] vacía+length en {name} (thinking comió budget) "
                    f"-> 1 reintento con enable_thinking=false")
                retry_payload = None
                try:
                    retry_payload = json.loads(body)
                except Exception:
                    retry_payload = None
                if isinstance(retry_payload, dict):
                    retry_payload.pop("chat_template_kwargs", None)
                    retry_payload["chat_template_kwargs"] = {
                        "enable_thinking": False}
                    if isinstance(retry_payload.get("max_tokens"), int):
                        retry_payload["max_tokens"] = max(
                            retry_payload["max_tokens"], 2000)
                    retry_body = json.dumps(retry_payload).encode("utf-8")
                    retry_headers = dict(fwd_headers)
                    retry_headers["content-length"] = str(len(retry_body))
                    try:
                        client = app.state.http
                        url = f"{BACKENDS[name]['url']}/{path}"
                        if query:
                            url += f"?{query}"
                        req = client.build_request(
                            method, url, content=retry_body,
                            headers=retry_headers)
                        upstream = await client.send(req, stream=False)
                        if upstream.status_code == 200:
                            rc = upstream.content
                            rc_choice = (json.loads(rc).get("choices") or [{}])[0]
                            if (rc_choice.get("message") or {}).get("content"):
                                status, content = upstream.status_code, rc
                                headers = {k: v for k, v in upstream.headers.items()
                                           if k.lower() not in HOP}
                                log.info(f"[bus] reintento thinking-off OK en {name}")
                    except Exception as e:
                        log.warning(f"[bus] reintento thinking-off falló: {e}")
            elif empty_content:
                # 200 con content vacío y finish != length (stop inmediato):
                # comportamiento del modelo — log tipificado, no reintentar
                log.warning(
                    f"[bus] respuesta vacía finish={finish!r} en {name} "
                    f"-> sin reintento (comportamiento del modelo)")
        except (json.JSONDecodeError, UnicodeDecodeError, IndexError, KeyError):
            pass

    # Retry budget: éxito en no-streaming devuelve 50% al bucket.
    if billing and client_id and name in ("qwen38", "qwen7b", "qwen3vl8b"):
        try:
            usage_est = extract_usage_from_response(
                name, content, headers.get("content-type", ""))
            pt_real = usage_est.get("prompt_tokens") or (len(body) // 3 or 1)
            ok = status == 200
            billing.consume_retry_budget(client_id, pt_real, success=ok)
        except Exception:
            pass

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
    "auto": {
        "id": "auto",
        "backend": "qwen38",
        "model_id": "auto",
        "name": "Auto Router",
        "owned_by": "ojoia",
        "description": ("Enrutamiento inteligente por tarea y carga: trivial→qwen7b, "
                        "media/visión→qwen3vl8b, pesada/agentes→qwen38. Con session "
                        "affinity: la misma conversación pega al prefix-cache del "
                        "backend que ya la tiene. Usa model='auto' o X-Route: auto."),
        "capabilities": ["routing", "text", "image"],
        "modalities": {"input": ["text", "image"], "output": ["text"]},
        "context_length": 150000,
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
# "auto" se resuelve a un modelo real en chat_completions antes de este mapeo
MODEL_TO_BACKEND = {m["id"]: m["backend"] for m in MODELS_CATALOG.values()
                    if m["id"] != "auto"}

# Mapeo del nombre canónico (tras normalize_model_name) -> backend.
# Permite que aliases del 7B/9B (qwen-vl-7b, qwen35, etc.) facturen al backend
# correcto en vez de caer silenciosamente al 9B.
_BACKEND_BY_CANONICAL = {
    "qwen7b": "qwen7b",
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
    """Proxy de chat completions con routing por modelo + affinity + auto-route."""
    body = await request.body()
    model_id = ""
    payload = None
    if body:
        try:
            payload = json.loads(body)
            model_id = payload.get("model", "") if isinstance(payload, dict) else ""
        except Exception:
            pass

    # ── FASE 2: auto-routing opcional ──────────────────────────────────────
    # Si el modelo es "auto" (o header X-Route: auto), clasificar la tarea
    # y enrutar al backend por costo: trivial→7B, media→VL8B, pesada→27B.
    # El modelo final se reescribe en el body antes del proxy.
    route_auto = (model_id == "auto"
                  or request.headers.get("x-route", "").lower() == "auto")
    if route_auto and isinstance(payload, dict):
        est_tok = _estimate_prompt_tokens(payload)
        task_cls = _classify_task(payload, est_tok)
        model_id = _TASK_ROUTING.get(task_cls, "qwen38")
        payload["model"] = model_id
        body = json.dumps(payload).encode("utf-8")
        request._body = body  # el proxy relee el body desde request
        log.info(f"[bus] auto-route: {task_cls} -> {model_id}")

    # ── FASE 2.1: session affinity ─────────────────────────────────────────
    # El prefix-cache vive POR BACKEND. Sin affinity, el turno N de una
    # conversación puede caer a otro backend que el N-1 y re-prefillea
    # TODO el contexto (~100k tokens = ~100s de GPU quemados por turn).
    # Con affinity: misma conversación -> mismo backend -> prefill cacheado
    # (medido: 15k-57k tok/s vs 1k-2k frío).
    # Regla: en modo auto, si la sesión ya tiene backend fijado Y la clase
    # de tarea actual es compatible, se respeta el pin (ahorra re-prefill);
    # si la clase escala (trivial->pesada), el pin se actualiza.
    _sess_backend_pin = ""
    if isinstance(payload, dict):
        sess = (request.headers.get("x-session", "")
                or _session_key_from_payload(payload))
        if sess:
            _bl = getattr(app.state, "billing", None) if app.state else None
            if _bl:
                try:
                    aff_key = f"ojoia_billing:affinity:{sess}"
                    pinned = _bl.r.get(aff_key)
                    if route_auto and pinned and pinned in MODEL_TO_BACKEND:
                        # clase actual vs pin: pesada > media > trivial
                        _rank = {"qwen7b": 0, "qwen3vl8b": 1, "qwen38": 2}
                        if _rank.get(model_id, 2) <= _rank.get(pinned, 2):
                            model_id = pinned
                            payload["model"] = pinned
                            body = json.dumps(payload).encode("utf-8")
                            request._body = body
                    _bl.r.set(aff_key, model_id, ex=7200)
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
            # Modelo desconocido: responder error claro (NO fallback a un
            # backend inexistente — el 9b fue retirado y daba 404 duro).
            log.warning(
                f"[billing] modelo no catalogado '{model_id}' -> rechazado. "
                f"Modelos válidos: {list(_BACKEND_BY_CANONICAL.keys())}"
            )
            return JSONResponse(
                status_code=400,
                content={"error": {"message": f"Modelo '{model_id}' no existe. Usa /v1/models para ver los disponibles.", "type": "invalid_request_error", "param": "model", "code": "model_not_found"}},
            )

    return await proxy_route(backend, "v1/chat/completions", request)


# Endpoints OpenAI-compatible en la raíz (para Kilo Code / VS Code).
# Routean /v1/* al backend por defecto con el mismo flujo de billing.
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
