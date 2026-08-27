"""
ComfyUI model download helper.
Endpoint para descargar modelos de Wan 2.1 I2V via huggingface_hub.
"""

import os
import asyncio
import threading
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

COMFYUI_MODELS_DIR = Path("/home/sam/ai_system/ComfyUI/models")

WAN_MODELS = {
    "wan2.1_i2v_720p_14B_fp8_scaled.safetensors": {
        "repo": "Comfy-Org/Wan_2.1_ComfyUI_repackaged",
        "file": "split_files/diffusion_models/wan2.1_i2v_720p_14B_fp8_scaled.safetensors",
        "folder": "diffusion_models",
        "size_gb": 13.0,
    },
    "wan2.1_t2v_720p_14B_fp8_scaled.safetensors": {
        "repo": "Comfy-Org/Wan_2.1_ComfyUI_repackaged",
        "file": "split_files/diffusion_models/wan2.1_t2v_720p_14B_fp8_scaled.safetensors",
        "folder": "diffusion_models",
        "size_gb": 13.0,
    },
    "umt5_xxl_fp8_e4m3fn_scaled.safetensors": {
        "repo": "Comfy-Org/Wan_2.1_ComfyUI_repackaged",
        "file": "split_files/text_encoders/umt5_xxl_fp8_e4m3fn_scaled.safetensors",
        "folder": "text_encoders",
        "size_gb": 9.5,
    },
    "wan_2.1_vae.safetensors": {
        "repo": "Comfy-Org/Wan_2.1_ComfyUI_repackaged",
        "file": "split_files/vae/wan_2.1_vae.safetensors",
        "folder": "vae",
        "size_gb": 0.3,
    },
    "clip_vision_h.safetensors": {
        "repo": "Comfy-Org/Wan_2.1_ComfyUI_repackaged",
        "file": "split_files/clip_vision/clip_vision_h.safetensors",
        "folder": "clip_vision",
        "size_gb": 2.5,
    },
}

# Estado de la descarga en progreso
_download_status = {
    "running": False,
    "current": "",
    "progress": {},
    "thread": None,
}


def _do_download_all():
    """Descargar todos los modelos en un hilo separado."""
    from huggingface_hub import hf_hub_download

    _download_status["running"] = True
    _download_status["progress"] = {}

    for filename, info in WAN_MODELS.items():
        _download_status["current"] = filename
        dest_dir = COMFYUI_MODELS_DIR / info["folder"]
        dest_path = dest_dir / filename

        if dest_path.exists():
            size_gb = dest_path.stat().st_size / (1024**3)
            _download_status["progress"][filename] = {
                "status": "already_exists",
                "size_gb": round(size_gb, 2),
            }
            continue

        try:
            dest_dir.mkdir(parents=True, exist_ok=True)
            result_path = hf_hub_download(
                repo_id=info["repo"],
                filename=info["file"],
                local_dir=str(dest_dir),
                local_dir_use_symlinks=False,
            )
            size_gb = Path(result_path).stat().st_size / (1024**3)
            _download_status["progress"][filename] = {
                "status": "downloaded",
                "size_gb": round(size_gb, 2),
            }
        except Exception as e:
            _download_status["progress"][filename] = {
                "status": "error",
                "error": str(e),
            }

    _download_status["running"] = False
    _download_status["current"] = ""


def register_comfyui_endpoints(app: FastAPI):
    """Registra los endpoints de descarga de modelos en la app."""

    @app.get("/api/comfyui/models/status")
    async def models_status():
        result = {}
        for name, info in WAN_MODELS.items():
            path = COMFYUI_MODELS_DIR / info["folder"] / name
            exists = path.exists()
            size_gb = round(path.stat().st_size / (1024**3), 2) if exists else 0
            result[name] = {
                "installed": exists,
                "size_gb": size_gb,
                "expected_gb": info["size_gb"],
                "folder": info["folder"],
                "ok": exists and size_gb > info["size_gb"] * 0.5,
            }
        return result

    @app.get("/api/comfyui/download-status")
    async def download_status():
        return {
            "running": _download_status["running"],
            "current": _download_status["current"],
            "progress": _download_status["progress"],
        }

    @app.post("/api/comfyui/download-all")
    async def download_all():
        if _download_status["running"]:
            return JSONResponse(
                {"status": "already_running", "current": _download_status["current"]},
                status_code=409,
            )
        # Iniciar descarga en background
        t = threading.Thread(target=_do_download_all, daemon=True)
        t.start()
        _download_status["thread"] = t
        return {"status": "started", "message": "Descarga iniciada en background. Usa /api/comfyui/download-status para ver progreso."}

    @app.post("/api/comfyui/download-model")
    async def download_model(request: Request):
        body = await request.json()
        filename = body.get("filename")
        if filename not in WAN_MODELS:
            return JSONResponse({"error": f"Modelo '{filename}' no reconocido"}, 400)

        info = WAN_MODELS[filename]
        dest_dir = COMFYUI_MODELS_DIR / info["folder"]
        dest_path = dest_dir / filename

        if dest_path.exists():
            size_gb = dest_path.stat().st_size / (1024**3)
            return {"status": "already_exists", "filename": filename, "size_gb": round(size_gb, 2)}

        dest_dir.mkdir(parents=True, exist_ok=True)

        try:
            from huggingface_hub import hf_hub_download
            loop = asyncio.get_event_loop()
            result_path = await loop.run_in_executor(None, lambda: hf_hub_download(
                repo_id=info["repo"],
                filename=info["file"],
                local_dir=str(dest_dir),
                local_dir_use_symlinks=False,
            ))
            size_gb = Path(result_path).stat().st_size / (1024**3)
            return {"status": "downloaded", "filename": filename, "size_gb": round(size_gb, 2)}
        except Exception as e:
            return JSONResponse({"error": str(e), "filename": filename}, 500)
