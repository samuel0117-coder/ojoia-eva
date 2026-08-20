#!/usr/bin/env python3
"""AudioLDM2 Server - Text to Audio - GPU 2 :8009 - Carga diferida"""
import os, io, wave, struct, torch, uvicorn, asyncio
import numpy as np
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from pydantic import BaseModel

os.environ["HF_HOME"] = "/home/sam/.cache/hf_models"
os.environ["TORCH_COMPILE_DISABLE"] = "1"
os.environ["TOKENIZERS_PARALLELISM"] = "false"

app = FastAPI(title="AudioLDM2", version="1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

pipe = None
pipe_loaded = False

def get_pipe():
    global pipe, pipe_loaded
    if not pipe_loaded:
        _load()
        pipe_loaded = True
    return pipe

def _load():
    global pipe
    from diffusers import (
        AudioLDM2Pipeline, AudioLDM2UNet2DConditionModel,
        AudioLDM2ProjectionModel, AutoencoderKL, DDIMScheduler,
    )
    from transformers import (
        ClapModel, T5EncoderModel, GPT2LMHeadModel,
        RobertaTokenizerFast, T5Tokenizer, ClapFeatureExtractor,
        SpeechT5HifiGan,
    )
    m = "/home/sam/ai_system/models/audioldm2"
    dt = torch.float16
    print("Cargando AudioLDM2 (bajo demanda)...")
    scheduler = DDIMScheduler.from_pretrained(os.path.join(m, "scheduler"))
    feature_extractor = ClapFeatureExtractor.from_pretrained(os.path.join(m, "feature_extractor"))
    tokenizer = RobertaTokenizerFast.from_pretrained(os.path.join(m, "tokenizer"))
    tokenizer_2 = T5Tokenizer.from_pretrained(os.path.join(m, "tokenizer_2"))
    text_encoder = ClapModel.from_pretrained(os.path.join(m, "text_encoder"), torch_dtype=dt)
    text_encoder_2 = T5EncoderModel.from_pretrained(os.path.join(m, "text_encoder_2"), torch_dtype=dt)
    language_model = GPT2LMHeadModel.from_pretrained(os.path.join(m, "language_model"), torch_dtype=dt)
    projection_model = AudioLDM2ProjectionModel.from_pretrained(os.path.join(m, "projection_model"), torch_dtype=dt)
    unet = AudioLDM2UNet2DConditionModel.from_pretrained(os.path.join(m, "unet"), torch_dtype=dt)
    vae = AutoencoderKL.from_pretrained(os.path.join(m, "vae"), torch_dtype=dt)
    vocoder = SpeechT5HifiGan.from_pretrained(os.path.join(m, "vocoder"), torch_dtype=dt)
    pipe = AudioLDM2Pipeline(
        scheduler=scheduler, feature_extractor=feature_extractor,
        tokenizer=tokenizer, tokenizer_2=tokenizer_2,
        text_encoder=text_encoder, text_encoder_2=text_encoder_2,
        language_model=language_model, projection_model=projection_model,
        unet=unet, vae=vae, vocoder=vocoder,
    )
    pipe.to("cuda:2")
    print(f"AudioLDM2 listo en GPU2 | VRAM: {torch.cuda.memory_allocated()/1e9:.2f} GB")

class GenerateRequest(BaseModel):
    prompt: str = "techno music with a strong beat"
    audio_length_in_s: float = 10.0
    num_inference_steps: int = 200
    guidance_scale: float = 3.5

@app.post("/generate")
async def generate(req: GenerateRequest):
    p = get_pipe()
    try:
        loop = asyncio.get_event_loop()
        audio_np = await loop.run_in_executor(None, lambda: p(
            prompt=req.prompt,
            audio_length_in_s=req.audio_length_in_s,
            num_inference_steps=req.num_inference_steps,
            guidance_scale=req.guidance_scale,
        ).audios[0])
        audio_np = audio_np.astype(np.float32)
        audio_int16 = (np.clip(audio_np, -1.0, 1.0) * 32767).astype(np.int16)
        buf = io.BytesIO()
        with wave.open(buf, 'wb') as wf:
            wf.setnchannels(1); wf.setsampwidth(2)
            wf.setframerate(16000); wf.writeframes(audio_int16.tobytes())
        buf.seek(0)
        return Response(content=buf.getvalue(), media_type="audio/wav")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/health")
async def health():
    return {"status": "healthy", "model": "audioldm2", "loaded": pipe_loaded}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8009)
