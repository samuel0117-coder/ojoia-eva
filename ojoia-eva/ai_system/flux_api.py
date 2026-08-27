#!/usr/bin/env python3
"""Flux API Bridge - Convierte requests REST a workflows de ComfyUI
Endpoint: POST /api/flux/generate
"""
import json, urllib.request, urllib.error, time, os, sys

COMFY_URL = "http://127.0.0.1:8006"
PROMPT_FILE = "/tmp/flux_prompt.json"

def create_workflow(prompt, negative, width, height, steps, cfg, seed):
    return {
        "3": {"class_type": "KSampler","inputs": {"seed": seed,"steps": steps,"cfg": cfg,"sampler_name": "euler","scheduler": "normal","denoise": 1.0,"model": ["4",0],"positive": ["6",0],"negative": ["7",0],"latent_image": ["5",0]}},
        "4": {"class_type": "UNETLoader","inputs": {"unet_name": "flux1-dev-Q5_K_S.gguf","weight_dtype": "default"}},
        "5": {"class_type": "EmptyLatentImage","inputs": {"width": width,"height": height,"batch_size": 1}},
        "6": {"class_type": "CLIPTextEncode","inputs": {"text": prompt,"clip": ["10",0]}},
        "7": {"class_type": "CLIPTextEncode","inputs": {"text": negative,"clip": ["10",0]}},
        "10": {"class_type": "DualCLIPLoader","inputs": {"clip_name1": "flux-clip.safetensors","clip_name2": "t5-v1_1-xxl-encoder-Q5_K_S.gguf","type": "flux"}},
        "11": {"class_type": "VAELoader","inputs": {"vae_name": "flux-vae.safetensors"}},
        "12": {"class_type": "VAEDecode","inputs": {"samples": ["3",0],"vae": ["11",0]}},
        "13": {"class_type": "SaveImage","inputs": {"filename_prefix": "flux_cinema","images": ["12",0]}}
    }

def submit_to_comfy(workflow):
    """Envía workflow a ComfyUI y espera resultado"""
    data = json.dumps({"prompt": workflow}).encode()
    req = urllib.request.Request(f"{COMFY_URL}/api/prompt", data=data, headers={"Content-Type": "application/json"})
    try:
        resp = urllib.request.urlopen(req, timeout=60)
        return json.loads(resp.read())
    except Exception as e:
        return {"error": str(e)}

def wait_for_output(prompt_id, max_wait=300):
    """Espera a que ComfyUI termine la generación"""
    start = time.time()
    while time.time() - start < max_wait:
        try:
            req = urllib.request.Request(f"{COMFY_URL}/api/history/{prompt_id}")
            resp = urllib.request.urlopen(req, timeout=10)
            history = json.loads(resp.read())
            if prompt_id in history:
                entry = history[prompt_id]
                if "outputs" in entry:
                    for node_id, output in entry["outputs"].items():
                        if "images" in output:
                            for img in output["images"]:
                                return img
        except:
            pass
        time.sleep(2)
    return None

def main():
    """CLI mode: python flux_api.py '{"prompt":"..."}'"""
    import sys
    if len(sys.argv) > 1:
        data = json.loads(sys.argv[1])
        prompt = data.get("prompt", "")
        negative = data.get("negative", "blurry, bad quality")
        width = data.get("width", 1024)
        height = data.get("height", 1024)
        steps = data.get("steps", 25)
        cfg = data.get("cfg", 4.0)
        seed = data.get("seed", -1)
        if seed < 0:
            import random
            seed = random.randint(1, 2**31)

        workflow = create_workflow(prompt, negative, width, height, steps, cfg, seed)
        result = submit_to_comfy(workflow)
        if "error" in result:
            print(json.dumps({"success": False, "error": result["error"]}))
            sys.exit(1)

        prompt_id = result.get("prompt_id")
        if not prompt_id:
            print(json.dumps({"success": False, "error": "No prompt_id"}))
            sys.exit(1)

        print(json.dumps({"success": True, "prompt_id": prompt_id, "status": "submitted"}))

if __name__ == "__main__":
    main()
