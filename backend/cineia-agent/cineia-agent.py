#!/usr/bin/env python3
"""
cineia-agent.py — Agente del nodo CineIA
========================================
Corre en el servidor CineIA (puerto 8300). Expone métricas locales
y acepta comandos del megapanel maestro para controlar servicios.

Endpoints:
  GET  /api/metrics         — GPUs, RAM, disco, carga, procesos
  GET  /api/services        — estado de servicios systemd
  POST /api/control         — {service, action} → start/stop/restart/enable/disable
"""
import json
import os
import platform
import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    from shared.config import NODE_ID, NODE_NAME
except ImportError:
    NODE_ID = os.environ.get("NODE_ID", "cineia")
    NODE_NAME = os.environ.get("NODE_NAME", "CineIA Worker")

import hmac
import os
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    from shared.config import NODE_ID, NODE_NAME
except ImportError:
    NODE_ID = os.environ.get("NODE_ID", "cineia")
    NODE_NAME = os.environ.get("NODE_NAME", "CineIA Worker")

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
import uvicorn

app = FastAPI(title="CineIA Agent", version="1.0")

# ── Auth ─────────────────────────────────────────────────────
_AGENT_TOKEN_FILE = Path("/home/sam/.cineia_agent_token")

def _load_agent_token() -> str:
    tok = (os.environ.get("CINEIA_AGENT_TOKEN") or "").strip()
    if not tok and _AGENT_TOKEN_FILE.exists():
        tok = _AGENT_TOKEN_FILE.read_text().strip()
    return tok

AGENT_TOKEN = _load_agent_token()

def _check_token(auth: str | None):
    if not AGENT_TOKEN:
        return  # sin token configurado, no exige auth (dev)
    if not auth or not auth.startswith("Bearer "):
        raise HTTPException(401, "Authorization requerido")
    tok = auth.replace("Bearer ", "").strip()
    if not hmac.compare_digest(tok, AGENT_TOKEN):
        raise HTTPException(401, "Token invalido")

@app.middleware("http")
async def _require_auth(request: Request, call_next):
    if request.url.path.startswith("/api/control"):
        try:
            _check_token(request.headers.get("authorization"))
        except HTTPException:
            return JSONResponse({"detail": "Token invalido"}, status_code=401)
    return await call_next(request)

LOCAL_SERVICES = [
    {"id": "comfyui.service", "port": 8006, "level": "user", "gpu": 2, "name": "ComfyUI (Wan)", "managed": True},
    {"id": "movie_server.service", "port": 8004, "level": "user", "gpu": 2, "name": "CineIA Movie Server"},
    {"id": "cineia_studio_server.service", "port": 8095, "level": "user", "gpu": -1, "name": "CineIA Studio API"},
    {"id": "post_server.service", "port": 8014, "level": "user", "gpu": 2, "name": "Post-Production (RIFE/Lipsync)"},
    {"id": "audio_server.service", "port": 8013, "level": "user", "gpu": 2, "name": "Audio Server"},
    {"id": "f5_tts_server.service", "port": 8017, "level": "user", "gpu": 2, "name": "F5-TTS"},
    {"id": "gpu1-monitor.service", "port": 0, "level": "user", "gpu": -1, "name": "GPU1 Monitor"},
]


def run_cmd(cmd: str, timeout: int = 10) -> str:
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip() or r.stderr.strip()
    except Exception as e:
        return f"ERROR: {e}"


@app.get("/api/metrics")
async def metrics():
    gpus = []
    out = run_cmd(
        "nvidia-smi --query-gpu=index,name,memory.total,memory.used,memory.free,"
        "temperature.gpu,utilization.gpu,power.draw --format=csv,noheader,nounits"
    )
    if out and "ERROR" not in out:
        for line in out.splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 7:
                gpus.append({
                    "index": int(parts[0]), "name": parts[1],
                    "mem_total_mb": int(parts[2]), "mem_used_mb": int(parts[3]),
                    "mem_free_mb": int(parts[4]), "temp_c": int(parts[5]),
                    "util_pct": int(parts[6]),
                    "power_w": float(parts[7]) if len(parts) > 7 and parts[7] not in ("", "[N/A]") else None,
                })
    try:
        load1, load5, load15 = os.getloadavg()
    except Exception:
        load1 = load5 = load15 = 0
    mem = run_cmd("free -m | awk '/Mem:/{print $2,$3,$6}'")
    mem_parts = mem.split() if mem and "ERROR" not in mem else [0, 0, 0]
    disk = run_cmd("df -h / --output=size,used,avail,pcent | tail -1").split()
    return {
        "node_id": NODE_ID, "node_name": NODE_NAME,
        "timestamp": datetime.now().isoformat(), "hostname": platform.node(),
        "gpus": gpus, "load": {"1": load1, "5": load5, "15": load15},
        "ram": {"total_mb": int(mem_parts[0]), "used_mb": int(mem_parts[1]), "cache_mb": int(mem_parts[2])},
        "disk": {"total": disk[0] if disk else "", "used": disk[1] if len(disk) > 1 else "",
                 "free": disk[2] if len(disk) > 2 else "", "pct": disk[3] if len(disk) > 3 else ""},
    }


@app.get("/api/services")
async def services():
    svcs = []
    for s in LOCAL_SERVICES:
        flag = "--user" if s["level"] == "user" else ""
        active = run_cmd(f"systemctl {flag} is-active --quiet {s['id']}") == ""
        enabled = run_cmd(f"systemctl {flag} is-enabled {s['id']} 2>/dev/null")
        svcs.append({**s, "active": active, "enabled": enabled})
    return {"node_id": NODE_ID, "services": svcs}


@app.post("/api/control")
async def control(req: dict):
    sid = req.get("service", "")
    action = req.get("action", "")
    if action not in ("start", "stop", "restart", "enable", "disable"):
        raise HTTPException(400, "action must be start|stop|restart|enable|disable")
    if not sid.endswith(".service"):
        sid += ".service"
    level = "user"
    for s in LOCAL_SERVICES:
        if s["id"] == sid:
            level = s["level"]
            break
    flag = "--user" if level == "user" else ""
    out = run_cmd(f"systemctl {flag} {action} {sid}", timeout=30)
    return {"service": sid, "action": action, "result": out or "ok"}


@app.get("/api/health")
async def health():
    return {"status": "ok", "node": NODE_ID}


if __name__ == "__main__":
    port = int(os.environ.get("CINEIA_AGENT_PORT", "8300"))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
