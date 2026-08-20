#!/usr/bin/env python3
"""SDXL + Flux Image Generation - puerto 8011"""
import json, urllib.request, urllib.error, time, os, sys

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from pydantic import BaseModel
from typing import Optional

os.environ["HF_HOME"] = "/home/sam/.cache/hf_models"

app = FastAPI(title="Image Gen", version="1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

COMFYUI = "http://127.0.0.1:8006"


class GenReq(BaseModel):
    prompt: str
    negative_prompt: str = ""
    model: str = "sdxl"
    width: int = 512
    height: int = 512
    num_inference_steps: int = 4
    guidance_scale: float = 5.0
    seed: int = -1


def submit_workflow(workflow: dict) -> str:
    data = json.dumps({"prompt": workflow}).encode("utf-8")
    req = urllib.request.Request(f"{COMFYUI}/prompt", data=data,
                                  headers={"Content-Type": "application/json"})
    resp = urllib.request.urlopen(req, timeout=30)
    return json.loads(resp.read())["prompt_id"]


@app.get("/health")
async def health():
    try:
        resp = urllib.request.urlopen(f"{COMFYUI}/system_stats", timeout=5)
        v = json.loads(resp.read()).get("system", {}).get("comfyui_version", "?")
        return {"status": "ok", "comfyui_version": v}
    except Exception as e:
        return {"status": "degraded", "error": str(e)}


@app.post("/generate")
async def generate(req: GenReq):
    try:
        seed = req.seed if req.seed > 0 else int(time.time() * 1000) % 2**31

        if req.model == "flux":
            wf = {
                "1": {"class_type": "UNETLoader",
                      "inputs": {"unet_name": "flux1-dev-Q5_K_S.gguf", "weight_dtype": "default"}},
                "2": {"class_type": "DualCLIPLoader",
                      "inputs": {"clip_name1": "t5-v1_1-xxl-encoder-Q5_K_S.gguf",
                                 "clip_name2": "flux-clip.safetensors", "type": "flux"}},
                "3": {"class_type": "VAELoader",
                      "inputs": {"vae_name": "flux-vae.safetensors"}},
                "4": {"class_type": "CLIPTextEncode",
                      "inputs": {"clip": ["2", 0], "text": req.prompt}},
                "5": {"class_type": "CLIPTextEncode",
                      "inputs": {"clip": ["2", 0], "text": req.negative_prompt}},
                "6": {"class_type": "FluxGuidance",
                      "inputs": {"guidance": req.guidance_scale, "conditioning": ["4", 0]}},
                "7": {"class_type": "EmptyFlux2LatentImage",
                      "inputs": {"width": req.width, "height": req.height, "batch_size": 1}},
                "8": {"class_type": "KSampler",
                      "inputs": {"model": ["1", 0], "positive": ["6", 0], "negative": ["5", 0],
                                 "latent_image": ["7", 0], "seed": seed,
                                 "steps": req.num_inference_steps, "cfg": 1.0,
                                 "sampler_name": "euler", "scheduler": "normal", "denoise": 1.0}},
                "9": {"class_type": "VAEDecode",
                      "inputs": {"samples": ["8", 0], "vae": ["3", 0]}},
                "10": {"class_type": "SaveImage",
                       "inputs": {"images": ["9", 0], "filename_prefix": "Flux"}},
            }
        else:
            wf = {
                "1": {"class_type": "CheckpointLoaderSimple",
                      "inputs": {"ckpt_name": "sd_xl_turbo_1.0_fp16.safetensors"}},
                "2": {"class_type": "CLIPTextEncode",
                      "inputs": {"clip": ["1", 0], "text": req.prompt}},
                "3": {"class_type": "CLIPTextEncode",
                      "inputs": {"clip": ["1", 0], "text": req.negative_prompt}},
                "4": {"class_type": "EmptyLatentImage",
                      "inputs": {"width": req.width, "height": req.height, "batch_size": 1}},
                "5": {"class_type": "KSampler",
                      "inputs": {"model": ["1", 0], "positive": ["2", 0], "negative": ["3", 0],
                                 "latent_image": ["4", 0], "seed": seed,
                                 "steps": req.num_inference_steps, "cfg": req.guidance_scale,
                                 "sampler_name": "euler", "scheduler": "normal", "denoise": 1.0}},
                "6": {"class_type": "VAEDecode",
                      "inputs": {"samples": ["5", 0], "vae": ["1", 2]}},
                "7": {"class_type": "SaveImage",
                      "inputs": {"images": ["6", 0], "filename_prefix": "SDXL"}},
            }

        pid = submit_workflow(wf)
        return {"prompt_id": pid}

    except Exception as e:
        print(f"[IMAGE_SERVER] Error: {e}", flush=True)
        raise HTTPException(status_code=500, detail=str(e))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8011, log_level="warning")
