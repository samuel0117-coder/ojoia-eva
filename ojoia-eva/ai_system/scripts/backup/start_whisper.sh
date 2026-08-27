#!/bin/bash
export HF_HOME=/home/sam/.cache/hf_models
export LD_LIBRARY_PATH="/home/sam/ai_system/venv/lib/python3.12/site-packages/nvidia/cudnn/lib:${LD_LIBRARY_PATH}"
exec /home/sam/ai_system/venv/bin/python /home/sam/ai_system/whisper_server.py
