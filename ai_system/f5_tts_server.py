#!/usr/bin/env python3
import io
import os
import tempfile
import threading
import time
import subprocess

import numpy as np
import soundfile as sf
import torch
import uvicorn
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from pydantic import BaseModel

os.environ["CUDA_VISIBLE_DEVICES"] = "2"
os.environ["HF_HOME"] = "/home/sam/.cache/hf_models"
os.environ["TOKENIZERS_PARALLELISM"] = "false"

APP = FastAPI(title="F5-TTS Server", version="1.0")
APP.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

DEFAULT_REF_AUDIO = "/home/sam/ai_system/venv_f5tts/lib/python3.12/site-packages/f5_tts/infer/examples/basic/basic_ref_en.wav"
DEFAULT_REF_TEXT = "Some call me nature, others call me mother nature."
MODEL_NAME = os.getenv("F5_MODEL", "F5TTS_v1_Base")
DEVICE = os.getenv("F5_DEVICE", "cuda")

tts = None
load_lock = threading.RLock()
current_model = None


class TTSRequest(BaseModel):
    input: str
    voice: str = "default"
    speed: float = 1.0
    duration: float | None = None
    nfe_step: int = 16
    cfg: float = 2.0
    remove_silence: bool = False


def nvidia_smi_gpu2():
    try:
        result = subprocess.run(
            ["nvidia-smi", "-i", "2", "--query-gpu=memory.total,memory.used,memory.free", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        parts = [int(x.strip()) for x in result.stdout.strip().split(",")]
        if len(parts) == 3:
            return {
                "total_mb": parts[0],
                "used_mb": parts[1],
                "free_mb": parts[2],
                "used_gb": round(parts[1] / 1024, 2),
                "free_gb": round(parts[2] / 1024, 2),
            }
    except Exception as e:
        return {"error": str(e)}


def get_tts():
    global tts, current_model
    if tts is not None:
        return tts
    with load_lock:
        if tts is not None:
            return tts
        from f5_tts.api import F5TTS
        print("[f5-tts] loading model", MODEL_NAME, flush=True)
        tts = F5TTS(model=MODEL_NAME, device=DEVICE, hf_cache_dir="/home/sam/.cache/hf_models")
        current_model = MODEL_NAME
        print(f"[f5-tts] loaded | VRAM: {torch.cuda.memory_allocated() / 1e9:.2f} GB", flush=True)
        return tts


def unload_tts():
    global tts, current_model
    with load_lock:
        if tts is None:
            return
        del tts
        tts = None
        current_model = None
        torch.cuda.empty_cache()
        time.sleep(2)
        print("[f5-tts] unloaded", flush=True)


def wav_to_bytes(wav, sr):
    buf = io.BytesIO()
    sf.write(buf, np.asarray(wav, dtype=np.float32), sr, format="WAV")
    buf.seek(0)
    return buf.read()


@APP.get("/health")
async def health():
    return {
        "status": "ok",
        "loaded": tts is not None,
        "model": current_model,
        "vram": nvidia_smi_gpu2(),
    }


@APP.post("/api/tts/load")
async def load():
    try:
        get_tts()
        return {"status": "loaded", "model": current_model, "vram": nvidia_smi_gpu2()}
    except Exception as e:
        return {"status": "error", "error": str(e)[:500]}


@APP.post("/api/tts/unload")
async def unload():
    unload_tts()
    return {"status": "unloaded", "model": current_model, "vram": nvidia_smi_gpu2()}


@APP.post("/api/tts/generate")
async def generate(req: TTSRequest):
    text = req.input.strip()
    if not text:
        raise HTTPException(status_code=400, detail="texto vacío")
    try:
        model = get_tts()
        result = model.infer(
            ref_file=DEFAULT_REF_AUDIO,
            ref_text=DEFAULT_REF_TEXT,
            gen_text=text,
            nfe_step=req.nfe_step,
            cfg_strength=req.cfg,
            speed=req.speed,
            fix_duration=req.duration,
            remove_silence=req.remove_silence,
        )
        wav, sr = result[0], int(result[1])
        content = wav_to_bytes(wav, sr)
        return Response(content=content, media_type="audio/wav", headers={"X-Model": current_model or MODEL_NAME})
    except Exception as e:
        print(f"[f5-tts] generate error: {e}", flush=True)
        raise HTTPException(status_code=500, detail=str(e)[:500])


@APP.post("/api/tts/clone")
async def clone(
    audio: UploadFile = File(...),
    text: str = Form(...),
    speed: float = Form(1.0),
    duration: float | None = Form(None),
    nfe_step: int = Form(16),
    cfg: float = Form(2.0),
    remove_silence: bool = Form(False),
):
    if not text.strip():
        raise HTTPException(status_code=400, detail="texto vacío")
    try:
        suffix = os.path.splitext(audio.filename or "ref.wav")[1] or ".wav"
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            tmp.write(await audio.read())
            ref_path = tmp.name
        model = get_tts()
        result = model.infer(
            ref_file=ref_path,
            ref_text=text.strip(),
            gen_text=text.strip(),
            nfe_step=nfe_step,
            cfg_strength=cfg,
            speed=speed,
            fix_duration=duration,
            remove_silence=remove_silence,
        )
        os.unlink(ref_path)
        wav, sr = result[0], int(result[1])
        content = wav_to_bytes(wav, sr)
        return Response(content=content, media_type="audio/wav", headers={"X-Model": current_model or MODEL_NAME})
    except Exception as e:
        print(f"[f5-tts] clone error: {e}", flush=True)
        raise HTTPException(status_code=500, detail=str(e)[:500])


@APP.post("/api/tts/clone_text")
async def clone_text(
    audio: UploadFile = File(...),
    ref_text: str = Form(...),
    input: str = Form(...),
    speed: float = Form(1.0),
    duration: float | None = Form(None),
    nfe_step: int = Form(16),
    cfg: float = Form(2.0),
    remove_silence: bool = Form(False),
):
    text = (input or "").strip()
    ref = (ref_text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="texto de salida vacío")
    if not ref:
        raise HTTPException(status_code=400, detail="ref_text requerido")
    ref_path = None
    try:
        suffix = os.path.splitext(audio.filename or "ref.wav")[1] or ".wav"
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            tmp.write(await audio.read())
            ref_path = tmp.name
        model = get_tts()
        result = model.infer(
            ref_file=ref_path,
            ref_text=ref,
            gen_text=text,
            nfe_step=nfe_step,
            cfg_strength=cfg,
            speed=speed,
            fix_duration=duration,
            remove_silence=remove_silence,
        )
        wav, sr = result[0], int(result[1])
        content = wav_to_bytes(wav, sr)
        return Response(content=content, media_type="audio/wav", headers={"X-Model": current_model or MODEL_NAME})
    except Exception as e:
        print(f"[f5-tts] clone_text error: {e}", flush=True)
        raise HTTPException(status_code=500, detail=str(e)[:500])
    finally:
        if ref_path and os.path.exists(ref_path):
            os.unlink(ref_path)


if __name__ == "__main__":
    uvicorn.run(APP, host="0.0.0.0", port=8017, log_level="warning")
