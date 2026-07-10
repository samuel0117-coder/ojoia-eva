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
import uvicorn
from fastapi import FastAPI, UploadFile, File, HTTPException
import tempfile
from fastapi.middleware.cors import CORSMiddleware

WHISPER_HIGH_WORKERS = int(os.getenv("WHISPER_HIGH_WORKERS", "2"))
WHISPER_NORMAL_WORKERS = int(os.getenv("WHISPER_NORMAL_WORKERS", "2"))
WHISPER_GLOBAL_CONCURRENT = int(os.getenv("WHISPER_GLOBAL_CONCURRENT", "2"))
WHISPER_BATCH_SIZE = int(os.getenv("WHISPER_BATCH_SIZE", "4"))
WHISPER_MEM_FRAC = float(os.getenv("WHISPER_MEM_FRAC", "0.15"))

app = FastAPI(title="Whisper Turbo ASR", version="2.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

model = None
queue = asyncio.Queue()
results = {}
next_id = 1
active = {"high": 0, "normal": 0}
high_semaphore = asyncio.Semaphore(WHISPER_HIGH_WORKERS)
normal_semaphore = asyncio.Semaphore(WHISPER_NORMAL_WORKERS)
# Global cap prevents VRAM spikes when many audios arrive at once.
# Combined with VRAM hard cap (WHISPER_MEM_FRAC), bursts above this cap
# will queue in `queue` rather than contending for GPU memory.
global_semaphore = asyncio.Semaphore(WHISPER_GLOBAL_CONCURRENT)
active_lock = asyncio.Lock()


async def transcribe_worker(job_id, tmp_path, language, priority):
    global active
    semaphore = high_semaphore if priority == "high" else normal_semaphore
    async with global_semaphore:        # ← NEW: global anti-pico
        async with semaphore:
            async with active_lock:
                active[priority] += 1
            try:
                segments, info = await asyncio.to_thread(
                    model.transcribe,
                    tmp_path,
                    language=language,
                    task="transcribe",
                    batch_size=WHISPER_BATCH_SIZE,   # ← NEW: segment-level batching
                    vad_filter=True,                # ← NEW: Silero VAD
                    vad_parameters=dict(
                        threshold=0.35,
                        min_speech_duration_ms=150,
                        max_speech_duration_s=20,
                    ),
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
    global model
    # Hard cap de VRAM por proceso: protege contra OOM-sistémico en producción.
    # Si se excede, PyTorch lanza RuntimeError en lugar de tirar todo el servidor.
    try:
        torch.cuda.set_per_process_memory_fraction(WHISPER_MEM_FRAC, device=0)
        print(f"[Whisper] VRAM hard cap = {WHISPER_MEM_FRAC*100:.0f}% del total (GPU {GPU_ID}, visible device=0)")
    except Exception as e:
        print(f"[Whisper] WARN: no se pudo fijar VRAM cap: {e}")
    try:
        model = WhisperModel(
            "openai/whisper-large-v3-turbo",
            device="cuda",
            compute_type="float16",     # ← FP16: ~2x throughput vs int8
            vad_filter=True,            # ← Silero VAD builtin
            vad_parameters=dict(
                threshold=0.35,
                min_speech_duration_ms=150,
                max_speech_duration_s=20,
            ),
        )
        print(f"[Whisper] Modelo cargado (float16 + VAD, batch={WHISPER_BATCH_SIZE}, workers={WHISPER_HIGH_WORKERS}/{WHISPER_NORMAL_WORKERS}, global_cap={WHISPER_GLOBAL_CONCURRENT})")
    except Exception as e:
        print(f"[Whisper] ERROR cargando modelo, fallback 'base' int8: {e}")
        model = WhisperModel("base", device="cuda", compute_type="int8")


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
            "global_concurrent": WHISPER_GLOBAL_CONCURRENT,
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
        "global_concurrent": WHISPER_GLOBAL_CONCURRENT,
        "batch_size": WHISPER_BATCH_SIZE,
        "active_high": active["high"],
        "active_normal": active["normal"],
        "queued": queue.qsize(),
    }


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8008)
