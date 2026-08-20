#!/usr/bin/env python3
"""Flux Bridge Server v2 - API REST → ComfyUI
Usa CheckpointLoaderSimple para cargar Flux desde checkpoints.
"""
import json, urllib.request, urllib.error, time, random, base64
from http.server import HTTPServer, BaseHTTPRequestHandler

COMFY_URL = "http://127.0.0.1:8006"

def create_workflow(prompt, negative, width, height, steps, cfg, seed):
    return {
        "3": {"class_type": "KSampler","inputs": {
            "seed": seed, "steps": steps, "cfg": cfg,
            "sampler_name": "euler", "scheduler": "simple",
            "denoise": 1.0, "model": ["4",0],
            "positive": ["6",0], "negative": ["7",0],
            "latent_image": ["5",0]
        }},
        "4": {"class_type": "CheckpointLoaderSimple","inputs": {"ckpt_name": "flux1-dev.safetensors"}},
        "5": {"class_type": "EmptyLatentImage","inputs": {"width": width, "height": height, "batch_size": 1}},
        "6": {"class_type": "CLIPTextEncode","inputs": {"text": prompt, "clip": ["4",1]}},
        "7": {"class_type": "CLIPTextEncode","inputs": {"text": negative, "clip": ["4",1]}},
        "11": {"class_type": "VAELoader","inputs": {"vae_name": "flux-vae.safetensors"}},
        "12": {"class_type": "VAEDecode","inputs": {"samples": ["3",0], "vae": ["11",0]}},
        "13": {"class_type": "SaveImage","inputs": {"filename_prefix": "flux_cinema", "images": ["12",0]}}
    }

def submit_and_wait(workflow, max_wait=300):
    # Submit
    data = json.dumps({"prompt": workflow}).encode()
    req = urllib.request.Request(f"{COMFY_URL}/api/prompt", data=data,
                                  headers={"Content-Type": "application/json"})
    resp = urllib.request.urlopen(req, timeout=60)
    result = json.loads(resp.read())
    prompt_id = result.get("prompt_id")
    if not prompt_id:
        return None, "No prompt_id"

    # Wait for completion
    start = time.time()
    while time.time() - start < max_wait:
        try:
            req = urllib.request.Request(f"{COMFY_URL}/api/history/{prompt_id}")
            resp = urllib.request.urlopen(req, timeout=15)
            history = json.loads(resp.read())
            if prompt_id in history:
                entry = history[prompt_id]
                if "outputs" in entry:
                    for nid, output in entry["outputs"].items():
                        if "images" in output:
                            for img_info in output["images"]:
                                fn = img_info.get("filename","")
                                sf = img_info.get("subfolder","")
                                t = img_info.get("type","output")
                                params = f"?filename={fn}&subfolder={sf}&type={t}"
                                img_req = urllib.request.Request(f"{COMFY_URL}/view{params}")
                                img_resp = urllib.request.urlopen(img_req, timeout=30)
                                return img_resp.read(), None
        except: pass
        time.sleep(2)
    return None, "Timeout"

class Handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin","*")
        self.send_header("Access-Control-Allow-Methods","GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers","*")
        self.end_headers()

    def do_GET(self):
        if self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type","application/json")
            self.send_header("Access-Control-Allow-Origin","*")
            self.end_headers()
            self.wfile.write(json.dumps({"status":"healthy","model":"flux"}).encode())
            return
        self.send_error(404)

    def do_POST(self):
        if self.path == "/api/flux/generate":
            cl = int(self.headers.get("Content-Length",0))
            body = self.rfile.read(cl)
            try:
                data = json.loads(body)
            except:
                self.send_error(400, "Invalid JSON")
                return

            prompt = data.get("prompt","")
            negative = data.get("negative_prompt","blurry, bad quality")
            width = data.get("width", 1024)
            height = data.get("height", 1024)
            steps = data.get("steps", 25)
            cfg = data.get("cfg", 4.0)
            seed = data.get("seed", -1)
            if seed < 0:
                seed = random.randint(1, 2**31)

            workflow = create_workflow(prompt, negative, width, height, steps, cfg, seed)
            img_data, err = submit_and_wait(workflow)

            if err:
                self.send_response(500)
                self.send_header("Content-Type","application/json")
                self.send_header("Access-Control-Allow-Origin","*")
                self.end_headers()
                self.wfile.write(json.dumps({"success":False,"error":err}).encode())
                return

            self.send_response(200)
            self.send_header("Content-Type","application/json")
            self.send_header("Access-Control-Allow-Origin","*")
            self.end_headers()
            self.wfile.write(json.dumps({
                "success": True,
                "image_b64": base64.b64encode(img_data).decode(),
                "seed": seed, "width": width, "height": height
            }).encode())
            return
        self.send_error(404)

    def log_message(self, *a):
        pass

if __name__ == "__main__":
    server = HTTPServer(("0.0.0.0", 8013), Handler)
    print("[Flux Bridge v2] Listening on :8013", flush=True)
    server.serve_forever()
