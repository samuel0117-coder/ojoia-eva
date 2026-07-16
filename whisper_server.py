#!/usr/bin/env python3
"""Whisper Turbo ASR (FP16 + Silero VAD + Batched Segments + VRAM hard cap)
Configurable por env vars para escalar entre GPUs en producción.
- WHISPER_GPU: physical GPU index (default "1")
- WHISPER_MEM_FRAC: per_process VRAM fraction (default 0.15 ≈ 3.6 GB)
- WHISPER_HIGH_WORKERS: high-priority concurrent transcriptions (default 2)
- WHISPER_NORMAL_WORKERS: normal-priority concurrent transcriptions (default 2)
- WHISPER_GLOBAL_CONCURRENT: global cap on simultaneous GPU transcriptions (default 2)
- WHISPER_BATCH_SIZE: segment-level batch inside audio (default 4)
"""
import asyncio
import os

GPU_ID = os.getenv("WHISPER_GPU", "1")
os.environ["CUDA_VISIBLE_DEVICES"] = GPU_ID
os.environ["HF_HOME"] = "/home/sam/.cache/hf_models"
os.environ["HF_HUB_OFFLINE"] = "0"

import torch
from faster_whisper import WhisperModel
try:
    from faster_whisper import BatchedInferencePipeline
    _HAS_BATCHED = True
except ImportError:
    _HAS_BATCHED = False
import uvicorn
from fastapi import FastAPI, UploadFile, File, HTTPException
import tempfile
from fastapi.middleware.cors import CORSMiddleware
import logging

WHISPER_HIGH_WORKERS = int(os.getenv("WHISPER_HIGH_WORKERS", "4"))
WHISPER_NORMAL_WORKERS = int(os.getenv("WHISPER_NORMAL_WORKERS", "4"))
WHISPER_GLOBAL_CONCURRENT = int(os.getenv("WHISPER_GLOBAL_CONCURRENT", "4"))
WHISPER_BATCH_SIZE = int(os.getenv("WHISPER_BATCH_SIZE", "4"))
WHISPER_MEM_FRAC = float(os.getenv("WHISPER_MEM_FRAC", "0.20"))
# Compute type: float16 is faster but CTranslate2 crashes on certain audio
# with "type_error.302". int8 never crashes but is ~150ms slower per audio.
# Default is int8 for production reliability. Override with WHISPER_COMPUTE=float16.
WHISPER_COMPUTE = os.getenv("WHISPER_COMPUTE", "int8").lower()  # "float16" or "int8"
# Modelo CT2. El "openai/whisper-large-v3-turbo" auto-convertido quedó corrupto
# (config formato transformers, sin alignment_heads/lang_ids -> type_error.302).
# Usamos una conversión CT2 sana y canónica por defecto.
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "deepdml/faster-whisper-large-v3-turbo-ct2")
USE_BATCHED = os.getenv("WHISPER_USER_BATCHED", "false").lower() in ("true", "1", "yes")

app = FastAPI(title="Whisper Turbo ASR", version="2.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("whisper_server")

model = None
_fallback_model = None  # int8 fallback, lazy-loaded on CTranslate2 type_error.302
_fallback_lock = None   # asyncio.Lock for lazy-loading the fallback model
_fallback_transient_lock = None  # asyncio.Lock for serializing int8 retries
queue = asyncio.Queue()
results = {}
next_id = 1
active = {"high": 0, "normal": 0}
high_semaphore = asyncio.Semaphore(WHISPER_HIGH_WORKERS)
normal_semaphore = asyncio.Semaphore(WHISPER_NORMAL_WORKERS)
# Global cap prevents VRAM spikes when many audios arrive at once.
# Combined with VRAM hard cap (WHISPER_MEM_FRAC), bursts above this cap
# will queue in `queue` rather than contending for GPU memory.
#
# NOTE: BatchedInferencePipeline wraps CTranslate2 which is NOT thread-safe
# for concurrent calls from multiple Python threads (causes
# "type must be number, but is number" JSON errors). We force the global
# semaphore to 1 when batched mode is active, serializing GPU access.
# Without batching, global_concurrent can be >1 for much higher throughput.
_global_concurrent = 1 if (USE_BATCHED and _HAS_BATCHED) else WHISPER_GLOBAL_CONCURRENT
global_semaphore = asyncio.Semaphore(_global_concurrent)
active_lock = asyncio.Lock()


async def transcribe_worker(job_id, tmp_path, language, priority):
    global active
    semaphore = high_semaphore if priority == "high" else normal_semaphore
    async with global_semaphore:        # ← NEW: global anti-pico
        async with semaphore:
            async with active_lock:
                active[priority] += 1
            try:
                # Build kwargs depending on whether batched pipeline is available
                # VAD params optimizados para rechazo de ruido/tos/"gracias"/"suscríbete"/etc
                # threshold mayor = menos falsos positivos (ruido no es voz)
                # min_speech_duration_ms mayor = rechaza tos/respiraciones (cortas <250ms)
                # min_silence_duration_ms menor = segmenta más rápido
                # speech_pad_ms = margen en bordes (no recorta palabras iniciales)
                transcribe_kwargs = dict(
                    language=language,
                    task="transcribe",
                    vad_filter=True,                # Silero VAD
                    vad_parameters=dict(
                        threshold=0.40,              # era 0.42 → menos falso-negativo (palabra suave no se corta)
                        min_speech_duration_ms=200,  # era 300 → palabra corta inicial se acepta
                        max_speech_duration_s=20,
                        min_silence_duration_ms=400, # era 200 → tolerate natural pauses 200-300ms
                        speech_pad_ms=250,           # era 200 → +50ms más para no comer sílabas
                    ),
                )
                if USE_BATCHED and _HAS_BATCHED:
                    transcribe_kwargs["batch_size"] = WHISPER_BATCH_SIZE  # segment-level batching

                try:
                    segments, info = await asyncio.to_thread(
                        model.transcribe,
                        tmp_path,
                        **transcribe_kwargs,
                    )
                except RuntimeError as e:
                    if "type_error.302" not in str(e):
                        raise
                    if WHISPER_COMPUTE != "float16":
                        # Already on int8, can't fallback further
                        log.warning(f"CTranslate2 type_error on int8 (job {job_id}): {e}")
                        raise
                    # CTranslate2 float16 bug: certain audio triggers
                    # "[json.exception.type_error.302] type must be number, but is number"
                    # during model.generate(). Retry with int8 fallback model.
                    log.warning(f"CTranslate2 type_error (job {job_id}), retrying with int8 fallback")
                    global _fallback_model, _fallback_lock, _fallback_transient_lock
                    if _fallback_lock is None:
                        _fallback_lock = asyncio.Lock()
                    if _fallback_transient_lock is None:
                        _fallback_transient_lock = asyncio.Lock()
                    async with _fallback_lock:
                        if _fallback_model is None:
                            log.info("[Whisper] Loading int8 fallback model...")
                            def _load_int8():
                                return WhisperModel(
                                    WHISPER_MODEL,
                                    device="cuda",
                                    compute_type="int8",
                                )
                            _fallback_model = await asyncio.to_thread(_load_int8)
                            log.info("[Whisper] int8 fallback model loaded")
                    # int8 retry: serialize to be safe, same pattern as fallback.
                    fallback_kwargs = dict(transcribe_kwargs)
                    fallback_kwargs.pop("batch_size", None)
                    async with _fallback_transient_lock:
                        segments, info = await asyncio.to_thread(
                            _fallback_model.transcribe,
                            tmp_path,
                            **fallback_kwargs,
                        )

                text = " ".join([s.text for s in segments])
                lang = info.language
                duration_raw = info.duration
                try:
                    duration_py = float(duration_raw)
                    if duration_py != duration_py or duration_py == float('inf'):
                        duration_py = 0.0
                except Exception:
                    duration_py = 0.0
                results[job_id] = {
                    "ok": True,
                    "text": text,
                    "language": str(lang),
                    "duration": duration_py,
                    "priority": priority,
                }
            except Exception as e:
                results[job_id] = {"ok": False, "error": str(e), "priority": priority}
            finally:
                async with active_lock:
                    active[priority] -= 1
                try:
                    os.unlink(tmp_path)
                except Exception:
                    pass


@app.on_event("startup")
async def load_model():
    global model, _fallback_model
    # Hard cap de VRAM por proceso: protege contra OOM-sistémico en producción.
    # Si se excede, PyTorch lanza RuntimeError en lugar de tirar todo el servidor.
    try:
        torch.cuda.set_per_process_memory_fraction(WHISPER_MEM_FRAC, device=0)
        print(f"[Whisper] VRAM hard cap = {WHISPER_MEM_FRAC*100:.0f}% del total (GPU {GPU_ID}, visible device=0)")
    except Exception as e:
        print(f"[Whisper] WARN: no se pudo fijar VRAM cap: {e}")
    try:
        base = WhisperModel(
            WHISPER_MODEL,
            device="cuda",
            compute_type=WHISPER_COMPUTE,
        )
        if USE_BATCHED and _HAS_BATCHED:
            model = BatchedInferencePipeline(base)
            print(f"[Whisper] BatchedInferencePipeline wrapped (batch={WHISPER_BATCH_SIZE})")
        else:
            model = base
            print(f"[Whisper] Non-batched mode (global_concurrent={_global_concurrent})")
        print(f"[Whisper] Modelo cargado (compute={WHISPER_COMPUTE}, batch={WHISPER_BATCH_SIZE}, workers={WHISPER_HIGH_WORKERS}/{WHISPER_NORMAL_WORKERS})")
    except Exception as e:
        print(f"[Whisper] ERROR cargando modelo ({WHISPER_COMPUTE}), fallback base int8: {e}")
        model = WhisperModel("base", device="cuda", compute_type="int8")

    _fallback_model = None
    if WHISPER_COMPUTE == "float16":
        print(f"[Whisper] int8 fallback: lazy-loaded on first CTranslate2 type_error.302")
    else:
        print(f"[Whisper] Primary model is {WHISPER_COMPUTE} - no fallback needed")
    log.info(f"[Whisper] Active compute_type={WHISPER_COMPUTE}, global_concurrent={_global_concurrent}, mem_frac={WHISPER_MEM_FRAC}")


@app.post("/transcribe")
async def transcribe(
    audio: UploadFile = File(...),
    language: str = "es",
    priority: str = "normal"
):
    global next_id
    if priority not in ("high", "normal"):
        raise HTTPException(status_code=400, detail="priority must be high or normal")

    try:
        orig_name = audio.filename or "audio.webm"
        suffix = ".wav"
        for ext in (".webm", ".mp3", ".mp4", ".m4a", ".ogg", ".oga", ".opus", ".flac", ".wav"):
            if orig_name.lower().endswith(ext):
                suffix = ext
                break
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            content = await audio.read()
            if not content or len(content) < 100:
                raise HTTPException(status_code=400, detail="audio vacío o muy pequeño")
            tmp.write(content)
            tmp_path = tmp.name

        job_id = str(next_id)
        next_id += 1
        queue.put_nowait((job_id, tmp_path, language, priority))
        asyncio.create_task(transcribe_worker(job_id, tmp_path, language, priority))

        while job_id not in results:
            await asyncio.sleep(0.05)

        result = results.pop(job_id)
        if not result["ok"]:
            raise HTTPException(status_code=500, detail=result["error"])

        duration = result.get("duration")
        try:
            duration = float(duration)
            if duration != duration or duration == float('inf'):
                duration = 0.0
        except (TypeError, ValueError):
            duration = 0.0

        return {
            "text": result["text"],
            "language": result["language"],
            "duration": duration,
            "priority": result["priority"],
            "high_workers": WHISPER_HIGH_WORKERS,
            "normal_workers": WHISPER_NORMAL_WORKERS,
        "global_concurrent": _global_concurrent,
            "batch_size": WHISPER_BATCH_SIZE,
            "vram_frac": WHISPER_MEM_FRAC,
            "active_high": active["high"],
            "active_normal": active["normal"],
            "queued": queue.qsize(),
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/health")
async def health():
    return {
        "status": "healthy",
        "model": "whisper-turbo-fp16-vad-batched",
        "gpu": GPU_ID,
        "vram_frac": WHISPER_MEM_FRAC,
        "high_workers": WHISPER_HIGH_WORKERS,
        "normal_workers": WHISPER_NORMAL_WORKERS,
        "global_concurrent": _global_concurrent,
        "batch_size": WHISPER_BATCH_SIZE,
        "active_high": active["high"],
        "active_normal": active["normal"],
        "queued": queue.qsize(),
        "int8_fallback_loaded": _fallback_model is not None,
    }


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8008)
