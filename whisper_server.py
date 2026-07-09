#!/usr/bin/env python3
"""Whisper Turbo INT8 ASR server - GPU 1 :8008"""
import asyncio
import os
os.environ["HF_HOME"] = "/home/sam/.cache/hf_models"
os.environ["HF_HUB_OFFLINE"] = "0"
os.environ["CUDA_VISIBLE_DEVICES"] = "1"

import torch
from faster_whisper import WhisperModel
import uvicorn
from fastapi import FastAPI, UploadFile, File, HTTPException
import tempfile
from fastapi.middleware.cors import CORSMiddleware

WHISPER_HIGH_WORKERS = 2
WHISPER_NORMAL_WORKERS = 2

app = FastAPI(title="Whisper Turbo ASR", version="1.2")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

model = None
queue = asyncio.Queue()
results = {}
next_id = 1
active = {"high": 0, "normal": 0}
high_semaphore = asyncio.Semaphore(WHISPER_HIGH_WORKERS)
normal_semaphore = asyncio.Semaphore(WHISPER_NORMAL_WORKERS)
active_lock = asyncio.Lock()


async def transcribe_worker(job_id, tmp_path, language, priority):
    global active
    semaphore = high_semaphore if priority == "high" else normal_semaphore
    async with semaphore:
        async with active_lock:
            active[priority] += 1
        try:
            segments, info = await asyncio.to_thread(
                model.transcribe,
                tmp_path,
                language=language
            )
            text = " ".join([s.text for s in segments])
            # Conversión explícita a tipos Python nativos (CTranslate2 a veces devuelve objetos nativos)
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
                "duration": duration_py,  # float Python nativo
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
    try:
        model = WhisperModel(
            "openai/whisper-large-v3-turbo",
            device="cuda",
            compute_type="int8"
        )
    except Exception as e:
        print(f"Error cargando modelo: {e}")
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
        # Usar el sufijo correcto según el archivo entrante (Whisper detecta formato por extension)
        orig_name = audio.filename or "audio.webm"
        suffix = ".wav"
        for ext in (".webm", ".mp3", ".mp4", ".m4a", ".ogg", ".oga", ".opus", ".flac", ".wav"):
            if orig_name.lower().endswith(ext):
                suffix = ext
                break
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            content = await audio.read()
            tmp.write(content)
            tmp_path = tmp.name

        job_id = str(next_id)
        next_id += 1
        queue.put_nowait((job_id, tmp_path, language, priority))
        asyncio.create_task(transcribe_worker(job_id, tmp_path, language, priority))

        while job_id not in results:
            await asyncio.sleep(0.2)

        result = results.pop(job_id)
        if not result["ok"]:
            raise HTTPException(status_code=500, detail=result["error"])

        # Sanitizar duration (puede ser NaN si Whisper no detecta el formato correctamente)
        duration = result.get("duration")
        try:
            duration = float(duration)
            if duration != duration or duration == float('inf'):  # NaN or inf
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
        "model": "whisper-turbo-int8",
        "high_workers": WHISPER_HIGH_WORKERS,
        "normal_workers": WHISPER_NORMAL_WORKERS,
        "active_high": active["high"],
        "active_normal": active["normal"],
        "queued": queue.qsize(),
    }


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8008)
