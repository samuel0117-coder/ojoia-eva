"""
Wrapper Eva v2 — Puerto 8007
Sirve frontend + API de Eva v2 (setup + OS) + Auth
"""
import sys
sys.path.insert(0, '/home/sam/ai_system')

from fastapi import FastAPI, Response, HTTPException
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
from pathlib import Path
import json, hashlib, logging

logger = logging.getLogger(__name__)
STORAGE_ROOT = Path("/home/sam/storage")

# ═══════════════════════════════════════════════════════════════════════════
# APP
# ═══════════════════════════════════════════════════════════════════════════

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ═══════════════════════════════════════════════════════════════════════════
# AUTH ENDPOINTS
# ═══════════════════════════════════════════════════════════════════════════

class RegisterRequest(BaseModel):
    phone: str
    pin: str
    name: str
    last_name: str
    business_name: str = ""

class LoginRequest(BaseModel):
    phone: str
    pin: str

class ChatRequest(BaseModel):
    user_id: str
    message: str
    session_id: Optional[str] = None
    cam_id: Optional[str] = None

class FeedbackRequest(BaseModel):
    user_id: str
    event_id: str
    is_real: bool


def hash_pin(pin: str) -> str:
    return hashlib.sha256(pin.encode()).hexdigest()


@app.post("/api/auth/register")
async def register(req: RegisterRequest):
    if not req.phone or not req.pin or not req.name:
        return {"success": False, "error": "Completa todos los campos"}
    if len(req.pin) != 4 or not req.pin.isdigit():
        return {"success": False, "error": "PIN debe ser 4 dígitos"}
    uf = STORAGE_ROOT / "users" / req.phone / "user.json"
    if uf.exists():
        return {"success": False, "error": "Teléfono ya registrado"}
    uf.parent.mkdir(parents=True, exist_ok=True)
    user_data = {
        "user_id": req.phone, "pin_hash": hash_pin(req.pin),
        "owner": {"name": req.name, "last_name": req.last_name, "phone": req.phone},
        "business_name": req.business_name if req.business_name else "",
        "business_type": "", "schedule": {"open": "07:00", "close": "19:00"},
        "main_concerns": [], "cameras": {},
        "people": {"known": [], "suspicious": []},
    }
    json.dump(user_data, open(uf, "w"), indent=2, ensure_ascii=False)
    return {"success": True, "user_id": req.phone, "user_name": req.name}


@app.post("/api/auth/login")
async def login(req: LoginRequest):
    if not req.phone or not req.pin:
        return {"success": False, "error": "Completa teléfono y PIN"}
    uf = STORAGE_ROOT / "users" / req.phone / "user.json"
    if not uf.exists():
        return {"success": False, "error": "Teléfono no registrado"}
    user_data = json.load(open(uf))
    if hash_pin(req.pin) != user_data.get("pin_hash", ""):
        return {"success": False, "error": "PIN incorrecto"}
    name = user_data.get("owner", {}).get("name", "")
    biz = user_data.get("business_name", "")
    return {"success": True, "user_id": req.phone, "user_name": name, "business_name": biz}


@app.post("/api/auth/send-pin")
async def send_pin(req: dict):
    phone = req.get("phone", "")
    uf = STORAGE_ROOT / "users" / phone / "user.json"
    if not uf.exists():
        return {"success": False, "error": "Teléfono no registrado"}
    return {"success": True, "message": "PIN enviado"}


# ═══════════════════════════════════════════════════════════════════════════
# EVA V2 — SETUP + OS (un solo endpoint)
# ═══════════════════════════════════════════════════════════════════════════

@app.post("/config/chat")
async def eva_chat_v2(req: ChatRequest):
    """Endpoint unificado Eva v2: setup + OS mode."""
    try:
        from eva.eva_v2 import handle_eva_v2
        result = await handle_eva_v2(
            user_id=req.user_id,
            message=req.message,
            session_id=req.session_id or f"chat_{req.user_id}_{int(__import__('time').time())}",
            cam_id=req.cam_id,
            storage_root=STORAGE_ROOT,
        )
        return result
    except Exception as e:
        logger.error(f"Error en eva_chat_v2: {e}", exc_info=True)
        return {"success": False, "error": str(e), "response": "Error de conexión"}


@app.post("/api/chat/eva/feedback")
async def eva_feedback(req: FeedbackRequest):
    return {"success": True}


# ═══════════════════════════════════════════════════════════════════════════
# FRONTEND — Servir archivos estáticos
# ═══════════════════════════════════════════════════════════════════════════

@app.get("/")
async def serve_index():
    return FileResponse("/home/sam/ojoia-pwa-recuperada/index.html")

@app.get("/{file_path:path}")
async def serve_static(file_path: str):
    if file_path.startswith("api/") or file_path.startswith("config/"):
        raise HTTPException(status_code=404)
    fp = f"/home/sam/ojoia-pwa-recuperada/{file_path}"
    if Path(fp).is_file():
        return FileResponse(fp)
    return FileResponse("/home/sam/ojoia-pwa-recuperada/index.html")


if __name__ == "__main__":
    import uvicorn
    print("Iniciando Eva v2 wrapper en puerto 8007...")
    uvicorn.run(app, host='0.0.0.0', port=8007, log_level='info')
