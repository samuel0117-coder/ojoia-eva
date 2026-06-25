#!/usr/bin/env python3
import io, os, random, threading, time, base64
os.environ["HF_HUB_OFFLINE"] = "0"  # MODIFICADO POR KILO
os.environ["TRANSFORMERS_OFFLINE"] = "1"
os.environ["CUDA_VISIBLE_DEVICES"] = "1"

import torch
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

APP = FastAPI(title="GPU1 Image Server", version="2.0")
APP.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

pipe = None
current_model = "none"
model_lock = threading.RLock()
startup_lock = threading.Lock()
startup_started = False

SDXL_MODELS = {
    "turbo": {
        "name": "SDXL Turbo",
        "ckpt": "/home/sam/ai_system/ComfyUI/models/checkpoints/sd_xl_turbo_1.0_fp16.safetensors",
        "width": 512,
        "height": 512,
        "steps": 4,
        "cfg": 0.0,
    },
    "juggernaut": {
        "name": "Juggernaut XL v10",
        "ckpt": "/home/sam/ai_system/ComfyUI/models/checkpoints/JuggernautXL_v10.safetensors",
        "width": 1024,
        "height": 1024,
        "steps": 30,
        "cfg": 7.0,
    },
}


def load_vae_sdxl(pipe_obj):
    vae_path = "/home/sam/ai_system/ComfyUI/models/vae/sdxl_vae.safetensors"
    if os.path.exists(vae_path):
        from diffusers import AutoencoderKL
        print(f"[gpu1] loading VAE sdxl_vae from {vae_path}", flush=True)
        vae = AutoencoderKL.from_single_file(vae_path, torch_dtype=torch.float16)
        pipe_obj.vae = vae
        print(f"[gpu1] VAE sdxl_vae loaded via AutoencoderKL v2.0", flush=True)
    else:
        print(f"[gpu1] WARNING: VAE sdxl_vae not found at {vae_path}", flush=True)


def unload_pipe():
    global pipe, current_model
    if pipe is not None:
        del pipe
        pipe = None
        torch.cuda.empty_cache()
        time.sleep(2)
    current_model = "none"


def load_sdxl_model(model_key):
    global pipe, current_model
    with model_lock:
        if current_model == model_key and pipe is not None:
            return
        print(f"[gpu1] deferred loading {model_key}", flush=True)
        unload_pipe()
        cfg = SDXL_MODELS[model_key]
        ckpt_path = cfg["ckpt"]
        print(f"[gpu1] loading {model_key} from {ckpt_path}", flush=True)
        from diffusers import StableDiffusionXLPipeline, EulerAncestralDiscreteScheduler
        pipe_obj = StableDiffusionXLPipeline.from_single_file(
            ckpt_path,
            torch_dtype=torch.float16,
            variant="fp16",
            use_safetensors=True,
            local_files_only=True,
        )
        if model_key == "turbo":
            scheduler_config = {
                "beta_end": 0.012,
                "beta_schedule": "scaled_linear",
                "beta_start": 0.00085,
                "clip_sample": False,
                "interpolation_type": "linear",
                "num_train_timesteps": 1000,
                "prediction_type": "epsilon",
                "sample_max_value": 1.0,
                "set_alpha_to_one": False,
                "skip_prk_steps": True,
                "steps_offset": 1,
                "timestep_spacing": "trailing",
                "trained_betas": None,
            }
            pipe_obj.scheduler = EulerAncestralDiscreteScheduler.from_config(scheduler_config)
            print(f"[gpu1] Scheduler: EulerAncestralDiscreteScheduler trailing", flush=True)
            load_vae_sdxl(pipe_obj)
        elif model_key == "juggernaut":
            from diffusers import EulerDiscreteScheduler
            pipe_obj.scheduler = EulerDiscreteScheduler.from_config(pipe_obj.scheduler.config)
        pipe_obj.to("cuda:0")
        pipe_obj.enable_attention_slicing()
        pipe = pipe_obj
        current_model = model_key
        vram = torch.cuda.memory_allocated(0) / 1024**3
        print(f"[gpu1] OK {cfg['name']} loaded | VRAM: {vram:.1f}GB", flush=True)


@APP.on_event("startup")
def startup():
    global startup_started
    with startup_lock:
        if startup_started:
            return
        startup_started = True
    def load_model():
        try:
            load_sdxl_model("turbo")
        except Exception as e:
            import traceback
            print(f"[gpu1] ERROR loading model: {e}", flush=True)
            traceback.print_exc()
    threading.Thread(target=load_model, daemon=True).start()


@APP.on_event("shutdown")
def shutdown():
    unload_pipe()


@APP.post("/api/sdxl/unload")
async def unload_sdxl():
    with model_lock:
        unload_pipe()
    return {"success": True, "loaded": False, "model": "none"}


@APP.get("/api/sdxl/status")
async def sdxl_status():
    gpu_mem = round(torch.cuda.memory_allocated() / 1024**3, 2) if torch.cuda.is_available() else 0
    return {
        "loaded": pipe is not None,
        "model": current_model,
        "mode": "deferred",
        "gpu1_mem_gb": gpu_mem,
    }


@APP.post("/api/sdxl/generate")
def generate(req: dict):
    model = req.get("model", "turbo")
    if model not in SDXL_MODELS:
        return JSONResponse({"success": False, "error": f"model invalido: {model}"}, status_code=400)
    prompt = req.get("prompt", "")
    if not prompt.strip():
        return JSONResponse({"success": False, "error": "prompt vacio"}, status_code=400)
    try:
        cfg = SDXL_MODELS[model]
        seed = int(req.get("seed", -1)) if int(req.get("seed", -1)) > 0 else random.randint(1, 2**31)
        width = int(req.get("width", cfg["width"]))
        height = int(req.get("height", cfg["height"]))
        steps = int(req.get("steps", cfg["steps"]))
        guidance = float(req.get("cfg", cfg["cfg"]))
        if model == "turbo":
            guidance = 0.0
        negative = req.get("negative_prompt", "blurry, bad quality, low quality")
        result = generate_sdxl_blocking(model, prompt, negative, width, height, steps, guidance, seed)
        img = result.images[0]
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return JSONResponse({
            "success": True,
            "image_b64": base64.b64encode(buf.getvalue()).decode("utf-8"),
            "model": current_model,
            "mode": "deferred",
            "seed": seed,
            "width": width,
            "height": height,
        })
    except Exception as e:
        import traceback
        traceback.print_exc()
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)


def generate_sdxl_blocking(model, prompt, negative, width, height, steps, guidance, seed):
    with model_lock:
        load_sdxl_model(model)
        gen = torch.Generator("cuda:0").manual_seed(seed)
        return pipe(
            prompt=prompt,
            negative_prompt=negative,
            width=width,
            height=height,
            num_inference_steps=steps,
            guidance_scale=guidance,
            generator=gen,
        )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(APP, host="0.0.0.0", port=8015)
