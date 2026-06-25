#!/usr/bin/env python3
import os
os.environ["CUDA_VISIBLE_DEVICES"] = "1"
os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["TRANSFORMERS_OFFLINE"] = "1"

import torch
from diffusers import StableDiffusionXLPipeline, EulerAncestralDiscreteScheduler, AutoencoderKL

print("[START] Cargando SDXL Turbo con VAE sdxl_vae...")

pipe = StableDiffusionXLPipeline.from_single_file(
    "/home/sam/ai_system/ComfyUI/models/checkpoints/sd_xl_turbo_1.0_fp16.safetensors",
    torch_dtype=torch.float16,
    variant="fp16",
    use_safetensors=True,
    local_files_only=True,
)

print("[START] Cargando VAE sdxl_vae...")
vae = AutoencoderKL.from_single_file(
    "/home/sam/ai_system/ComfyUI/models/vae/sdxl_vae.safetensors",
    torch_dtype=torch.float16,
)
pipe.vae = vae

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
pipe.scheduler = EulerAncestralDiscreteScheduler.from_config(scheduler_config)
pipe.to("cuda:0")
pipe.enable_attention_slicing()

print(f"[START] SDXL Turbo cargado | VRAM: {torch.cuda.memory_allocated(0) / 1024**3:.1f}GB")

# Guardar el pipe globalmente para que gpu1_image_server.py lo use
import pickle
with open("/tmp/sdxl_pipe.pkl", "wb") as f:
    pickle.dump(pipe, f)

print("[START] Pipe guardado en /tmp/sdxl_pipe.pkl")
