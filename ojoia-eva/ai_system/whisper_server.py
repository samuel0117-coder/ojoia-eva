#!/usr/bin/env python3
"""Whisper Turbo INT8 ASR server - GPU 1 :8008"""
import os
os.environ["HF_HOME"] = "/home/sam/.cache/hf_models"
os.environ["HF_HUB_OFFLINE"] = "0"
os.environ["CUDA_VISIBLE_DEVICES"] = "1"

import torch
from faster_whisper import WhisperModel
import uvicorn
from fastapi import FastAPI, UploadFile, File, HTTPException
import tempfile
import numpy as np
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="Whisper Turbo ASR", version="1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

model = None

@app.on_event("startup")
async def load_model():
    global model
    try:
        model = WhisperModel(
            "openai/whisper-large-v3-turbo",
            device="cuda",
            compute_type="int8"
        )
    except Exception as e:
        print(f"Error cargando modelo: {e}")
        model = WhisperModel("base", device="cuda", compute_type="int8")

@app.post("/transcribe")
async def transcribe(
    audio: UploadFile = File(...),
    language: str = "es"
):
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".wav") as tmp:
            content = await audio.read()
            tmp.write(content)
            tmp_path = tmp.name
        
        segments, info = model.transcribe(tmp_path, language=language)
        text = " ".join([s.text for s in segments])
        os.unlink(tmp_path)
        
        return {"text": text, "language": info.language, "duration": info.duration}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/health")
async def health():
    return {"status": "healthy", "model": "whisper-turbo-int8"}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8008)
