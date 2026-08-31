#!/usr/bin/env python3
"""OjoIA Portal — User-facing portal for model access."""
import os
import sys
import json
import secrets
import hashlib
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from billing import BillingStore

from fastapi import FastAPI, Request, Form, HTTPException, Depends, Header
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
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
    return templates.TemplateResponse(request, "models.html", {
        "user": user,
        "api_key": api_key
    })

@app.post("/api/chat")
async def api_chat(request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(401, "Unauthorized")
    data = await request.json()
    model = data.get("model", "qwen35b")
    messages = data.get("messages", [])
    # Get user's API key
    billing = get_billing()
    keys = billing.list_keys(user["email"])
    if not keys:
        raise HTTPException(403, "No API key")
    api_key = keys[0]["key"]
    # Forward to Service Bus
    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(
            f"{SERVICE_BUS}/{model}/v1/chat/completions",
            json={"model": model, "messages": messages, "max_tokens": data.get("max_tokens", 500)},
            headers={"Authorization": f"Bearer {api_key}"}
        )
        return JSONResponse(resp.json())

@app.get("/logout")
async def logout():
    response = RedirectResponse("/", status_code=302)
    response.delete_cookie("portal_token")
    return response

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8006))
    uvicorn.run(app, host="0.0.0.0", port=port)
