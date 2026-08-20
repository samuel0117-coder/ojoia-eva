#!/usr/bin/env python3
"""Flux Server v1.0 - Generación de imágenes de alta calidad
Usa diffusers con Flux.1-dev para producción cinematográfica.
"""
import os, sys, time, threading, io, base64, random
os.environ["HF_HOME"] = "/home/sam/.cache/hf_models"
os.environ["TORCH_COMPILE_DISABLE"] = "1"
os.environ["CUDA_VISIBLE_DEVICES"] = "2"

import torch
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Optional

APP = FastAPI(title="Flux GPU2", version="1.0")
APP.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

pipe = None
lock = threading.Lock()

class FluxReq(BaseModel):
    prompt: str
    width: int = 1024
    height: int = 1024
    steps: int = 25
    cfg: float = 4.0
    seed: int = -1
    negative_prompt: str = ""

def load_pipe():
    global pipe
    print("[Flux] Cargando pipeline...")
    from diffusers import FluxPipeline
    pipe = FluxPipeline.from_pretrained(
        "black-forest-labs/FLUX.1-dev",
        torch_dtype=torch.bfloat16,
        use_safetensors=True
    )
    pipe.enable_model_cpu_offload()  # Optimiza VRAM
    print("[Flux] Pipeline cargado")

@APP.on_event("startup")
async def startup():
    try:
        load_pipe()
    except Exception as e:
        print(f"[Flux] Error: {e}")
        import traceback; traceback.print_exc()

@APP.post("/api/flux/generate")
async def flux_generate(req: FluxReq):
    global pipe
    if pipe is None:
        return JSONResponse({"success": False, "error": "Flux no cargado"}, 503)
    try:
        seed = req.seed if req.seed > 0 else random.randint(1, 2**31)
        gen = torch.Generator("cuda:2").manual_seed(seed)
        with lock:
            result = pipe(
                prompt=req.prompt,
                width=req.width,
                height=req.height,
                num_inference_steps=req.steps,
                guidance_scale=req.cfg,
                generator=gen,
                max_sequence_length=256
            )
        img = result.images[0]
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return JSONResponse({
            "success": True,
            "image_b64": base64.b64encode(buf.getvalue()).decode(),
            "seed": seed,
            "width": req.width,
            "height": req.height
        })
    except Exception as e:
        import traceback; traceback.print_exc()
        return JSONResponse({"success": False, "error": str(e)}, 500)

@APP.get("/api/flux/status")
async def flux_status():
    gpu_mem = 0
    try:
        import subprocess
        r = subprocess.run(["nvidia-smi","-i","2","--query-gpu=memory.used","--format=noheader,nounits"], capture_output=True, text=True, timeout=5)
        gpu_mem = int(r.stdout.strip())
    except: pass
    return {"loaded": pipe is not None, "gpu2_mem_mb": gpu_mem}

@APP.get("/health")
async def health():
    return {"status": "healthy", "model": "flux", "loaded": pipe is not None}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(APP, host="0.0.0.0", port=8013, log_level="warning")
