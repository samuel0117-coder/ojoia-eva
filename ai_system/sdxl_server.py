#!/usr/bin/env python3
"""SDXL Server v6.0 - Genera imagenes SDXL Turbo/JuggernautXL"""
import os, sys, time, threading, io, base64, random
os.environ["HF_HOME"] = "/home/sam/.cache/hf_models"
os.environ["TORCH_COMPILE_DISABLE"] = "1"
os.environ["CUDA_VISIBLE_DEVICES"] = "1"

import torch
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

APP = FastAPI(title="SDXL GPU1", version="6.0")
APP.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

pipe = None
current_model = "none"
model_lock = threading.Lock()

SDXL_MODELS = {
    "turbo": {
        "name": "SDXL Turbo",
        "ckpt": "/home/sam/ai_system/ComfyUI/models/checkpoints/sd_xl_turbo_1.0_fp16.safetensors",
        "steps": 4, "cfg": 1.5, "width": 512, "height": 512,
    },
    "juggernaut": {
        "name": "JuggernautXL v10",
        "ckpt": "/home/sam/ai_system/ComfyUI/models/checkpoints/JuggernautXL_v10.safetensors",
        "steps": 30, "cfg": 5.5, "width": 1024, "height": 1024,
    },
}

class GenReq(BaseModel):
    prompt: str
    negative_prompt: str = "blurry, bad quality"
    width: int = -1
    height: int = -1
    steps: int = -1
    cfg: float = -1.0
    seed: int = -1
    model: str = "turbo"

def load_model(model_key):
    global pipe, current_model
    with model_lock:
        if pipe is not None:
            del pipe; pipe = None
            torch.cuda.empty_cache(); time.sleep(2)
        cfg = SDXL_MODELS[model_key]
        ckpt_path = cfg["ckpt"]
        print(f"[SDXL] Cargando {model_key}...")
        from diffusers import StableDiffusionXLPipeline
        from safetensors.torch import load_file as load_safetensors
        pipe = StableDiffusionXLPipeline.from_pretrained(
            "stabilityai/stable-diffusion-xl-base-1.0",
            torch_dtype=torch.float16, variant="fp16", use_safetensors=True)
        state = load_safetensors(ckpt_path, device="cpu")
        unet_sd = {k.replace("unet.","",1): v for k,v in state.items() if k.startswith("unet.")}
        vae_sd = {k.replace("vae.","",1): v for k,v in state.items() if k.startswith("vae.")}
        pipe.unet.load_state_dict(unet_sd, strict=False)
        pipe.vae.load_state_dict(vae_sd, strict=False)
        pipe.vae.config.force_upcast = True
        del state; torch.cuda.empty_cache()
        pipe.to("cuda:0")
        current_model = model_key
        vram = torch.cuda.memory_allocated()/1024**3
        print(f"[SDXL] OK {cfg['name']} | VRAM: {vram:.1f}GB")

# Cargar modelo al inicio (fuera de async)
print("[SDXL] Iniciando carga de modelo...")
try:
    load_model("turbo")
    print("[SDXL] Modelo cargado correctamente")
except Exception as e:
    print(f"[SDXL] Error cargando modelo: {e}")
    import traceback; traceback.print_exc()

@APP.post("/api/sdxl/generate")
async def generate(req: GenReq):
    global pipe, current_model
    if pipe is None:
        return JSONResponse({"error":"SDXL no cargado"},503)
    try:
        cfg_info = SDXL_MODELS.get(req.model if req.model in SDXL_MODELS else current_model, SDXL_MODELS["turbo"])
        w = req.width if req.width>0 else cfg_info["width"]
        h = req.height if req.height>0 else cfg_info["height"]
        s = req.steps if req.steps>0 else cfg_info["steps"]
        c = req.cfg if req.cfg>0 else cfg_info["cfg"]
        seed = req.seed if req.seed>0 else random.randint(1,2**31)
        gen = torch.Generator("cuda:0").manual_seed(seed)
        with model_lock:
            result = pipe(prompt=req.prompt, negative_prompt=req.negative_prompt,
                width=w, height=h, num_inference_steps=s, guidance_scale=c, generator=gen)
        img = result.images[0]; buf = io.BytesIO(); img.save(buf, format="PNG")
        return JSONResponse({"success":True,"image_b64":base64.b64encode(buf.getvalue()).decode(),
            "model":current_model,"seed":seed,"width":w,"height":h})
    except Exception as e:
        import traceback; traceback.print_exc()
        return JSONResponse({"error":str(e)},500)

@APP.get("/api/sdxl/status")
async def sdxl_status():
    gpu_mem = torch.cuda.memory_allocated()/1024**3 if pipe else 0
    return {"loaded":pipe is not None,"model":current_model,"gpu_mem_gb":round(gpu_mem,2)}

@APP.post("/api/sdxl/switch")
async def switch(req: dict):
    model = req.get("model","turbo")
    if model not in SDXL_MODELS:
        return JSONResponse({"error":f"Modelo invalido: {model}"},400)
    try:
        load_model(model)
        return JSONResponse({"success":True,"model":model})
    except Exception as e:
        return JSONResponse({"error":str(e)},500)

@APP.get("/health")
async def health():
    return {"status":"healthy","model":current_model,"loaded":pipe is not None}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(APP, host="0.0.0.0", port=8011, log_level="warning")
