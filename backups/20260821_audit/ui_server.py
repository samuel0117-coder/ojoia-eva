"""
ui_server.py — Sirve test_ui.html desde la red y proxyea las APIs
Escucha en 0.0.0.0:8090

Endpoints:
  GET  /                    → test_ui.html
  GET  /health/services     → health de todos los servicios
  POST /api/sdxl/generate   → Genera imagen (traduce a ComfyUI workflow)
  GET  /api/sdxl/models     → Lista modelos SDXL disponibles
  POST /api/audioldm2/generate → Genera audio desde texto
  POST /api/voxtral/v1/audio/speech → TTS (traduce formato)
  POST /api/voxtral/v1/audio/clone  → Clonación de voz
  *    /api/whisper/*       → Proxy directo → :8008
  *    /api/flux/*          → Proxy directo → :8007
  *    /api/qwen/*          → Proxy directo → :8004
  *    /api/comfyui/*       → Proxy directo → :8080 (ComfyUI)
"""

import json, random, threading, time, asyncio
import httpx, uvicorn
from comfyui_models import register_comfyui_endpoints
from pathlib import Path
from fastapi import FastAPI, Request, Response, File, UploadFile, Form
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware

APP = FastAPI(title="UI Proxy", version="2.0.0")
register_comfyui_endpoints(APP)

APP.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Requested-With", "Accept", "Origin", "User-Agent", "Cache-Control", "Keep-Alive", "Pragma"],
    expose_headers=["*"],
)

# ── Configuración backends ──
_ComfyUI = "http://localhost:8006"
_AUDIO = "http://localhost:8009"
_VOXTRAL = "http://localhost:8010"
_BACKENDS = {
    "whisper":   "http://localhost:8008",
    "sdxl":      _ComfyUI,
    "voxtral":   _VOXTRAL,
    "audioldm2": _AUDIO,
    "qwen":      "http://localhost:8004",
    "flux":      _ComfyUI,
    "comfyui":   _ComfyUI,
}

_SDXL_MODELS = {
    "turbo":      "sd_xl_turbo_1.0_fp16.safetensors",
    "juggernaut": "JuggernautXL_v10.safetensors",
}
_SDXL_DEFAULTS = {
    "turbo":      {"steps": 2,  "cfg": 1.5, "width": 512,  "height": 512,  "sampler": "euler_ancestral", "scheduler": "normal"},
    "juggernaut": {"steps": 30, "cfg": 5.5, "width": 1344, "height": 768,  "sampler": "dpmpp_2m",       "scheduler": "karras"},
}


# ═══════════════════════════════════════════════════════════════════════════
#  SDXL /generate  →  ComfyUI workflow
# ═══════════════════════════════════════════════════════════════════════════
@APP.post("/api/sdxl/generate")
async def sdxl_generate(request: Request):
    body = await request.json()
    prompt = body.get("prompt", "")
    if not prompt.strip():
        return JSONResponse({"error": "prompt vacío"}, status_code=400)

    model_type = body.get("model", "turbo")
    if model_type not in _SDXL_MODELS:
        return JSONResponse({"error": f"modelo '{model_type}' desconocido"}, status_code=400)

    defaults = _SDXL_DEFAULTS[model_type]
    steps = body.get("steps", defaults["steps"])
    cfg = body.get("guidance_scale", 0) or defaults["cfg"]
    width = body.get("width", defaults["width"])
    height = body.get("height", defaults["height"])
    seed = body.get("seed", random.randint(1, 2**31))
    negative = body.get("negative_prompt", "blurry, bad quality")

    workflow = {
        "prompt": {
            "3": {"class_type": "KSampler", "inputs": {"seed": seed, "steps": steps, "cfg": cfg, "sampler_name": defaults["sampler"], "scheduler": defaults["scheduler"], "denoise": 1, "model": ["4", 0], "positive": ["6", 0], "negative": ["7", 0], "latent_image": ["5", 0]}},
            "4": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": _SDXL_MODELS[model_type]}},
            "5": {"class_type": "EmptyLatentImage", "inputs": {"width": width, "height": height, "batch_size": 1}},
            "6": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["4", 1]}},
            "7": {"class_type": "CLIPTextEncode", "inputs": {"text": negative, "clip": ["4", 1]}},
            "8": {"class_type": "VAEDecode", "inputs": {"samples": ["3", 0], "vae": ["4", 2]}},
            "9": {"class_type": "SaveImage", "inputs": {"filename_prefix": "ui_gen", "images": ["8", 0]}}
        }
    }

    try:
        async with httpx.AsyncClient(timeout=300) as client:
            resp = await client.post(f"{_ComfyUI}/prompt", json=workflow)
            resp.raise_for_status()
            prompt_result = resp.json()
            prompt_id = prompt_result.get("prompt_id")

            # Esperar a que la imagen se genere (poll history)
            img_filename = None
            for _ in range(120):
                await asyncio.sleep(1)
                hist = await client.get(f"{_ComfyUI}/history/{prompt_id}")
                hist_data = hist.json()
                if prompt_id in hist_data:
                    outputs = hist_data[prompt_id].get("outputs", {})
                    for node_id, node_out in outputs.items():
                        images = node_out.get("images", [])
                        if images:
                            img_filename = images[0].get("filename")
                            break
                    if img_filename:
                        break

            if not img_filename:
                return JSONResponse({"error": "timeout esperando imagen"}, status_code=504)

            img_resp = await client.get(f"{_ComfyUI}/view", params={"filename": img_filename})
            return Response(content=img_resp.content, media_type="image/png", headers={"X-Seed": str(seed), "X-Model": model_type})

    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@APP.get("/api/sdxl/models")
async def sdxl_models():
    return {"models": {k: {"ckpt": _SDXL_MODELS[k], **_SDXL_DEFAULTS[k]} for k in _SDXL_MODELS}}


# ═══════════════════════════════════════════════════════════════════════════
#  Flux GGUF /generate — acepta prompt, devuelve prompt_id para polling
# ═══════════════════════════════════════════════════════════════════════════
_flux_prompts = {}  # prompt_id → status

@APP.post("/api/flux/generate")
async def flux_generate(request: Request):
    body = await request.json()
    prompt = body.get("prompt", "")
    if not prompt.strip():
        return JSONResponse({"error": "prompt vacío"}, status_code=400)

    width = body.get("width", 768)
    height = body.get("height", 768)
    steps = body.get("num_inference_steps", 20)
    cfg = body.get("guidance_scale", 1.0)
    seed = body.get("seed", random.randint(1, 2**31))

    workflow = {
        "prompt": {
            "1": {"class_type": "UnetLoaderGGUF", "inputs": {"unet_name": "flux1-dev-Q5_K_S.gguf"}},
            "2": {"class_type": "DualCLIPLoaderGGUF", "inputs": {"clip_name1": "flux-clip.safetensors", "clip_name2": "t5-v1_1-xxl-encoder-Q5_K_S.gguf", "type": "flux"}},
            "3": {"class_type": "VAELoader", "inputs": {"vae_name": "flux-vae.safetensors"}},
            "4": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["2", 0], "text": prompt}},
            "5": {"class_type": "EmptyLatentImage", "inputs": {"width": width, "height": height, "batch_size": 1}},
            "6": {"class_type": "KSampler", "inputs": {"seed": seed, "steps": steps, "cfg": cfg, "sampler_name": "euler", "scheduler": "simple", "denoise": 1.0, "model": ["1", 0], "positive": ["4", 0], "negative": ["7", 0], "latent_image": ["5", 0]}},
            "7": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["2", 0], "text": ""}},
            "8": {"class_type": "VAEDecode", "inputs": {"samples": ["6", 0], "vae": ["3", 0]}},
            "9": {"class_type": "SaveImage", "inputs": {"filename_prefix": "flux_gen", "images": ["8", 0]}}
        }
    }

    try:
        async with httpx.AsyncClient(timeout=300) as client:
            resp = await client.post(f"{_ComfyUI}/prompt", json=workflow)
            if resp.status_code >= 400:
                return JSONResponse({"error": f"ComfyUI error {resp.status_code}: {resp.text[:200]}"}, status_code=502)
            result = resp.json()
            prompt_id = result.get("prompt_id")
            if not prompt_id:
                return JSONResponse({"error": f"Sin prompt_id: {result}"}, status_code=502)
            
            _flux_prompts[prompt_id] = {"status": "processing", "seed": seed}
            return JSONResponse({"prompt_id": prompt_id, "status": "processing", "poll_url": f"/api/flux/status/{prompt_id}"})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@APP.get("/api/flux/status/{prompt_id}")
async def flux_status(prompt_id: str):
    """Polling: verifica si la imagen de Flux está lista"""
    if prompt_id not in _flux_prompts:
        return JSONResponse({"error": "prompt_id no encontrado"}, status_code=404)
    
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            hist = await client.get(f"{_ComfyUI}/history/{prompt_id}")
            hist_data = hist.json()
            if prompt_id in hist_data:
                outputs = hist_data[prompt_id].get("outputs", {})
                for node_id, node_out in outputs.items():
                    images = node_out.get("images", [])
                    if images:
                        img_filename = images[0].get("filename")
                        img_resp = await client.get(f"{_ComfyUI}/view", params={"filename": img_filename})
                        _flux_prompts[prompt_id]["status"] = "done"
                        return Response(content=img_resp.content, media_type="image/png")
    except Exception:
        pass
    
    return JSONResponse({"status": "processing", "prompt_id": prompt_id})

# ═══════════════════════════════════════════════════════════════════════════
#  SDXL /generate — usa el pipe cargado en GPU 1
# ═══════════════════════════════════════════════════════════════════════════
@APP.post("/api/sdxl/generate")
async def sdxl_generate(request: Request):
    body = await request.json()
    prompt = body.get("prompt", "")
    if not prompt.strip():
        return JSONResponse({"error": "prompt vacío"}, status_code=400)
    
    width = body.get("width", 768)
    height = body.get("height", 768)
    steps = body.get("num_inference_steps", 4)
    cfg = body.get("guidance_scale", 1.5)
    negative = body.get("negative_prompt", "blurry, bad quality")
    model = body.get("model", "turbo")  # turbo o juggernaut

    # Elegir checkpoint según modelo
    if model == "juggernaut":
        ckpt = "JuggernautXL_v10.safetensors"
        steps = max(steps, 25)
        cfg = max(cfg, 5.0)
    else:
        ckpt = "sd_xl_turbo_1.0_fp16.safetensors"
        steps = min(steps, 8)
        cfg = min(cfg, 2.0)

    workflow = {
        "prompt": {
            "3": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": ckpt}},
            "4": {"class_type": "EmptyLatentImage", "inputs": {"width": width, "height": height, "batch_size": 1}},
            "5": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["3", 1]}},
            "6": {"class_type": "CLIPTextEncode", "inputs": {"text": negative, "clip": ["3", 1]}},
            "7": {"class_type": "KSampler", "inputs": {"seed": random.randint(1, 2**31), "steps": steps, "cfg": cfg, "sampler_name": "euler", "scheduler": "normal", "denoise": 1.0, "model": ["3", 0], "positive": ["5", 0], "negative": ["6", 0], "latent_image": ["4", 0]}},
            "8": {"class_type": "VAEDecode", "inputs": {"samples": ["7", 0], "vae": ["3", 2]}},
            "9": {"class_type": "SaveImage", "inputs": {"filename_prefix": "sdxl_gen", "images": ["8", 0]}}
        }
    }

    try:
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(f"{_ComfyUI}/prompt", json=workflow)
            if resp.status_code >= 400:
                return JSONResponse({"error": f"ComfyUI error: {resp.text[:200]}"}, status_code=502)
            result = resp.json()
            prompt_id = result.get("prompt_id")
            if not prompt_id:
                return JSONResponse({"error": str(result)}, status_code=502)

            # Poll hasta 120s
            for _ in range(60):
                await asyncio.sleep(2)
                try:
                    hist = await client.get(f"{_ComfyUI}/history/{prompt_id}")
                    hist_data = hist.json()
                    if prompt_id in hist_data:
                        outputs = hist_data[prompt_id].get("outputs", {})
                        for node_out in outputs.values():
                            images = node_out.get("images", [])
                            if images:
                                img_filename = images[0].get("filename")
                                img_resp = await client.get(f"{_ComfyUI}/view", params={"filename": img_filename})
                                return Response(content=img_resp.content, media_type="image/png")
                except Exception:
                    continue

            return JSONResponse({"error": "timeout SDXL"}, status_code=504)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@APP.get("/api/sdxl/models")
async def sdxl_models():
    return {
        "models": {
            "turbo": {"ckpt": "sd_xl_turbo_1.0_fp16.safetensors", "steps": 4, "cfg": 1.5, "vram_gb": 6.5},
            "juggernaut": {"ckpt": "JuggernautXL_v10.safetensors", "steps": 30, "cfg": 5.5, "vram_gb": 6.7}
        }
    }

@APP.post("/api/sdxl/switch")
async def sdxl_switch(request: Request):
    """Cambiar modelo SDXL: turbo o juggernaut"""
    body = await request.json()
    model = body.get("model", "turbo")
    if model not in ["turbo", "juggernaut"]:
        return JSONResponse({"error": "modelo debe ser 'turbo' o 'juggernaut'"}, 400)
    
    try:
        import subprocess
        result = subprocess.run(
            ["/home/sam/ai_system/scripts/switch_sdxl.sh", model],
            capture_output=True, text=True, timeout=120
        )
        return {"status": "switching", "model": model, "output": result.stdout[-300:]}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# ═══════════════════════════════════════════════════════════════════════════
#  AudioLDM2 /generate
# ═══════════════════════════════════════════════════════════════════════════
@APP.post("/api/audioldm2/generate")
async def audioldm2_generate(request: Request):
    body = await request.json()
    prompt = body.get("prompt", "")
    if not prompt.strip():
        return JSONResponse({"error": "prompt vacío"}, status_code=400)

    steps = body.get("num_inference_steps", 200)
    length = body.get("audio_length_in_s", 10)

    try:
        async with httpx.AsyncClient(timeout=300) as client:
            resp = await client.post(f"{_AUDIO}/generate", json={
                "prompt": prompt,
                "audio_length_in_s": length,
                "num_inference_steps": steps,
                "guidance_scale": 3.5,
            })
            resp.raise_for_status()
        excluded = {"content-encoding", "content-length", "transfer-encoding", "connection"}
        headers = {k: v for k, v in resp.headers.items() if k.lower() not in excluded}
        return Response(content=resp.content, status_code=200, headers=headers, media_type="audio/raw")
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# ═══════════════════════════════════════════════════════════════════════════
#  Voxtral TTS /v1/audio/speech
# ═══════════════════════════════════════════════════════════════════════════
@APP.post("/api/voxtral/v1/audio/speech")
async def voxtral_speech(request: Request):
    body = await request.json()
    text = body.get("input", "")
    voice = body.get("voice", "tara")
    if not text.strip():
        return JSONResponse({"error": "texto vacío"}, status_code=400)

    try:
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(f"{_VOXTRAL}/v1/audio/speech", json={
                "input": text,
                "voice": voice,
                "response_format": "wav",
                "speed": 1.0,
            })
            resp.raise_for_status()
        excluded = {"content-encoding", "content-length", "transfer-encoding", "connection"}
        headers = {k: v for k, v in resp.headers.items() if k.lower() not in excluded}
        return Response(content=resp.content, status_code=200, headers=headers, media_type=resp.headers.get("content-type", "audio/wav"))
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# ═══════════════════════════════════════════════════════════════════════════
#  Voz clonada /v1/audio/clone  (audio de referencia + texto)
# ═══════════════════════════════════════════════════════════════════════════
@APP.post("/api/voxtral/v1/audio/clone")
async def voxtral_clone(request: Request):
    form = await request.form()
    audio_file = form.get("audio")
    text = form.get("text", "")

    if not audio_file or not text.strip():
        return JSONResponse({"error": "audio y texto requeridos"}, status_code=400)

    file_content = await audio_file.read()
    files = {"audio": (audio_file.filename, file_content, audio_file.content_type or "audio/wav")}
    data = {"text": text}

    try:
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(f"{_VOXTRAL}/v1/audio/clone", files=files, data=data)
            resp.raise_for_status()
        excluded = {"content-encoding", "content-length", "transfer-encoding", "connection"}
        headers = {k: v for k, v in resp.headers.items() if k.lower() not in excluded}
        return Response(content=resp.content, status_code=200, headers=headers, media_type=resp.headers.get("content-type", "audio/wav"))
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# ═══════════════════════════════════════════════════════════════════════════
#  Proxy genérico para whisper, qwen, flux
# ═══════════════════════════════════════════════════════════════════════════
_HTTP_METHODS = {"GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"}

@APP.get("/api/gpu2/status")
async def gpu2_status():
    import subprocess
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used,memory.total", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5
        )
        lines = result.stdout.strip().split("\n")
        # GPU 0 es la que usa ComfyUI/sdxl por defecto
        parts = lines[0].split(",") if lines else ["0", "0"]
        mem_used = int(parts[0].strip()) if parts else 0
        mem_total = int(parts[1].strip()) if len(parts) > 1 else 0
        return {"memory_used_mb": mem_used, "memory_total_mb": mem_total}
    except Exception as e:
        return {"memory_used_mb": 0, "error": str(e)}

@APP.api_route("/api/{backend}/{path:path}", methods=list(_HTTP_METHODS))
async def proxy(backend: str, path: str, request: Request):
    base = _BACKENDS.get(backend)
    if base is None:
        return JSONResponse({"error": f"backend '{backend}' desconocido"}, status_code=404)

    url = f"{base}/{path}"
    body = await request.body()
    headers = {k: v for k, v in request.headers.items() if k.lower() in ("content-type", "accept", "user-agent")}

    async with httpx.AsyncClient(timeout=300, follow_redirects=True) as client:
        resp = await client.request(request.method, url, headers=headers, content=body, params=dict(request.query_params))

    excluded = {"content-encoding", "content-length", "transfer-encoding", "connection"}
    resp_headers = {k: v for k, v in resp.headers.items() if k.lower() not in excluded}
    return Response(content=resp.content, status_code=resp.status_code, headers=resp_headers, media_type=resp.headers.get("content-type"))


# ═══════════════════════════════════════════════════════════════════════════
#  Health agregado
# ═══════════════════════════════════════════════════════════════════════════
@APP.get("/health/services")
async def health_services():
    out = {}
    for name, base in _BACKENDS.items():
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                r = await client.get(f"{base}/health")
                body = r.content.strip().decode()
                if body.startswith("{"):
                    try:
                        out[name] = json.loads(body)
                    except Exception:
                        out[name] = {"status": "ok" if r.status_code < 400 else "offline"}
                else:
                    out[name] = {"status": "ok" if r.status_code < 400 else "offline", "raw": body[:120]}
        except Exception as e:
            out[name] = {"status": "offline", "error": str(e)}
    return out


# ═══════════════════════════════════════════════════════════════════════════
#  Servir HTML
# ═══════════════════════════════════════════════════════════════════════════
_HTML_PATH = "/home/sam/ai_system/test_ui.html"
_MOVIE_PATH = "/home/sam/ai_system/movie.html"

@APP.get("/", response_class=HTMLResponse)
async def root():
    try:
        with open(_MOVIE_PATH) as f:
            return f.read()
    except FileNotFoundError:
        return HTMLResponse("<h1>404</h1>", status_code=404)

@APP.get("/test_ui.html", response_class=HTMLResponse)
async def test_ui():
    try:
        with open(_HTML_PATH) as f:
            return f.read()
    except FileNotFoundError:
        return HTMLResponse("<h1>404</h1>", status_code=404)

@APP.get("/movie.html", response_class=HTMLResponse)
async def movie():
    try:
        with open(_MOVIE_PATH) as f:
            return f.read()
    except FileNotFoundError:
        return HTMLResponse("<h1>404</h1>", status_code=404)

@APP.get("/api/info")
async def api_info():
    return {"ui_port": 8080, "backends": _BACKENDS}


# ═══════════════════════════════════════════════════════════════════════════
# ═══════════════════════════════════════════════════════════════════════════
#  Firmware OTA
# ═══════════════════════════════════════════════════════════════════════════
_FIRMWARE_BIN = str(Path("/home/sam/esp32cam_project/.pio/build/esp32cam/firmware.bin"))

@APP.get("/firmware/bin")
async def firmware_download():
    return FileResponse(
        _FIRMWARE_BIN,
        media_name="firmware.bin",
        headers={"Content-Disposition": "attachment; filename=firmware.bin"}
    )

#  Main — HTTP simple (sin SSL para evitar problemas con setsid/nohup)
# ═══════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    print("=" * 52)
    print("  UI Proxy v2  →  http://0.0.0.0:8090")
    print("  ComfyUI      →  http://0.0.0.0:8080")
    print("=" * 52)
    for name, base in _BACKENDS.items():
        print(f"  /api/{name}/  →  {base}")
    print("  /api/sdxl/generate    → ComfyUI workflow")
    print("  /api/flux/generate    → Flux GGUF workflow")
    print("  /api/audioldm2/generate → AudioLDM2")
    print("  /api/comfyui/*          → ComfyUI :8080")
    print("=" * 52)
    uvicorn.run(APP, host="0.0.0.0", port=8090, log_level="info")
