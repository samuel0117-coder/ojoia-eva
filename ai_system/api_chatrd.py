#!/usr/bin/env python3
"""
ChatRD API v1 — Chat minimalista con herramientas visuales (SDXL + SVG)
Arquitectura: Qwen2.5-VL como cerebro, SDXL Turbo y SVG como herramientas.
 Las imágenes se cachean por hash de prompt para reutilización entre usuarios.
"""
import logging
import os
import json
import re
import time
import base64
import hashlib
import asyncio
from datetime import datetime
from pathlib import Path
from typing import Optional, List

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from pydantic import BaseModel
import httpx

# ═══════════════════════════════════════════════════════════════════════════
#  CONFIGURACIÓN
# ═══════════════════════════════════════════════════════════════════════════

QWEN_URL = "http://localhost:8004/v1"
COMFYUI_URL = "http://localhost:8007"
IMAGES_DIR = Path("/home/sam/storage/chatrd/images")
IMAGES_DIR.mkdir(parents=True, exist_ok=True)

logger = logging.getLogger("chatrd")

# ═══════════════════════════════════════════════════════════════════════════
#  MODELOS
# ═══════════════════════════════════════════════════════════════════════════

class ChatRequest(BaseModel):
    message: str
    history: Optional[List[dict]] = None

class SDXLRequest(BaseModel):
    prompt: str
    negative_prompt: str = "blurry, bad quality, distorted, ugly"
    width: int = 512
    height: int = 512
    steps: int = 20
    cfg: float = 5.5
    seed: Optional[int] = None

class ChatResponse(BaseModel):
    response: str
    tools_used: List[str] = []
    images: List[dict] = []

# ═══════════════════════════════════════════════════════════════════════════
#  APP
# ═══════════════════════════════════════════════════════════════════════════

app = FastAPI(title="ChatRD API", version="1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ═══════════════════════════════════════════════════════════════════════════
#  FUNCIONES AUXILIARES
# ═══════════════════════════════════════════════════════════════════════════

def prompt_hash(prompt: str) -> str:
    normalized = re.sub(r'\s+', ' ', prompt.lower().strip())
    return hashlib.md5(normalized.encode()).hexdigest()[:12]

def get_cached_image(prompt: str) -> Optional[Path]:
    h = prompt_hash(prompt)
    for ext in ["png", "jpg", "webp"]:
        p = IMAGES_DIR / f"{h}.{ext}"
        if p.exists():
            return p
    return None

def list_images_index() -> List[dict]:
    index_path = IMAGES_DIR / "index.json"
    if index_path.exists():
        return json.loads(index_path.read_text())
    return []

def save_image_entry(prompt: str, filename: str, tool: str):
    index_path = IMAGES_DIR / "index.json"
    index = list_images_index()
    entry = {
        "prompt": prompt,
        "filename": filename,
        "tool": tool,
        "created": datetime.now().isoformat(),
        "hash": prompt_hash(prompt)
    }
    index = [e for e in index if e["hash"] != entry["hash"]]
    index.insert(0, entry)
    index = index[:200]
    index_path.write_text(json.dumps(index, ensure_ascii=False, indent=2))
    return entry

async def generate_sdxl(prompt: str, negative: str = "", width: int = 512, height: int = 512, steps: int = 20, cfg: float = 5.5, seed: Optional[int] = None) -> dict:
    seed = seed or int(time.time()) % 2147483647
    workflow = {
        "3": {
            "class_type": "KSampler",
            "inputs": {
                "seed": seed, "steps": steps, "cfg": cfg,
                "sampler_name": "euler", "scheduler": "normal",
                "denoise": 0.85,
                "model": ["4", 0], "positive": ["6", 0],
                "negative": ["7", 0], "latent_image": ["5", 0]
            }
        },
        "4": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "sd_xl_turbo_1.0_fp16.safetensors"}},
        "5": {"class_type": "EmptyLatentImage", "inputs": {"width": width, "height": height, "batch_size": 1}},
        "6": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["4", 1]}},
        "7": {"class_type": "CLIPTextEncode", "inputs": {"text": negative or "blurry, bad quality", "clip": ["4", 1]}},
        "8": {"class_type": "VAEDecode", "inputs": {"samples": ["3", 0], "vae": ["4", 2]}},
        "9": {"class_type": "SaveImage", "inputs": {"images": ["8", 0], "filename_prefix": "chatrd_sdxl"}}
    }
    async with httpx.AsyncClient(timeout=300) as client:
        r = await client.post(f"{COMFYUI_URL}/prompt", json={"prompt": workflow})
        data = r.json()
        prompt_id = data.get("prompt_id")
        if not prompt_id:
            raise Exception(f"ComfyUI sin prompt_id: {data}")
        for _ in range(90):
            await asyncio.sleep(1)
            r2 = await client.get(f"{COMFYUI_URL}/history/{prompt_id}")
            h = r2.json()
            if prompt_id in h and h[prompt_id].get("outputs"):
                if "9" in h[prompt_id]["outputs"]:
                    img_info = h[prompt_id]["outputs"]["9"]["images"][0]
                    filename = img_info["filename"]
                    subfolder = img_info.get("subfolder", "")
                    folder_type = img_info.get("type", "output")
                    r3 = await client.get(f"{COMFYUI_URL}/view?filename={filename}&subfolder={subfolder}&type={folder_type}")
                    return {"image_data": base64.b64encode(r3.content).decode("utf-8"), "seed": seed, "prompt_id": prompt_id}
        raise Exception("Timeout esperando imagen SDXL")

async def qwen_call(message: str, history: List[dict], tools_context: str = "") -> str:
    system = f"""Eres un asistente útil y conciso llamado ChatRD. Responde siempre en español.
Si el usuario necesita una explicación visual, usa la herramienta tool_generate_image o tool_generate_svg.
{tools_context}"""

    messages = [{"role": "system", "content": system}]

    if history:
        for msg in history[-8:]:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            if isinstance(content, list):
                text_parts = [p.get("text", "") for p in content if p.get("type") == "text"]
                content = " ".join(text_parts)
            messages.append({"role": role, "content": content})

    messages.append({"role": "user", "content": message})

    # Tool-use con formato OpenAI
    payload = {
        "model": "Qwen/Qwen2.5-VL-7B-Instruct-AWQ",
        "messages": messages,
        "max_tokens": 2048,
        "temperature": 0.3,
        "stream": False,
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "tool_generate_image",
                    "description": "Genera una imagen explicativa con SDXL Turbo (calidad baja, rápida). Útil cuando el usuario necesita una explicación visual o un diagrama.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "prompt": {"type": "string", "description": "Descripción de la imagen a generar (en inglés para mejor resultado)"}
                        },
                        "required": ["prompt"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "tool_generate_svg",
                    "description": "Genera un diagrama SVG para explicar algo visualmente (diagramas de flujo, esquemas, gráficos simples).",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "prompt": {"type": "string", "description": "Descripción del diagrama SVG (en español)"}
                        },
                        "required": ["prompt"]
                    }
                }
            }
        ]
    }

    async with httpx.AsyncClient(timeout=120) as client:
        r = await client.post(f"{QWEN_URL}/chat/completions", json=payload)
        data = r.json()

    if "choices" in data and data["choices"]:
        msg = data["choices"][0]["message"]
        if msg.get("tool_calls"):
            return {"tool_calls": msg["tool_calls"], "content": msg.get("content", "")}
        return {"content": msg.get("content", ""), "tool_calls": []}

    return {"content": str(data), "tool_calls": []}

# ═══════════════════════════════════════════════════════════════════════════
#  ENDPOINTS
# ═══════════════════════════════════════════════════════════════════════════

@app.get("/api/chatrd/health")
async def health():
    return {"status": "ok", "service": "chatrd", "version": "1.0"}

@app.get("/api/chatrd/gallery")
async def gallery():
    """Lista todas las imágenes generadas"""
    images = list_images_index()
    return {"images": images, "count": len(images)}

@app.get("/api/chatrd/image/{hash_val}")
async def get_image(hash_val: str):
    """Obtener imagen por hash"""
    for ext in ["png", "jpg", "webp"]:
        p = IMAGES_DIR / f"{hash_val}.{ext}"
        if p.exists():
            return Response(p.read_bytes(), media_type=f"image/{ext}")
    raise HTTPException(404, "Imagen no encontrada")

@app.post("/api/chatrd/chat")
async def chat(request: ChatRequest):
    """Chat principal con herramientas automáticas"""
    try:
        result = await qwen_call(request.message, request.history or [])

        response_text = result.get("content", "")
        tool_calls = result.get("tool_calls", [])
        images_used = []
        tools_used = []

        if tool_calls:
            for tc in tool_calls:
                func = tc.get("function", {})
                func_name = func.get("name", "")
                func_args = json.loads(func.get("arguments", "{}"))

                if func_name == "tool_generate_image":
                    prompt = func_args.get("prompt", "")
                    cached = get_cached_image(prompt)
                    if cached:
                        img_data = base64.b64encode(cached.read_bytes()).decode()
                        tool_result = f"Imagen encontrada en caché: {prompt}"
                    else:
                        sdxl_result = await generate_sdxl(prompt, width=512, height=512, steps=20, cfg=5.5)
                        img_bytes = base64.b64decode(sdxl_result["image_data"])
                        h = prompt_hash(prompt)
                        save_path = IMAGES_DIR / f"{h}.png"
                        save_path.write_bytes(img_bytes)
                        save_image_entry(prompt, f"{h}.png", "sdxl")
                        tool_result = f"Imagen generada: {prompt}"
                        img_data = sdxl_result["image_data"]

                    images_used.append({"prompt": prompt, "url": f"/api/chatrd/image/{prompt_hash(prompt)}", "tool": "sdxl"})
                    tools_used.append("sdxl")

                    response_text += f"\n\n[Imagen generada: {prompt}]"

                elif func_name == "tool_generate_svg":
                    prompt = func_args.get("prompt", "")
                    svg_result = await qwen_generate_svg(prompt)
                    h = prompt_hash(f"svg:{prompt}")
                    svg_path = IMAGES_DIR / f"{h}.svg"
                    svg_path.write_text(svg_result, encoding="utf-8")
                    save_image_entry(prompt, f"{h}.svg", "svg")
                    images_used.append({"prompt": prompt, "url": f"/api/chatrd/image/{h}", "tool": "svg"})
                    tools_used.append("svg")
                    response_text += f"\n\n[Diagrama SVG generado: {prompt}]"

        return ChatResponse(
            response=response_text,
            tools_used=tools_used,
            images=images_used
        )

    except Exception as e:
        logger.error(f"Chat error: {e}")
        raise HTTPException(500, str(e))

async def qwen_generate_svg(prompt: str) -> str:
    """Genera SVG vía Qwen"""
    messages = [
        {"role": "system", "content": "Genera código SVG válido y limpio. Solo el SVG, sin explicaciones. El SVG debe ser visualmente claro y explicativo."},
        {"role": "user", "content": f"Genera un SVG que explique: {prompt}"}
    ]
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(f"{QWEN_URL}/chat/completions", json={
            "model": "Qwen/Qwen2.5-VL-7B-Instruct-AWQ",
            "messages": messages,
            "max_tokens": 1024,
            "temperature": 0.5,
            "stream": False
        })
        data = r.json()
    if "choices" in data and data["choices"]:
        return data["choices"][0]["message"].get("content", "")
    return "<svg></svg>"

@app.post("/api/chatrd/image/sdxl")
async def generate_sdxl_direct(request: SDXLRequest):
    """Generar imagen SDXL directamente (sin chat)"""
    cached = get_cached_image(request.prompt)
    if cached:
        img_data = base64.b64encode(cached.read_bytes()).decode()
        return {"image_url": f"/api/chatrd/image/{prompt_hash(request.prompt)}", "cached": True}

    result = await generate_sdxl(
        request.prompt, request.negative_prompt,
        request.width, request.height,
        request.steps, request.cfg, request.seed
    )
    img_bytes = base64.b64decode(result["image_data"])
    h = prompt_hash(request.prompt)
    save_path = IMAGES_DIR / f"{h}.png"
    save_path.write_bytes(img_bytes)
    save_image_entry(request.prompt, f"{h}.png", "sdxl")
    return {"image_url": f"/api/chatrd/image/{h}", "cached": False, "seed": result["seed"]}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8010)
