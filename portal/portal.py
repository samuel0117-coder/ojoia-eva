#!/usr/bin/env python3
"""OjoIA Portal — User-facing portal for model access."""
import os
import sys
import json
import secrets
import hashlib
import time
import base64
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from billing import BillingStore

# gateway_resize para redimensionar imágenes y agrupar frames de video
sys.path.insert(0, str(Path(__file__).parent.parent / "ai_system"))
try:
    from gateway_resize import resize_image, create_grid_image
    HAS_RESIZE = True
except ImportError:
    HAS_RESIZE = False

from fastapi import FastAPI, Request, Form, HTTPException, Depends, Header, UploadFile, File
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
import httpx

app = FastAPI(title="OjoIA Portal", version="1.0")
templates = Jinja2Templates(directory="/opt/ojoia/code/portal/templates")
app.mount("/static", StaticFiles(directory="/opt/ojoia/code/portal/static"), name="static")

# SEGURIDAD: jamás fallback con credenciales hardcodeadas. Si falta la env var,
# fallar ruidosamente al iniciar (billing.py hace lo mismo).
REDIS_URL = os.environ.get("REDIS_URL")
if not REDIS_URL:
    raise RuntimeError(
        "REDIS_URL no está configurada. Defínela en /opt/ojoia/config/ojoia.env "
        "antes de iniciar el portal."
    )
SERVICE_BUS = "http://127.0.0.1:8200"

def get_billing():
    return BillingStore.instance()

async def _get_apikey(user: dict) -> str:
    billing = get_billing()
    keys = billing.list_keys(user["email"])
    if not keys:
        raise HTTPException(403, "No API key")
    return keys[0]["key"]

def get_current_user(request: Request):
    token = request.cookies.get("portal_token")
    if not token:
        return None
    # Validate token against Redis
    billing = get_billing()
    raw = billing.r.get(f"portal:session:{token}")
    if raw:
        return json.loads(raw)
    return None

# ─────────────────────────────────────────────────────────────
# /chat — Página principal de chat del portal
# Sirve el chat para usuarios logueados, usando su API key (que factura su uso)
# ─────────────────────────────────────────────────────────────

@app.get("/chat", response_class=HTMLResponse)
def chat_page(request: Request):
    """Chat público — sin login funciona con la master key (billing registra uso).
    Con sesión, se factura con la key del usuario del portal."""
    user = get_current_user(request)
    if user:
        billing = get_billing()
        keys = billing.list_keys(user["email"])
        api_key = keys[0]["key"] if keys else "ojoia_live_dWJVU541r3mphLlfi4ZvMn1tMbVe74WacxCSW0N9mSg"
    else:
        api_key = "ojoia_live_dWJVU541r3mphLlfi4ZvMn1tMbVe74WacxCSW0N9mSg"

    # Read the chat HTML template and fill it with the user's API key.
    # The chat uses /v1/* from the same origin (portal), so no CORS issues.
    html_src = Path("/home/sam/chatrd/test_qwen35b.html").read_text(encoding="utf-8")
    html = html_src.replace(
        'const DEFAULT_KEY = ""',
        f'const DEFAULT_KEY = "{api_key}"'
    ).replace(
        'const BUS_URL = "http://127.0.0.1:8200";',
        'const BUS_URL = "";'
    ).replace(
        '`${BUS_URL}/v1/chat/completions`',
        '`/v1/chat/completions`'
    )
    return HTMLResponse(html)


@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    user = get_current_user(request)
    return templates.TemplateResponse(request, "index.html", {"user": user})

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse(request, "login.html", {})

@app.post("/login")
async def login(request: Request, email: str = Form(...), password: str = Form(...)):
    billing = get_billing()
    # Simple auth - in production use proper password hashing
    user_data = billing.r.get(f"portal:user:{email}")
    if not user_data:
        return RedirectResponse("/login?error=invalid", status_code=302)
    user = json.loads(user_data)
    pw_hash = hashlib.sha256(password.encode()).hexdigest()
    if pw_hash != user.get("password_hash"):
        return RedirectResponse("/login?error=invalid", status_code=302)
    # Create session
    token = secrets.token_urlsafe(32)
    billing.r.setex(f"portal:session:{token}", 86400, json.dumps(user))
    response = RedirectResponse("/dashboard", status_code=302)
    response.set_cookie("portal_token", token, httponly=True, max_age=86400)
    return response

@app.get("/register", response_class=HTMLResponse)
async def register_page(request: Request):
    return templates.TemplateResponse(request, "register.html", {"plans": {"free": {"tokens": 1000000, "price": 0}}})

@app.post("/register")
async def register(request: Request, name: str = Form(...), email: str = Form(...), password: str = Form(...), plan: str = Form("free")):
    billing = get_billing()
    # Check if user exists
    if billing.r.get(f"portal:user:{email}"):
        return RedirectResponse("/register?error=exists", status_code=302)
    # Create user
    pw_hash = hashlib.sha256(password.encode()).hexdigest()
    user = {
        "email": email,
        "name": name,
        "password_hash": pw_hash,
        "plan": plan,
        "created_at": int(time.time())
    }
    billing.r.set(f"portal:user:{email}", json.dumps(user))
    # Create API key for user
    api_key = billing.create_key(email, name, plan)
    # Auto-login
    token = secrets.token_urlsafe(32)
    billing.r.setex(f"portal:session:{token}", 86400, json.dumps(user))
    response = RedirectResponse("/dashboard", status_code=302)
    response.set_cookie("portal_token", token, httponly=True, max_age=86400)
    return response

@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    billing = get_billing()
    email = user["email"]
    usage = billing.get_client_usage(email, "month")
    keys = billing.list_keys(email)
    quota = billing.get_quota_status(email, user.get("plan", "free"))
    return templates.TemplateResponse(request, "dashboard.html", {
        "user": user,
        "usage": usage,
        "keys": keys,
        "quota": quota
    })

@app.get("/models", response_class=HTMLResponse)
async def models_page(request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    billing = get_billing()
    keys = billing.list_keys(user["email"])
    api_key = keys[0]["key"] if keys else ""
    # Base URL pública para que el usuario la copie en Kilo Code
    scheme = request.url.scheme
    host = request.url.netloc or "models.ojoia.com.do"
    base_url = f"{scheme}://{host}"
    return templates.TemplateResponse(request, "models.html", {
        "user": user,
        "api_key": api_key,
        "base_url": base_url
    })

@app.post("/api/chat")
async def api_chat(request: Request):
    """Chat con opción de imagen o video.
    - Soporta content-type multipart/form-data (con archivos)
    - Video: extrae frames con ffmpeg, arma grid 4x4 con gateway_resize
    - Imagen: redimensiona con gateway_resize
    """
    content_type = request.headers.get("content-type", "")
    api_key = None
    user = get_current_user(request)

    # ── multipart (imagen o video) ────────────────────────────────────
    if "multipart/form-data" in content_type:
        form = await request.form()
        model = form.get("model", "qwen3vl8b")
        text  = form.get("text", "")
        image_file = form.get("image")
        video_file = form.get("video")
        max_tokens = int(form.get("max_tokens", 500))

        # auth
        if not user:
            raise HTTPException(401, "Unauthorized")
        api_key = await _get_apikey(user)

        media_parts = []
        media_b64 = None
        media_type_desc = "ninguna"

        # ── Procesar IMAGEN ──
        if image_file and hasattr(image_file, "filename"):
            img_bytes = await image_file.read()
            if HAS_RESIZE:
                img_bytes = resize_image(img_bytes, max_size=768)
            media_b64 = base64.b64encode(img_bytes).decode()
            media_type_desc = f"imagen ({len(img_bytes)//1024}KB)"

        # ── Procesar VIDEO → frames → grid ──
        elif video_file and hasattr(video_file, "filename"):
            video_bytes = await video_file.read()
            media_b64, media_type_desc = await _video_to_grid(video_bytes)

        # Armar el mensaje con imagen si existe
        messages = [{"role": "user", "content": text or "¿Qué ves en la imagen?"}]
        if media_b64:
            messages[0]["content"] = [
                {"type": "text", "text": messages[0]["content"]},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{media_b64}"}}
            ]

        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(
                f"{SERVICE_BUS}/v1/chat/completions",
                json={"model": model, "messages": messages, "max_tokens": max_tokens},
                headers={"Authorization": f"Bearer {api_key}"}
            )
            return JSONResponse(resp.json())

    # ── JSON puro (sin archivo) ────────────────────────────────
    else:
        data = await request.json()
        model = data.get("model", "qwen7b")
        messages = data.get("messages", [])
        max_tokens = data.get("max_tokens", 500)

        if not user:
            raise HTTPException(401, "Unauthorized")
        api_key = await _get_apikey(user)

        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(
                f"{SERVICE_BUS}/v1/chat/completions",
                json={"model": model, "messages": messages, "max_tokens": max_tokens},
                headers={"Authorization": f"Bearer {api_key}"}
            )
            return JSONResponse(resp.json())


async def _video_to_grid(video_bytes: bytes) -> tuple[str | None, str]:
    """Extrae frames de un video, arma un grid 4x4 con gateway_resize,
    y lo devuelve en base64."""
    if not HAS_RESIZE:
        return None, "resize no disponible"

    import tempfile, os, subprocess, asyncio

    # Guardar video temporalmente
    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as f:
        f.write(video_bytes)
        vs_path = f.name

    try:
        frames_dir = tempfile.mkdtemp()
        # Extraer N=12 frames con ffmpeg
        cmd = [
            "ffmpeg", "-i", vs_path,
            "-vf", "fps=1,scale=384:384",
            "-frames", "12",
            "-q:v", "2",
            f"{frames_dir}/frame_%03d.jpg"
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
        )
        await proc.wait()

        # Leer frames
        import glob
        frame_paths = sorted(glob.glob(f"{frames_dir}/frame_*.jpg"))
        if not frame_paths:
            return None, "sin frames extraidos"

        frames = [open(p, "rb").read() for p in frame_paths]
        grid_bytes = create_grid_image(frames, max_size=384)
        media_b64 = base64.b64encode(grid_bytes).decode()

        # Cleanup
        for p in frame_paths:
            os.unlink(p)
        os.rmdir(frames_dir)
        return media_b64, f"video-grid({len(frames)} frames)"
    except Exception as e:
        return None, f"video error: {e}"
    finally:
        try: os.unlink(vs_path)
        except: pass

@app.api_route("/v1/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"])
async def v1_proxy(path: str, request: Request):
    """Proxy OpenAI-compatible /v1/* al Service Bus para que Kilo Code se conecte.
    Kilo manda su propia API key en Authorization: Bearer -> se reenvía tal cual
    al bus (que es quien valida la key contra Redis)."""
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(401, {"error": "API key requerida",
                                  "detail": "Authorization: Bearer ojoia_live_..."})
    body = await request.body()
    url = f"{SERVICE_BUS}/v1/{path}"
    fwd_headers = {k: v for k, v in request.headers.items()
                   if k.lower() not in {"host", "content-length"}}
    async with httpx.AsyncClient(timeout=300) as client:
        resp = await client.request(request.method, url, content=body, headers=fwd_headers)
        content = resp.content
        headers = {k: v for k, v in resp.headers.items()
                   if k.lower() not in {"content-length", "transfer-encoding", "connection"}}
        return Response(content=content, status_code=resp.status_code,
                        media_type=resp.headers.get("content-type", "application/json"),
                        headers=headers)

@app.get("/logout")
async def logout():
    response = RedirectResponse("/", status_code=302)
    response.delete_cookie("portal_token")
    return response

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8006))
    uvicorn.run(app, host="0.0.0.0", port=port)
