#!/usr/bin/env python3
import asyncio
import os
import re
import subprocess
import time

import httpx
import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

os.environ["CUDA_VISIBLE_DEVICES"] = "2"

APP = FastAPI(title="GPU2 Swap Manager", version="1.0")
APP.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

COMFYUI_URL = os.getenv("COMFYUI_URL", "http://127.0.0.1:8006")
GPU_ID = os.getenv("GPU_ID", "2")
PORT = int(os.getenv("PORT", "8016"))

current_model = "none"
swap_lock = asyncio.Lock()


class SwitchRequest(BaseModel):
    model: str = "none"
    min_free_gb: float = 10.0
    timeout: int = 120


class FreeRequest(BaseModel):
    timeout: int = 90
    min_free_gb: float = 10.0


def nvidia_smi_gpu2():
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "-i",
                GPU_ID,
                "--query-gpu=memory.total,memory.used,memory.free",
                "--format=csv,noheader,nounits",
            ],
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
                "total_gb": round(parts[0] / 1024, 2),
                "used_gb": round(parts[1] / 1024, 2),
                "free_gb": round(parts[2] / 1024, 2),
            }
    except Exception as e:
        return {"error": str(e)}


async def comfy_system_stats():
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"{COMFYUI_URL}/system_stats")
            resp.raise_for_status()
            return resp.json()
    except Exception as e:
        return {"error": str(e)}


async def free_comfy():
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            await client.post(f"{COMFYUI_URL}/free", json={"unload_models": True, "free_memory": True})
        return True
    except Exception:
        return False


async def wait_free_vram(min_free_gb: float, timeout: int):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = nvidia_smi_gpu2()
        if isinstance(last, dict) and "free_gb" in last and last["free_gb"] >= min_free_gb:
            return True, last
        await asyncio.sleep(2)
    return False, last


async def switch_model(model: str, min_free_gb: float, timeout: int):
    global current_model
    model = model.lower()
    if model not in {"none", "flux", "wan"}:
        raise ValueError("model must be none, flux, or wan")

    async with swap_lock:
        previous = current_model
        if previous != model:
            if previous != "none":
                await free_comfy()
                await asyncio.sleep(2)
            current_model = model

        ok, vram = await wait_free_vram(min_free_gb, timeout)
        stats = await comfy_system_stats()
        return {
            "success": ok,
            "model": current_model,
            "previous_model": previous,
            "vram": vram,
            "comfyui": stats,
        }


@APP.get("/health")
async def health():
    vram = nvidia_smi_gpu2()
    return {
        "status": "ok",
        "model": current_model,
        "vram": vram,
    }


@APP.get("/api/gpu2/status")
async def status():
    vram = nvidia_smi_gpu2()
    stats = await comfy_system_stats()
    return {
        "model": current_model,
        "mode": "deferred_swap",
        "vram": vram,
        "comfyui": stats,
    }


@APP.post("/api/gpu2/free")
async def free(req: FreeRequest = FreeRequest()):
    global current_model
    async with swap_lock:
        ok = await free_comfy()
        await asyncio.sleep(2)
        waited, vram = await wait_free_vram(req.min_free_gb, req.timeout)
        current_model = "none"
    return {
        "success": ok and waited,
        "model": current_model,
        "vram": vram,
    }


@APP.post("/api/gpu2/switch")
async def switch(req: SwitchRequest):
    try:
        result = await switch_model(req.model, req.min_free_gb, req.timeout)
        return result
    except ValueError as e:
        from fastapi.responses import JSONResponse
        return JSONResponse({"error": str(e)}, status_code=400)


@APP.post("/api/gpu2/prepare-flux")
async def prepare_flux():
    return await switch_model("flux", 10.0, 120)


@APP.post("/api/gpu2/prepare-wan")
async def prepare_wan():
    return await switch_model("wan", 10.0, 120)


@APP.post("/api/gpu2/release")
async def release():
    return await switch_model("none", 10.0, 120)


if __name__ == "__main__":
    uvicorn.run(APP, host="0.0.0.0", port=PORT, log_level="warning")
