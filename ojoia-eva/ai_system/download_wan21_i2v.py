#!/usr/bin/env python3
"""
Descarga Wan 2.1 I2V (720P fp8_scaled) a las carpetas de ComfyUI.
Usa hf_hug_download para bajar directo, sin necesidad de huggingface-cli.

GPU: La descarga va a SSD (sin GPU). ComfyUI carga modelo en GPU 2 para inferencia.
"""
import os
import sys
import json
import time
from pathlib import Path

COMFYUI_DIR = Path("/home/sam/ai_system/ComfyUI")

MODELS = {
    "diffusion_models": [
        {
            "repo": "Comfy-Org/Wan_2.1_ComfyUI_repackaged",
            "file": "split_files/diffusion_models/wan2.1_i2v_720p_14B_fp8_scaled.safetensors",
            "name": "wan2.1_i2v_720p_14B_fp8_scaled.safetensors",
            "size_gb": 13.0,
        },
        {
            "repo": "Comfy-Org/Wan_2.1_ComfyUI_repackaged",
            "file": "split_files/diffusion_models/wan2.1_t2v_720p_14B_fp8_scaled.safetensors",
            "name": "wan2.1_t2v_720p_14B_fp8_scaled.safetensors",
            "size_gb": 13.0,
        },
    ],
    "text_encoders": [
        {
            "repo": "Comfy-Org/Wan_2.1_ComfyUI_repackaged",
            "file": "split_files/text_encoders/umt5_xxl_fp8_e4m3fn_scaled.safetensors",
            "name": "umt5_xxl_fp8_e4m3fn_scaled.safetensors",
            "size_gb": 9.5,
        },
    ],
    "vae": [
        {
            "repo": "Comfy-Org/Wan_2.1_ComfyUI_repackaged",
            "file": "split_files/vae/wan_2.1_vae.safetensors",
            "name": "wan_2.1_vae.safetensors",
            "size_gb": 0.3,
        },
    ],
    "clip_vision": [
        {
            "repo": "Comfy-Org/Wan_2.1_ComfyUI_repackaged",
            "file": "split_files/clip_vision/clip_vision_h.safetensors",
            "name": "clip_vision_h.safetensors",
            "size_gb": 2.5,
        },
    ],
}

def download_model(dest_dir: Path, repo: str, file_path: str, name: str, skip_existing=True):
    """Descargar un modelo usando hf_hub_download."""
    dest = dest_dir / name
    
    if dest.exists() and skip_existing:
        size_gb = dest.stat().st_size / (1024**3)
        print(f"  [SKIP] {name} ({size_gb:.1f}G ya existe)")
        return True

    print(f"  [DOWNLOAD] {name} (~{dest_dir}/{name})...")
    t0 = time.time()
    
    try:
        from huggingface_hub import hf_hub_download
        
        result = hf_hub_download(
            repo_id=repo,
            filename=file_path,
            local_dir=str(dest_dir),
            local_dir_use_symlinks=False,
        )
        
        elapsed = time.time() - t0
        size_gb = Path(result).stat().st_size / (1024**3)
        speed = size_gb / (elapsed / 60)
        print(f"  [OK] {name}: {size_gb:.1f}G en {elapsed:.0f}s ({speed:.1f}G/min)")
        return True
        
    except Exception as e:
        print(f"  [ERROR] {name}: {e}")
        return False


def create_workflow():
    """Crear workflow JSON de Wan 2.1 I2V para ComfyUI."""
    workflow = {
        "meta": {
            "title": "Wan 2.1 I2V 720P",
            "description": "Image to Video con Wan 2.1 (GPU 2). Sube imagen + escribe prompt.",
        },
        "1": {
            "class_type": "UNETLoader",
            "_meta": {"title": "Load Diffusion Model"},
            "inputs": {
                "unet_name": "wan2.1_i2v_720p_14B_fp8_scaled.safetensors",
                "weight_dtype": "fp8_scaled",
            },
        },
        "2": {
            "class_type": "CLIPLoader",
            "_meta": {"title": "Load Text Encoder"},
            "inputs": {
                "clip_name": "umt5_xxl_fp8_e4m3fn_scaled.safetensors",
                "type": "wan",
                "device": "default",
            },
        },
        "3": {
            "class_type": "VAELoader",
            "_meta": {"title": "Load VAE"},
            "inputs": {"vae_name": "wan_2.1_vae.safetensors"},
        },
        "4": {
            "class_type": "CLIPVisionLoader",
            "_meta": {"title": "Load CLIP Vision"},
            "inputs": {"clip_name": "clip_vision_h.safetensors"},
        },
        "5": {
            "class_type": "LoadImage",
            "_meta": {"title": "Load Image"},
            "inputs": {"image": "input.jpg"},
        },
        "6": {
            "class_type": "CLIPTextEncode",
            "_meta": {"title": "Positive Prompt"},
            "inputs": {
                "clip": ["2", 0],
                "text": "",
            },
        },
        "7": {
            "class_type": "CLIPTextEncode",
            "_meta": {"title": "Negative Prompt"},
            "inputs": {
                "clip": ["2", 0],
                "text": "overexposed, low quality, blurry, static, text watermark, deformed, ugly, bad anatomy, extra fingers, cropped, worst quality",
            },
        },
        "8": {
            "class_type": "CLIPVisionEncode",
            "_meta": {"title": "Encode Image"},
            "inputs": {
                "clip_vision": ["4", 0],
                "image": ["5", 0],
            },
        },
        "9": {
            "class_type": "WanImageToVideo",
            "_meta": {"title": "Wan Image to Video"},
            "inputs": {
                "positive": ["6", 0],
                "negative": ["7", 0],
                "vae": ["3", 0],
                "clip_vision_output": ["8", 0],
                "start_image": ["5", 0],
                "width": 1280,
                "height": 720,
                "length": 81,
                "batch_size": 1,
            },
        },
        "10": {
            "class_type": "EmptyLatentVideo",
            "_meta": {"title": "Empty Latent Video"},
            "inputs": {
                "width": 1280,
                "height": 720,
                "length": 81,
                "batch_size": 1,
            },
        },
        "11": {
            "class_type": "KSampler",
            "_meta": {"title": "KSampler"},
            "inputs": {
                "model": ["1", 0],
                "positive": ["9", 0],
                "negative": ["9", 1],
                "latent_image": ["10", 0],
                "seed": 42,
                "steps": 20,
                "cfg": 6.0,
                "sampler_name": "euler",
                "scheduler": "simple",
                "denoise": 1.0,
            },
        },
        "12": {
            "class_type": "VAEDecode",
            "_meta": {"title": "VAE Decode"},
            "inputs": {
                "samples": ["11", 0],
                "vae": ["3", 0],
            },
        },
        "13": {
            "class_type": "VHS_VideoCombine",
            "_meta": {"title": "Combine Video"},
            "inputs": {
                "images": ["12", 0],
                "frame_rate": 16,
                "loop_count": 0,
                "filename_prefix": "Wan2.1_I2V",
                "format": "video/h264-mp4",
                "pix_fmt": "yuv420p",
                "crf": 19,
                "save_metadata": True,
                "prompt": None,
                "extra_pnginfo": None,
            },
        },
    }

    # Guardar workflow en la carpeta de workflows de ComfyUI
    wf_dir = COMFYUI_DIR / "user" / "default" 
    wf_dir.mkdir(parents=True, exist_ok=True)
    wf_file = wf_dir / "Wan2.1_I2V_720P.json"
    
    with open(wf_file, "w") as f:
        json.dump(workflow, f, indent=2)
    
    print(f"\n[OK] Workflow guardado: {wf_file}")
    return wf_file


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-existing", action="store_true", default=True)
    parser.add_argument("--workflow-only", action="store_true")
    args = parser.parse_args()

    print("=" * 60)
    print("  Wan 2.1 I2V 720P - Descarga para ComfyUI")
    print(f"  ComfyUI: {COMFYUI_DIR}")
    print("=" * 60)

    if args.workflow_only:
        create_workflow()
        return

    # Verificar espacio
    stat = os.statvfs(COMFYUI_DIR)
    free_gb = (stat.f_bavail * stat.f_frsize) / (1024**3)
    print(f"\nEspacio libre: {free_gb:.1f}G (se necesitan ~38G)")
    if free_gb < 40:
        print("[ERROR] No hay suficiente espacio")
        sys.exit(1)

    # Crear carpetas
    for folder in MODELS:
        (COMFYUI_DIR / "models" / folder).mkdir(parents=True, exist_ok=True)

    # Descargar
    print("\n--- Descargando modelos ---")
    all_ok = True
    for folder, models in MODELS.items():
        dest = COMFYUI_DIR / "models" / folder
        print(f"\n{folder}/")
        for m in models:
            ok = download_model(dest, m["repo"], m["file"], m["name"], args.skip_existing)
            if not ok:
                all_ok = False

    # Crear workflow
    print("\n--- Creando workflow ---")
    wf = create_workflow()

    print("\n" + "=" * 60)
    if all_ok:
        print("  ¡Modelos descargados!")
        print(f"  Workflow: {wf}")
        print("\n  Abre ComfyUI (http://10.0.0.44:8080/)")
        print("  El workflow 'Wan2.1_I2V_720P' aparece en la lista.")
    else:
        print("  Algunos modelos fallaron. Revisa los logs.")
    print("=" * 60)


if __name__ == "__main__":
    main()
