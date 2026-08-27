#!/usr/bin/env python3
"""
OjoIA Server Megapanel — Panel de Control Completo del Servidor
================================================================
Puerto 9001 — Dashboard unificado para gestión de producción.

Funcionalidades:
  - Estado de TODOS los servicios (system + user) con GPU asignada
  - Métricas en tiempo real de las 3 GPUs (VRAM, temp, utilización)
  - Logs centralizados (journalctl + health-monitor incidents)
  - Reiniciar/detener/iniciar cualquier servicio (POST)
  - Estado de colas de CineIA (queue.py ProjectStore)
  - Modo Mantenimiento: cambia tráfico a API externa
  - Estado del Cloudflare Tunnel
  - Información de carga del sistema (CPU, RAM, disco)
  - Swarm-ready: detecta otros nodos vía /nodes (futuro)

Cloudflare Tunnel expondrá: server-admin.ojoia.com.do -> localhost:9001
"""

import asyncio
import json
import os
import platform
import subprocess
import time
from datetime import datetime
from pathlib import Path

import hmac
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel
import httpx

app = FastAPI(title="OjoIA Server Megapanel", version="1.2")

# ── Carga de configuración desde ojoia.env ─────────────────────────────────
# Variables de entorno para multi-nodo y service bus. Ver NODO_CONEXION.md.
_OJOIA_ENV_FILE = Path("/opt/ojoia/config/ojoia.env")


def _load_ojoia_env() -> dict:
    """Carga KEY=VALUE de /opt/ojoia/config/ojoia.env al entorno."""
    env_vars = {}
    if _OJOIA_ENV_FILE.exists():
        try:
            for line in _OJOIA_ENV_FILE.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    k, v = line.split("=", 1)
                    k, v = k.strip(), v.strip().strip('"').strip("'")
                    os.environ[k] = v
                    env_vars[k] = v
        except Exception as e:
            print(f"[megapanel] ERROR leyendo {_OJOIA_ENV_FILE}: {e}")
    return env_vars


_OJOIA_ENV = _load_ojoia_env()
NODE_ID = os.environ.get("NODE_ID", platform.node())
CINEIA_AGENT_URL = os.environ.get("CINEIA_AGENT_URL", "")
CINEIA_AGENT_TOKEN = os.environ.get("CINEIA_AGENT_TOKEN", "")
SERVICE_BUS_PORT = int(os.environ.get("SERVICE_BUS_PORT", "8200"))
SERVICE_BUS_URL = f"http://127.0.0.1:{SERVICE_BUS_PORT}"

# ── Auth: Bearer token (defense in depth) ────────────────────────────────
# El panel está expuesto vía túnel Cloudflare (server-admin.ojoia.com.do).
# Antes: cero auth + POST /api/control/{id}/{action} ejecutaba systemctl sin
# validación -> RCE remoto. Ahora se exige Authorization: Bearer <MEGAPANEL_TOKEN>
# en TODOS los /api/* (excepto / que sirve el HTML). El token se carga desde
# env MEGAPANEL_TOKEN, /opt/ojoia/config/ojoia.env, o /home/sam/.ojoia_megapanel_token.
# Si no hay token configurado, EL PANEL SE NIEGA A ARRANCAR (fail-loud) para que
# nunca quede expuesto por omisión.
_MEGAPANEL_TOKEN_FILE = Path("/home/sam/.ojoia_megapanel_token")


def _load_megapanel_token() -> str:
    tok = (os.environ.get("MEGAPANEL_TOKEN") or "").strip()
    if not tok and _MEGAPANEL_TOKEN_FILE.exists():
        try:
            tok = _MEGAPANEL_TOKEN_FILE.read_text().strip()
        except Exception as e:
            print(f"[megapanel] ERROR leyendo token file {e}")
    if not tok:
        print("[megapanel] FATAL: MEGAPANEL_TOKEN no configurado (env o "
              f"{_MEGAPANEL_TOKEN_FILE}). Panel expuesto a internet sin auth -> "
              "negandose a arrancar. Generar con: python -c \"import secrets; "
              "print(secrets.token_urlsafe(32))\" y guardar en el archivo.")
        raise SystemExit(2)
    return tok


MEGAPANEL_TOKEN = _load_megapanel_token()

# CORS: solo el propio panel (servido por /) y, si hace falta, la lista
# explicita. NUNCA wildcard + credentials (invalido por spec).
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://server-admin.ojoia.com.do", "http://localhost:9001",
                   "http://127.0.0.1:9001"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
    max_age=600,
)

BASE_DIR = Path(__file__).parent
HEALTH_MONITOR_URL = f"http://localhost:{os.environ.get('HEALTH_MONITOR_PORT','9000')}"
MAINTENANCE_FLAG = Path("/home/sam/.ojoia_maintenance_mode")


def _check_token(authorization: str | None) -> None:
    """Valida Authorization: Bearer <token>. Lanza 401 si invalido."""
    if not authorization:
        raise HTTPException(status_code=401, detail="Authorization requerido")
    tok = authorization.replace("Bearer ", "").strip()
    if not tok or not hmac.compare_digest(tok, MEGAPANEL_TOKEN):
        raise HTTPException(status_code=401, detail="Token invalido")


@app.middleware("http")
async def _require_auth(request: Request, call_next):
    """Exige Bearer en todos los /api/* (no en / que sirve el HTML)."""
    if request.url.path == "/" or request.url.path.startswith("/api/"):
        if request.url.path.startswith("/api/"):
            try:
                _check_token(request.headers.get("authorization"))
            except HTTPException:
                return JSONResponse({"detail": "Token invalido"},
                                    status_code=401)
    return await call_next(request)


# ─── Tabla maestra de servicios (idéntica al health_monitor) ─────────────────

SERVICES = [
    {"id": "tunnel.service", "port": 0, "level": "system", "gpu": -1, "name": "Cloudflare Tunnel", "kind": "network"},
    {"id": "api-eva.service", "port": 8005, "level": "system", "gpu": -1, "name": "OjoIA API Eva", "kind": "api"},
    {"id": "qwen.service", "port": 8004, "level": "system", "gpu": 0, "name": "Qwen VL-7B (SGLang)", "kind": "llm"},
    {"id": "whisper.service", "port": 8008, "level": "system", "gpu": 1, "name": "Whisper Turbo ASR", "kind": "asr"},
    {"id": "yolo-server.service", "port": 8002, "level": "system", "gpu": 1, "name": "YOLO Pose", "kind": "vision"},
    {"id": "qwen14b.service", "port": 8015, "level": "user", "gpu": 1, "name": "Qwen 14B (SGLang)", "kind": "llm"},
    {"id": "chatrd.service", "port": 8010, "level": "user", "gpu": -1, "name": "ChatRD API", "kind": "api"},
    {"id": "admin_panel.service", "port": 8030, "level": "user", "gpu": -1, "name": "ChatRD Admin (legacy)", "kind": "api"},
    {"id": "comfyui.service", "port": 8006, "level": "user", "gpu": 2, "name": "ComfyUI (Wan)", "kind": "image", "managed": True},
    {"id": "movie_server.service", "port": 8090, "level": "user", "gpu": 2, "name": "CineIA Movie Server", "kind": "video"},
    {"id": "cineia_studio_server.service", "port": 8095, "level": "user", "gpu": -1, "name": "CineIA Studio API", "kind": "api"},
    {"id": "post_server.service", "port": 8014, "level": "user", "gpu": 2, "name": "Post-Production (RIFE/Lipsync)", "kind": "post"},
    {"id": "audio_server.service", "port": 8013, "level": "user", "gpu": 2, "name": "Audio Server (MusicGen)", "kind": "audio"},
    {"id": "f5_tts_server.service", "port": 8017, "level": "user", "gpu": 2, "name": "F5-TTS", "kind": "audio"},
    {"id": "health-monitor.service", "port": 9000, "level": "user", "gpu": -1, "name": "Health Monitor (this)", "kind": "infra"},

    # ── Stack CineIA nuevo (Docker, ver docker-compose.h3.yml) ──
    {"id": "cineia-h3-worker", "port": 8000, "level": "docker", "gpu": -1, "name": "MiniMax H3 Worker (vLLM-Omni)", "kind": "video", "managed": "docker", "container": "cineia-h3-worker", "compose_file": "/home/sam/proyecto movie/docker-compose.h3.yml"},
    {"id": "cineia-flux", "port": 8020, "level": "docker", "gpu": 2, "name": "Flux GGUF (master images + batch)", "kind": "image", "managed": "docker", "container": "cineia-flux", "compose_file": "/home/sam/proyecto movie/docker-compose.h3.yml"},
    {"id": "cineia-f5tts", "port": 8017, "level": "docker", "gpu": -1, "name": "F5-TTS (CPU)", "kind": "audio", "managed": "docker", "container": "cineia-f5tts", "compose_file": "/home/sam/proyecto movie/docker-compose.h3.yml"},
    {"id": "cineia-musicgen", "port": 8023, "level": "docker", "gpu": -1, "name": "MusicGen (CPU)", "kind": "audio", "managed": "docker", "container": "cineia-musicgen", "compose_file": "/home/sam/proyecto movie/docker-compose.h3.yml"},
    {"id": "cineia-realesrgan", "port": 8021, "level": "docker", "gpu": -1, "name": "RealESRGAN (CPU)", "kind": "upscale", "managed": "docker", "container": "cineia-realesrgan", "compose_file": "/home/sam/proyecto movie/docker-compose.h3.yml"},
    {"id": "cineia-stable-audio", "port": 8025, "level": "docker", "gpu": -1, "name": "Stable Audio (stub)", "kind": "audio", "managed": "docker", "container": "cineia-stable-audio", "compose_file": "/home/sam/proyecto movie/docker-compose.h3.yml"},
    {"id": "cineia-sadtalker", "port": 8026, "level": "docker", "gpu": -1, "name": "SadTalker (stub)", "kind": "lipsync", "managed": "docker", "container": "cineia-sadtalker", "compose_file": "/home/sam/proyecto movie/docker-compose.h3.yml"},
    {"id": "cineia-redis", "port": 6379, "level": "docker", "gpu": -1, "name": "Redis (cola)", "kind": "infra", "managed": "docker", "container": "cineia-redis", "compose_file": "/home/sam/proyecto movie/docker-compose.h3.yml"},

    # ── Service Bus (OpenAI-compatible) ──
    {"id": "service-bus", "port": 8200, "level": "system", "gpu": -1, "name": "Service Bus (OpenAI API)", "kind": "infra"},
]

# ── DOCKER_MAP: service_id -> container name (para start/stop via docker) ──
DOCKER_MAP = {
    "qwen9b.service":  "ai-qwen-9b-1",
    "qwen.service":    "qwen-7b",
    "qwen35b.service": "qwen-35b-a3b",
    "whisper.service": "whisper-turbo",
    "yolo-server.service": "yolo-pose",
    # CineIA Docker
    "cineia-h3-worker":  "cineia-h3-worker",
    "cineia-flux":       "cineia-flux",
    "cineia-f5tts":      "cineia-f5tts",
    "cineia-musicgen":   "cineia-musicgen",
    "cineia-realesrgan": "cineia-realesrgan",
    "cineia-stable-audio": "cineia-stable-audio",
    "cineia-sadtalker":    "cineia-sadtalker",
    "cineia-redis":        "cineia-redis",
}

# ── Nodos remotos configurados (CINEIA_AGENT_URL en ojoia.env apunta al nodo
# hermano; el nombre del nodo remoto se infiere del hostname o de la URL). ──
REMOTE_NODES = []
if CINEIA_AGENT_URL:
    # Intentar inferir el node_id del nodo remoto desde la URL o usar "ojoia"
    # por convención (este nodo = cineia, el otro = ojoia según docs).
    remote_node_id = "ojoia"
    for env_k, env_v in _OJOIA_ENV.items():
        if env_k == "REMOTE_NODE_ID":
            remote_node_id = env_v
    REMOTE_NODES.append({
        "node_id": remote_node_id,
        "agent_url": CINEIA_AGENT_URL,
        "agent_token": CINEIA_AGENT_TOKEN,
        "name": f"{remote_node_id} (nodo remoto)",
    })


def run_cmd(cmd, timeout=8):
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip() or r.stderr.strip()
    except Exception as e:
        return f"ERROR: {e}"


def service_active(sid: str, level: str) -> bool:
    cmd = "systemctl"
    if level == "user":
        cmd += " --user"
    elif level == "docker":
        # Para Docker: docker inspect --format='{{.State.Running}}' container
        container = _docker_container_for(sid)
        if container:
            out = run_cmd(f"docker inspect --format='{{{{.State.Running}}}}' {container} 2>/dev/null")
            return out.strip() == "true"
        return False
    return run_cmd(f"{cmd} is-active --quiet {sid}") == ""


def _docker_container_for(sid: str) -> str | None:
    """Devuelve el nombre del container docker asociado a un service id."""
    for s in SERVICES:
        if s["id"] == sid and s.get("managed") == "docker":
            return s.get("container")
    return DOCKER_MAP.get(sid)


def _docker_compose_for(sid: str) -> str | None:
    """Devuelve la ruta del docker-compose asociado a un service id."""
    for s in SERVICES:
        if s["id"] == sid and s.get("managed") == "docker":
            return s.get("compose_file")
    return None


def service_enabled(sid: str, level: str) -> str:
    cmd = "systemctl"
    if level == "user":
        cmd += " --user"
    return run_cmd(f"{cmd} is-enabled {sid} 2>/dev/null")


def measure_cpu_power_w() -> float | None:
    """Lee consumo CPU via RAPL (intel-rapl). Fallback a None si no disponible."""
    rapl_path = Path("/sys/class/powercap/intel-rapl:0/energy_uj")
    if not rapl_path.exists():
        return None
    try:
        e1 = int(rapl_path.read_text().strip())
        t1 = time.monotonic()
        time.sleep(0.1)
        e2 = int(rapl_path.read_text().strip())
        t2 = time.monotonic()
        microjoules = e2 - e1
        seconds = t2 - t1
        if microjoules < 0:
            microjoules += 2**32  # overflow
        return round(microjoules / 1e6 / seconds, 1)
    except Exception:
        return None


async def fetch_remote_node(node: dict) -> dict:
    """Consulta el agent de un nodo remoto (CINEIA_AGENT_URL) y devuelve su estado."""
    result = {
        "node_id": node["node_id"],
        "name": node["name"],
        "agent_url": node["agent_url"],
        "online": False,
        "services": [],
        "gpus": [],
    }
    try:
        headers = {}
        if node.get("agent_token"):
            headers["Authorization"] = f"Bearer {node['agent_token']}"
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.get(f"{node['agent_url']}/status", headers=headers)
            if r.status_code == 200:
                data = r.json()
                result["online"] = True
                result["services"] = data.get("services", [])
                result["gpus"] = data.get("gpus", [])
    except Exception as e:
        result["error"] = str(e)
    return result


# ─── Endpoints API ───────────────────────────────────────────────────────────

@app.get("/api/status")
async def status():
    """Estado completo del servidor en un solo snapshot."""
    # GPU info
    gpus = []
    out = run_cmd("nvidia-smi --query-gpu=index,name,memory.total,memory.used,memory.free,temperature.gpu,utilization.gpu,power.draw --format=csv,noheader,nounits")
    if out and "ERROR" not in out:
        for line in out.splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 7:
                gpus.append({
                    "index": int(parts[0]),
                    "name": parts[1],
                    "mem_total_mb": int(parts[2]),
                    "mem_used_mb": int(parts[3]),
                    "mem_free_mb": int(parts[4]),
                    "temp_c": int(parts[5]),
                    "util_pct": int(parts[6]),
                    "power_w": float(parts[7]) if len(parts) > 7 and parts[7] not in ("", "[N/A]") else None,
                    "mem_pct": round(int(parts[3]) / max(int(parts[2]), 1) * 100, 1),
                })

    # load avg
    try:
        load1, load5, load15 = os.getloadavg()
    except Exception:
        load1 = load5 = load15 = 0

    # memory
    meminfo = run_cmd("free -m --wide").splitlines()
    mem_used = mem_free = mem_total = ram_cache = 0
    for line in meminfo:
        if line.startswith("Mem:"):
            p = line.split()
            if len(p) >= 7:
                mem_total, mem_used, mem_free = int(p[1]), int(p[2]), int(p[3])
                ram_cache = int(p[5]) if len(p) > 5 else 0
            break
    swap_used = 0
    for line in meminfo:
        if line.startswith("Swap:"):
            p = line.split()
            if len(p) >= 3:
                swap_used = int(p[2])
            break

    # disk
    disk_out = run_cmd("df -h / --output=size,used,avail,pcent 2>/dev/null | tail -1")
    disk_total = disk_used = disk_free = ""
    disk_pct = 0
    if disk_out and "ERROR" not in disk_out:
        parts = disk_out.split()
        if len(parts) >= 4:
            disk_total, disk_used, disk_free, disk_pct_str = parts[:4]
            disk_pct = int(disk_pct_str.replace("%", "")) if disk_pct_str.endswith("%") else 0

    # services
    svcs = []
    for s in SERVICES:
        active = service_active(s["id"], s["level"])
        enabled = service_enabled(s["id"], s["level"])
        s_copy = {**s, "active": active, "enabled": enabled, "node": NODE_ID}
        svcs.append(s_copy)

    # uptime
    uptime_s = 0
    try:
        with open("/proc/uptime") as f:
            uptime_s = int(float(f.readline().split()[0]))
    except Exception:
        pass

    # health-monitor incidents
    incidents = []
    try:
        async with httpx.AsyncClient(timeout=3) as c:
            r = await c.get(f"{HEALTH_MONITOR_URL}/status")
            if r.status_code == 200:
                data = r.json()
                incidents = data.get("incidents", [])[-20:]
    except Exception:
        pass

    # maintenance mode
    maintenance = MAINTENANCE_FLAG.exists()

    # power: CPU (RAPL) + GPUs (nvidia-smi)
    cpu_power_w = measure_cpu_power_w()
    gpu_power_w = sum(g.get("power_w") or 0 for g in gpus)
    total_power_w = round((cpu_power_w or 0) + gpu_power_w, 1)

    return {
        "timestamp": datetime.now().isoformat(),
        "hostname": platform.node(),
        "node_id": NODE_ID,
        "uptime_s": uptime_s,
        "load": {"1": load1, "5": load5, "15": load15},
        "ram": {"total_mb": mem_total, "used_mb": mem_used, "free_mb": mem_free, "cache_mb": ram_cache, "pct": round(mem_used / max(mem_total, 1) * 100, 1)},
        "swap": {"used_mb": swap_used},
        "disk": {"total": disk_total, "used": disk_used, "free": disk_free, "pct": disk_pct},
        "gpus": gpus,
        "services": svcs,
        "incidents": incidents,
        "maintenance_mode": maintenance,
        "health_monitor_url": HEALTH_MONITOR_URL,
        "power": {
            "cpu_w": cpu_power_w,
            "gpu_w": round(gpu_power_w, 1),
            "total_w": total_power_w,
        },
        "remote_nodes": [n["node_id"] for n in REMOTE_NODES],
        "service_bus_url": SERVICE_BUS_URL,
    }


@app.get("/api/nodes")
async def nodes():
    """Estado de todos los nodos: este nodo + nodos remotos configurados."""
    # Este nodo
    local = {
        "node_id": NODE_ID,
        "name": platform.node(),
        "online": True,
        "self": True,
    }
    # Nodos remotos
    remote = []
    for node in REMOTE_NODES:
        remote.append(await fetch_remote_node(node))
    return {"local": local, "remote": remote}


@app.post("/api/exec/{node_id}/{backend}")
async def exec_remote_model(node_id: str, backend: str, body: dict):
    """Ejecuta un modelo en un nodo remoto (o service bus local) via OpenAI API."""
    # Determinar URL destino
    if node_id == "local" or node_id == NODE_ID:
        # Local service bus
        url = f"{SERVICE_BUS_URL}/{backend}/v1/chat/completions"
        token = os.environ.get("SERVICE_BUS_TOKEN", "")
    else:
        # Buscar nodo remoto
        target = None
        for n in REMOTE_NODES:
            if n["node_id"] == node_id:
                target = n
                break
        if not target:
            raise HTTPException(404, f"Nodo {node_id} no encontrado")
        url = f"{target['agent_url']}/{backend}/v1/chat/completions"
        token = target.get("agent_token", "")

    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    try:
        async with httpx.AsyncClient(timeout=120) as c:
            r = await c.post(url, json=body, headers=headers)
            return JSONResponse(r.json(), status_code=r.status_code)
    except Exception as e:
        raise HTTPException(502, f"Error llamando {node_id}: {e}")


@app.get("/api/billing/clients")
async def billing_clients():
    """Lista clientes y uso (proxy a Redis billing store)."""
    try:
        import redis
        r = redis.Redis.from_url(os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0"))
        keys = r.keys("ojoia_billing:usage:*:total")
        clients = []
        for k in keys:
            cid = k.decode().split(":")[2]
            usage = r.get(k) or b"0"
            cost = r.get(f"ojoia_billing:cost:{cid}:total") or b"0"
            clients.append({
                "client_id": cid,
                "usage": int(usage),
                "cost_cents": int(cost),
            })
        return {"clients": clients}
    except Exception as e:
        return {"clients": [], "error": str(e)}


@app.get("/api/billing/stats")
async def billing_stats():
    """Stats agregadas últimas 24h."""
    try:
        import redis
        r = redis.Redis.from_url(os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0"))
        keys = r.keys("ojoia_billing:usage:24h:*")
        total_usage = sum(int(r.get(k) or 0) for k in keys)
        return {"window": "24h", "total_requests": total_usage}
    except Exception as e:
        return {"window": "24h", "total_requests": 0, "error": str(e)}


@app.get("/api/logs/{service_id}")
async def logs(service_id: str, lines: int = 100):
    """Obtiene logs journalctl de un servicio."""
    # seguridad: solo nombres alfanuméricos + .service
    if not all(c.isalnum() or c in ".-_" for c in service_id):
        raise HTTPException(400, "invalid service name")
    if not service_id.endswith(".service"):
        service_id += ".service"
    # detectar level
    level = "user"
    for s in SERVICES:
        if s["id"] == service_id:
            level = s["level"]
            break
    flag = "--user" if level == "user" else ""
    out = run_cmd(f"journalctl {flag} -u {service_id} -n {lines} --no-pager -o cat 2>&1")
    return {"service": service_id, "lines": out.splitlines()}


@app.post("/api/control/{service_id}/{action}")
async def control(service_id: str, action: str):
    """Controlar servicio: start | stop | restart | enable | disable."""
    if action not in ("start", "stop", "restart", "enable", "disable"):
        raise HTTPException(400, "action must be start|stop|restart|enable|disable")

    # Buscar el servicio
    svc = None
    for s in SERVICES:
        if s["id"] == service_id:
            svc = s
            break
    if not svc:
        raise HTTPException(404, f"servicio no encontrado: {service_id}")

    level = svc.get("level", "user")

    # ── Docker (usa DOCKER_MAP si no hay container/compose_file en el svc) ──
    is_docker = svc.get("managed") == "docker" or service_id in DOCKER_MAP
    if is_docker:
        container = svc.get("container") or DOCKER_MAP.get(service_id)
        compose_file = svc.get("compose_file")
        if action in ("enable", "disable"):
            return {
                "service": service_id,
                "action": action,
                "result": "servicio docker: enable/disable no aplica (usa docker compose up/down)",
            }
        if action == "start":
            cmd = f"docker start {container}" if container else f"docker compose -f {compose_file} start"
        elif action == "stop":
            cmd = f"docker stop {container}" if container else f"docker compose -f {compose_file} stop"
        elif action == "restart":
            cmd = f"docker restart {container}" if container else f"docker compose -f {compose_file} restart"
        else:
            raise HTTPException(400, f"acción no soportada para docker: {action}")
        out = run_cmd(cmd, timeout=60)
        return {"service": service_id, "action": action, "result": out or "ok", "container": container, "node": NODE_ID}

    # ── Nodo remoto ──
    remote_node_id = svc.get("node")
    if remote_node_id and remote_node_id != NODE_ID:
        target = None
        for n in REMOTE_NODES:
            if n["node_id"] == remote_node_id:
                target = n
                break
        if not target:
            raise HTTPException(404, f"Nodo remoto {remote_node_id} no encontrado")
        try:
            headers = {"Authorization": f"Bearer {MEGAPANEL_TOKEN}"}
            async with httpx.AsyncClient(timeout=30) as c:
                r = await c.post(f"{target['agent_url']}/api/control/{service_id}/{action}", headers=headers)
                return JSONResponse(r.json(), status_code=r.status_code)
        except Exception as e:
            raise HTTPException(502, f"Error enviando acción a {remote_node_id}: {e}")

    # ── Systemd ──
    if not service_id.endswith(".service"):
        service_id += ".service"
    level = "user"
    for s in SERVICES:
        if s["id"] == service_id:
            level = s["level"]
            break
    if level == "system" and action in ("restart", "start", "stop"):
        return {
            "service": service_id,
            "action": action,
            "result": (
                f"servicio system-level: systemd ya lo reinicia solo (Restart=on-failure). "
                f"Para forzar manualmente: sudo systemctl {action} {service_id}"
            ),
        }
    flag = "--user" if level == "user" else ""
    cmd = f"systemctl {flag} {action} {service_id}"
    out = run_cmd(cmd, timeout=30)
    return {"service": service_id, "action": action, "result": out or "ok", "node": NODE_ID}


@app.get("/api/gpu")
async def gpu_detail():
    out = run_cmd("nvidia-smi --query-compute-apps=pid,gpu_uuid,used_memory,process_name --format=csv")
    procs = []
    if out and "ERROR" not in out:
        for line in out.splitlines()[1:]:
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 4:
                procs.append({"pid": parts[0], "gpu_uuid": parts[1], "mem_mb": parts[2], "name": parts[3]})
    return {"processes": procs}


@app.get("/api/queues")
async def cineia_queues():
    """Estado de colas de CineIA (_projects.json)."""
    root = Path("/home/sam/proyecto movie/data")
    projects = []
    if root.exists():
        for pdir in root.iterdir():
            if not pdir.is_dir():
                continue
            jf = pdir / "project.json"
            if jf.exists():
                try:
                    data = json.loads(jf.read_text())
                    projects.append({
                        "id": data.get("id", pdir.name),
                        "title": data.get("title", pdir.name),
                        "status": data.get("status", "?"),
                        "progress": data.get("progress", 0),
                        "updated_at": data.get("updated_at", ""),
                        "error": data.get("error", None),
                    })
                except Exception as e:
                    projects.append({"id": pdir.name, "error": str(e)})
    # group by status
    by_status = {}
    for p in projects:
        st = p.get("status", "?")
        by_status.setdefault(st, []).append(p["id"])
    return {"count": len(projects), "by_status": by_status, "projects": projects}


class MaintenanceCmd(BaseModel):
    enable: bool
    external_api_url: str | None = None


@app.post("/api/maintenance")
async def maintenance(req: MaintenanceCmd):
    """Activa/desactiva modo mantenimiento (redirect tráfico a API externa)."""
    if req.enable:
        MAINTENANCE_FLAG.write_text(json.dumps({
            "enabled_at": datetime.now().isoformat(),
            "external_api": req.external_api_url,
        }))
        return {"maintenance": True, "msg": "Mantenimiento activado. Frontend redirige a API externa."}
    else:
        if MAINTENANCE_FLAG.exists():
            MAINTENANCE_FLAG.unlink()
        return {"maintenance": False, "msg": "Mantenimiento desactivado. Tráfico normal restaurado."}


@app.get("/api/tunnel")
async def tunnel_status():
    """Estado del Cloudflare tunnel."""
    active = service_active("tunnel.service", "system")
    # /etc/cloudflared/config.yml o /home/sam/.cloudflared/config.yml
    cfg = Path("/home/sam/.cloudflared/config.yml")
    config_text = ""
    if cfg.exists():
        config_text = cfg.read_text()
    return {
        "active": active,
        "config": config_text,
    }


# ─── UI HTML embebida ─────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def ui():
    return HTML_FILE


# Autenticación simple (opcional): en producción Cloudflare Access controla acceso.
# En /etc/cloudflared se gestiona el hostname server-admin.ojoia.com.do


HTML_FILE = """<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="utf-8">
<title>OjoIA Server Control</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
:root {
  --bg: #0d1117; --card: #161b22; --border: #30363d;
  --text: #e6edf3; --muted: #8b949e; --green: #3fb950; --red: #f85149;
  --yellow: #d29922; --blue: #58a6ff; --purple: #bc8cff;
}
* { box-sizing: border-box; }
body { background: var(--bg); color: var(--text); font-family: -apple-system, Segoe UI, Roboto, monospace; margin: 0; }
header { padding: 16px 24px; border-bottom: 1px solid var(--border); display: flex; justify-content: space-between; align-items: center; }
header h1 { margin: 0; font-size: 18px; }
.badge { padding: 4px 8px; border-radius: 12px; font-size: 12px; }
.badge-ok { background: rgba(63,185,80,.2); color: var(--green); }
.badge-err { background: rgba(248,81,73,.2); color: var(--red); }
.container { padding: 20px; max-width: 1400px; margin: 0 auto; }
.grid { display: grid; gap: 16px; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); }
.card { background: var(--card); border: 1px solid var(--border); border-radius: 8px; padding: 16px; }
.card h2 { margin: 0 0 12px; font-size: 14px; color: var(--muted); text-transform: uppercase; letter-spacing: .5px; }
.gpu-bar { background: var(--border); height: 8px; border-radius: 4px; margin: 4px 0 12px; overflow: hidden; }
.gpu-bar > div { height: 100%; background: linear-gradient(90deg, var(--green), var(--yellow), var(--red)); }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
tr { border-bottom: 1px solid var(--border); }
td, th { padding: 6px 8px; text-align: left; }
th { color: var(--muted); font-weight: normal; font-size: 11px; text-transform: uppercase; }
.dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 6px; }
.dot-on { background: var(--green); }
.dot-off { background: var(--red); }
.tag { padding: 2px 6px; border-radius: 3px; background: rgba(88,166,255,.15); color: var(--blue); font-size: 11px; margin-right: 4px; }
.tag.g0 { background: rgba(188,140,255,.15); color: var(--purple); }
.tag.g1 { background: rgba(63,185,80,.15); color: var(--green); }
.tag.g2 { background: rgba(210,153,34,.15); color: var(--yellow); }
.tag.cpu { background: rgba(139,148,158,.15); color: var(--muted); }
button { background: var(--border); color: var(--text); border: 0; padding: 4px 10px; border-radius: 4px; cursor: pointer; font-size: 12px; margin: 2px; }
button:hover { background: #444c56; }
button.r { color: var(--red); }
button.s { color: var(--green); }
.maint { border: 1px solid var(--yellow); color: var(--yellow); }
.log { background: #010409; border: 1px solid var(--border); border-radius: 6px; padding: 12px; font-family: monospace; font-size: 11px; max-height: 300px; overflow: auto; white-space: pre; }
input { background: var(--border); color: var(--text); border:0; padding:6px; border-radius:4px; }
.pill { padding: 1px 6px; border-radius: 3px; font-size: 11px; }
.pill.L1 { background: rgba(63,185,80,.15); color: var(--green); }
.pill.L5 { background: rgba(210,153,34,.15); color: var(--yellow); }
.pill.L15{ background: rgba(248,81,73,.15); color: var(--red); }
</style>
</head>
<body>
<header>
  <h1>OjoIA Server Control — <span id="host"></span></h1>
  <div>
    <span id="maint-badge" class="badge badge-ok">NORMAL</span>
    <button class="maint" onclick="toggleMaint(true)">Modo Mantenimiento</button>
    <button onclick="toggleMaint(false)">Salir Mantenimiento</button>
    <button onclick="refresh()">⟳</button>
  </div>
</header>
<div class="card" style="margin-bottom:16px">
  <h2>Auth</h2>
  <input id="tok" type="password" placeholder="Bearer token (MEGAPANEL_TOKEN)" style="width:60%">
  <button onclick="saveTok()">Guardar</button>
  <span id="tok-status" style="color:var(--muted);font-size:12px;margin-left:8px"></span>
</div>

<div class="container">
  <div class="grid">
    <div class="card">
      <h2>GPUs</h2>
      <div id="gpus"></div>
    </div>
    <div class="card">
      <h2>Sistema</h2>
      <div id="sys"></div>
    </div>
    <div class="card">
      <h2>Colas CineIA</h2>
      <div id="queues"></div>
    </div>
  </div>

  <div class="card" style="margin-top:16px">
    <h2>Servicios <span id="svc-count"></span></h2>
    <table>
      <thead><tr><th>Servicio</th><th>Puerto</th><th>GPU</th><th>Node</th><th>Estado</th><th>Enabled</th><th></th></tr></thead>
      <tbody id="services"></tbody>
    </table>
  </div>

  <div class="grid" style="margin-top:16px">
    <div class="card">
      <h2>⚡ Energía (CPU RAPL + GPUs)</h2>
      <div id="power"></div>
    </div>
    <div class="card">
      <h2>🌐 Nodos remotos <span id="remote-cnt"></span></h2>
      <div id="remote-nodes"></div>
    </div>
  </div>

  <div class="card" style="margin-top:16px">
    <h2>🧪 Ejecutar modelo (Service Bus OpenAI-compatible)</h2>
    <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:8px">
      <label>Nodo:
        <select id="exec-node" style="margin-left:4px"></select>
      </label>
      <label>Backend:
        <select id="exec-backend" style="margin-left:4px">
          <option value="qwen7b">qwen7b (8004)</option>
          <option value="qwen9b">qwen9b (8018)</option>
          <option value="qwen35b">qwen35b (8019)</option>
          <option value="whisper">whisper (8008)</option>
          <option value="yolo">yolo (8002)</option>
        </select>
      </label>
      <input id="exec-prompt" placeholder="Prompt..." style="flex:1;min-width:200px;padding:6px;background:var(--bg);color:var(--fg);border:1px solid var(--muted);border-radius:4px">
      <button onclick="runExec()">▶ Ejecutar</button>
    </div>
    <pre id="exec-out" style="background:var(--bg);padding:8px;border-radius:4px;max-height:200px;overflow:auto;font-size:12px;margin:0"></pre>
  </div>

  <div class="card" style="margin-top:16px">
    <h2>💰 Billing (clientes enterprise)</h2>
    <div id="billing"></div>
  </div>

  <div class="grid" style="margin-top:16px">
    <div class="card">
      <h2>Incidentes recientes (Health Monitor)</h2>
      <div id="incidents" class="log"></div>
    </div>
    <div class="card">
      <h2>Logs <select id="log-svc" onchange="loadLog()"></select></h2>
      <div id="logs" class="log"></div>
    </div>
  </div>
</div>

<script>
// Auth: token persistido en localStorage, enviado como Authorization: Bearer.
const API = '';
let TOKEN = localStorage.getItem('megapanel_token') || '';
function saveTok(){
  TOKEN = document.getElementById('tok').value.trim();
  localStorage.setItem('megapanel_token', TOKEN);
  document.getElementById('tok-status').textContent = TOKEN ? 'guardado ✓' : 'vacio';
  refresh();
}
if(TOKEN) document.getElementById('tok-status').textContent = 'cargado de memoria';
async function get(url){
  const r = await fetch(API + url, {headers: TOKEN ? {'Authorization':'Bearer '+TOKEN} : {}});
  if(r.status === 401){ document.getElementById('tok-status').textContent = 'token invalido/falta'; }
  return await r.json();
}
async function post(url, body){
  const r = await fetch(API + url, {method:'POST',
    headers:{'Content-Type':'application/json', ...(TOKEN ? {'Authorization':'Bearer '+TOKEN} : {})},
    body: body ? JSON.stringify(body) : undefined});
  if(r.status === 401){ document.getElementById('tok-status').textContent = 'token invalido/falta'; }
  return await r.json();
}
function pct(p){ return `<span class="pill ${p<70?'L1':p<90?'L5':'L15'}">${p}%</span>`; }
function gpuTag(g){ if(g<0) return '<span class="tag cpu">CPU</span>'; return `<span class="tag g${g}">GPU ${g}</span>`; }

async function refresh(){
  try {
    const s = await get('/api/status');
    document.getElementById('host').textContent = s.hostname + ' · node=' + (s.node_id||'?') + ' · uptime ' + Math.floor(s.uptime_s/3600) + 'h';
    // gpus
    document.getElementById('gpus').innerHTML = s.gpus.map(g=>`
      <div><b>GPU ${g.index}</b> ${g.name} · ${pct(g.mem_pct)} · ${g.util_pct}% util · ${g.temp_c}°C
      ${g.power_w?'· '+g.power_w+'W':''}</div>
      <div class="gpu-bar"><div style="width:${g.mem_pct}%"></div></div>
      <div style="color:var(--muted);font-size:12px">${g.mem_used_mb}/${g.mem_total_mb} MB · ${g.mem_free_mb} free</div>
    `).join('');
    // system
    document.getElementById('sys').innerHTML = `
      <div>Load: ${s.load['1'].toFixed(1)} / ${s.load['5'].toFixed(1)} / ${s.load['15'].toFixed(1)}</div>
      <div>RAM: ${pct(s.ram.pct)} · ${s.ram.used_mb}/${s.ram.total_mb} MB</div>
      <div>Swap: ${s.swap.used_mb} MB</div>
      <div>Disk: ${pct(s.disk.pct)} · ${s.disk.used}/${s.disk.total}</div>
    `;
    // power (CPU RAPL + GPU total)
    const pw = s.power || {cpu_w:null, gpu_w:0, total_w:0};
    document.getElementById('power').innerHTML = `
      <div>CPU: <b>${pw.cpu_w!=null?pw.cpu_w+' W':'N/A (RAPL no disponible)'}</b></div>
      <div>GPUs: <b>${pw.gpu_w} W</b> (suma de ${s.gpus.length} GPUs)</div>
      <div style="margin-top:6px;font-size:18px">Total: <b>${pw.total_w} W</b></div>
      <div style="color:var(--muted);font-size:11px;margin-top:4px">Service Bus: ${s.service_bus_url||'-'}</div>
    `;
    // services
    document.getElementById('svc-count').textContent = '(' + s.services.length + ')';
    document.getElementById('services').innerHTML = s.services.map(sv=>`
      <tr>
        <td>${sv.name}<div style="color:var(--muted);font-size:11px">${sv.id}</div></td>
        <td>${sv.port||'-'}</td>
        <td>${gpuTag(sv.gpu)}</td>
        <td><span class="tag ${sv.node===(s.node_id||'')?'g0':'g1'}">${sv.node||'?'}</span></td>
        <td><span class="dot ${sv.active?'dot-on':'dot-off'}"></span>${sv.active?'OK':'DOWN'}</td>
        <td style="color:var(--muted);font-size:11px">${sv.enabled||'-'}</td>
        <td>
          <button class="s" onclick="control('${sv.id}','start')">▶</button>
          <button class="r" onclick="control('${sv.id}','stop')">■</button>
          <button onclick="control('${sv.id}','restart')">↻</button>
          <button onclick="loadLog('${sv.id}')">logs</button>
        </td>
      </tr>`).join('');
    // exec node select
    const ns = document.getElementById('exec-node');
    if(ns.options.length === 0){
      ns.add(new Option('local (este nodo)', s.node_id||'local'));
      (s.remote_nodes||[]).forEach(n => ns.add(new Option(n, n)));
    }
    // log svc select
    const sel = document.getElementById('log-svc');
    if(sel.options.length === 0){
      s.services.forEach(sv => sel.add(new Option(sv.name, sv.id)));
    }
    // maintenance
    const m = document.getElementById('maint-badge');
    if(s.maintenance_mode){ m.className='badge badge-err'; m.textContent='MANTENIMIENTO'; }
    else { m.className='badge badge-ok'; m.textContent='NORMAL'; }
    // incidents
    document.getElementById('incidents').textContent = s.incidents.map(i=>`[${i.t}] [${i.level}] ${i.msg}`).join('\n');
  } catch(e) {
    console.error(e);
  }
  // remote nodes
  try {
    const n = await get('/api/nodes');
    document.getElementById('remote-cnt').textContent = '(' + (n.remote?n.remote.length:0) + ')';
    if(n.remote && n.remote.length){
      document.getElementById('remote-nodes').innerHTML = n.remote.map(r=>`
        <div style="padding:6px 0;border-bottom:1px solid var(--muted)">
          <b>${r.name}</b> · <span class="dot ${r.online?'dot-on':'dot-off'}"></span>${r.online?'online':'offline'}
          <div style="color:var(--muted);font-size:11px">${r.agent_url}</div>
          ${r.error?'<div style="color:var(--err);font-size:11px">'+r.error+'</div>':''}
          ${r.online?`<div style="font-size:12px;margin-top:4px">${(r.services||[]).length} servicios · ${(r.gpus||[]).length} GPUs</div>`:''}
        </div>
      `).join('');
    } else {
      document.getElementById('remote-nodes').innerHTML = '<span style="color:var(--muted)">Sin nodos remotos configurados (CINEIA_AGENT_URL vacío en ojoia.env)</span>';
    }
  } catch(e) { document.getElementById('remote-nodes').innerHTML = '<span style="color:var(--muted)">Error consultando nodos</span>'; }
  // billing
  try {
    const b = await get('/api/billing/clients');
    if(b.clients && b.clients.length){
      document.getElementById('billing').innerHTML = '<table style="width:100%"><tr><th>Cliente</th><th>Uso</th><th>Costo</th></tr>' +
        b.clients.map(c=>`<tr><td>${c.client_id}</td><td>${c.usage}</td><td>${c.cost_cents/100} USD</td></tr>`).join('') + '</table>';
    } else {
      document.getElementById('billing').innerHTML = '<span style="color:var(--muted)">Sin clientes registrados</span>';
    }
  } catch(e) { document.getElementById('billing').innerHTML = '<span style="color:var(--muted)">Error billing</span>'; }
  // queues
  try {
    const q = await get('/api/queues');
    let html = `<div><b>${q.count}</b> proyectos</div>`;
    for(const [st, ids] of Object.entries(q.by_status)){
      html += `<div style="margin-top:6px"><span class="tag ${st==='done'?'g0':st==='failed'?'g2':'g1'}">${st} (${ids.length})</span></div>`;
      ids.slice(0,5).forEach(id => html += `<div style="font-size:11px;color:var(--muted);margin-left:14px">${id}</div>`);
    }
    document.getElementById('queues').innerHTML = html;
  } catch(e) { document.getElementById('queues').innerHTML = `<span style="color:var(--muted)">Sin datos</span>`; }
}
async function control(id, action){
  const r = await post(`/api/control/${id}/${action}`);
  setTimeout(refresh, 800);
}
async function loadLog(svc){
  svc = svc || document.getElementById('log-svc').value;
  if(!svc) return;
  const r = await get(`/api/logs/${svc.replace('.service','')}?lines=80`);
  document.getElementById('logs').textContent = r.lines.join('\n');
  document.getElementById('logs').scrollTop = 9999;
}
async function toggleMaint(enable){
  let ext = '';
  if(enable){ ext = prompt('URL de API externa para fallback (dejar vacío si ninguna):') || ''; }
  await post('/api/maintenance', {enable, external_api_url: ext});
  refresh();
}
async function runExec(){
  const node = document.getElementById('exec-node').value;
  const backend = document.getElementById('exec-backend').value;
  const prompt = document.getElementById('exec-prompt').value;
  if(!prompt){ alert('Prompt vacío'); return; }
  document.getElementById('exec-out').textContent = 'Ejecutando...';
  const body = {model: backend, messages: [{role:'user', content: prompt}], max_tokens: 200};
  try {
    const r = await post(`/api/exec/${node}/${backend}`, body);
    let text = '';
    if(r.choices && r.choices[0]) text = r.choices[0].message.content;
    else text = JSON.stringify(r, null, 2);
    document.getElementById('exec-out').textContent = text;
  } catch(e) {
    document.getElementById('exec-out').textContent = 'ERROR: ' + e;
  }
}
refresh();
setInterval(refresh, 5000);
</script>
</body>
</html>
"""

if __name__ == "__main__":
    import uvicorn
    # A2 (defense in depth): bind 127.0.0.1 unicamente. El panel se expone
    # exclusivamente via el tunel Cloudflare -> nginx 19001 -> 127.0.0.1:9001.
    # Antes bind="0.0.0.0" lo dejaba alcanzable en la LAN 10.0.0.44 sin pasar
    # por nginx/CF (bypass de toda proteccion).

    # ── Sincronización multi-nodo (Firestore push + poll control) ──
    _port = int(os.environ.get("MEGAPANEL_PORT", "9001"))
    _local_url = f"http://127.0.0.1:{_port}"

    def _status_provider():
        """Snapshot de estado de ESTE nodo (para push al clúster Firestore)."""
        import httpx as _hx
        try:
            r = _hx.get(f"{_local_url}/api/status", timeout=6,
                        headers={"Authorization": f"Bearer {MEGAPANEL_TOKEN}"})
            if r.status_code == 200:
                return r.json()
            return {"error": f"HTTP {r.status_code}"}
        except Exception as _e:
            return {"error": str(_e)}

    def _control_executor(service_id: str, action: str):
        """Ejecuta un comando de control recibido desde el panel web (Firestore)."""
        import httpx as _hx
        try:
            r = _hx.post(f"{_local_url}/api/control/{service_id}/{action}",
                         timeout=30,
                         headers={"Authorization": f"Bearer {MEGAPANEL_TOKEN}"})
            return r.json() if r.status_code == 200 else {"error": f"HTTP {r.status_code}"}
        except Exception as e:
            return {"error": str(e)}

    def _run_sync_loop(sync, status_provider, control_executor):
        import asyncio as _asyncio
        try:
            async def _loop():
                while True:
                    try:
                        sync.push_status(status_provider())
                    except Exception as _e:
                        print(f"[ojoia_sync] push error: {_e}")
                    try:
                        await sync.poll_control(control_executor)
                    except Exception as _e:
                        print(f"[ojoia_sync] poll error: {_e}")
                    await _asyncio.sleep(sync.interval)
            _asyncio.run(_loop())
        except Exception as _e:
            print(f"[ojoia_sync] loop terminó: {_e}")

    import threading as _th
    try:
        from ojoia_sync import OjoiaSync
        _sync = OjoiaSync(node_id=NODE_ID)
        if _sync.enabled:
            t = _th.Thread(target=_run_sync_loop, args=(_sync, _status_provider, _control_executor), daemon=True)
            t.start()
    except Exception as e:
        print(f"[megapanel] No se pudo iniciar sincronización Firestore: {e}")

    uvicorn.run(app, host="127.0.0.1", port=_port, log_level="info")
