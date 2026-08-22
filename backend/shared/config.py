"""shared/config.py — Configuración central multi-nodo."""
import os
from pathlib import Path

NODE_ID = os.environ.get("NODE_ID", "ojoia")
NODE_NAME = os.environ.get("NODE_NAME", "OjoIA Primary")

NODES = {
    "ojoia": {
        "host": os.environ.get("OJOIA_HOST", "127.0.0.1"),
        "megapanel_port": int(os.environ.get("MEGAPANEL_PORT", "9001")),
        "agent_port": None,
        "role": "master",
    },
    "cineia": {
        "host": os.environ.get("CINEIA_HOST", "10.0.0.44"),
        "megapanel_port": None,
        "agent_port": int(os.environ.get("CINEIA_AGENT_PORT", "8300")),
        "role": "worker",
    },
}

BILLING_ENABLED = os.environ.get("BILLING_ENABLED", "0") == "1"
CREDITS_PER_REQUEST = {
    "qwen7b": 1, "qwen14b": 3, "qwen35b": 10,
    "whisper": 1, "yolo": 1, "comfyui_wan": 5, "comfyui_flux": 8,
}

BASE_DIR = Path(__file__).resolve().parent.parent
TOKEN_FILE = Path("/home/sam/.ojoia_megapanel_token")
