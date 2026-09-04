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
import sys
from pathlib import Path as _Path
sys.path.insert(0, str(_Path(__file__).parent))
from fastapi import FastAPI, HTTPException, Request, Header, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel
import httpx
from billing import BillingStore
from billing_log import (get_requests, get_request_detail, set_rating,
                          get_stats, get_storage_info, purge_old,
                          get_alerts, get_capacity_report)

app = FastAPI(title="OjoIA Server Megapanel", version="1.1")

# ── Auth: Bearer token (defense in depth) ────────────────────────────────
# El panel está expuesto vía túnel Cloudflare (server-admin.ojoia.com.do).
# Antes: cero auth + POST /api/control/{id}/{action} ejecutaba systemctl sin
# validación -> RCE remoto. Ahora se exige Authorization: Bearer <MEGAPANEL_TOKEN>
# en TODOS los /api/* (excepto / que sirve el HTML). El token se carga desde
# env MEGAPANEL_TOKEN o desde /home/sam/.ojoia_megapanel_token (mode 600).
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

# ── Config multi-nodo ──────────────────────────────────────────
NODE_ID = os.environ.get("NODE_ID", "ojoia")
CINEIA_AGENT_URL = os.environ.get("CINEIA_AGENT_URL", "http://10.0.0.44:8300")


def run_cmd(cmd, timeout=8):
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip() or r.stderr.strip()
    except Exception as e:
        return f"ERROR: {e}"


# ── Descubrimiento dinámico de servicios y Docker ──────────────────────────
# En lugar de una lista hardcoded, escaneamos los .service files en
# /etc/systemd/system/ y los contenedores Docker reales para mostrar solo
# los que están instalados en este nodo. Los servicios del nodo remoto se
# descubren via el agent (CINEIA_AGENT_URL).

# Metadata de servicios systemd conocidos (nombre bonito + puerto + gpu)
_KNOWN_SYSTEMD_SERVICES = {
    "tunnel.service":      {"name": "Cloudflare Tunnel", "port": 0, "gpu": -1, "kind": "network"},
    "api-eva.service":     {"name": "OjoIA API Eva", "port": 8005, "gpu": -1, "kind": "api"},
    "chatrd.service":      {"name": "ChatRD API", "port": 8010, "gpu": -1, "kind": "api"},
    "admin_panel.service": {"name": "ChatRD Admin", "port": 8030, "gpu": -1, "kind": "api"},
    "comfyui.service":     {"name": "ComfyUI (Wan)", "port": 8006, "gpu": 2, "kind": "image"},
    "movie_server.service": {"name": "CineIA Movie Server", "port": 8090, "gpu": 2, "kind": "video"},
    "cineia_studio_server.service": {"name": "CineIA Studio API", "port": 8095, "gpu": -1, "kind": "api"},
    "post_server.service": {"name": "Post-Production (RIFE/Lipsync)", "port": 8014, "gpu": 2, "kind": "post"},
    "audio_server.service": {"name": "Audio Server (MusicGen)", "port": 8013, "gpu": 2, "kind": "audio"},
    "f5_tts_server.service": {"name": "F5-TTS", "port": 8017, "gpu": 2, "kind": "audio"},
    "health-monitor.service": {"name": "Health Monitor", "port": 9000, "gpu": -1, "kind": "infra"},
    "megapanel.service":   {"name": "Megapanel (this)", "port": 9001, "gpu": -1, "kind": "infra"},
    "ojoia-bus.service":   {"name": "OjoIA Service Bus (loopback)", "port": 8200, "gpu": -1, "kind": "infra"},
    "ojoia-bus-lan.service": {"name": "OjoIA Service Bus LAN (10.0.0.71:8205, clientes)", "port": 8205, "gpu": -1, "kind": "infra"},
    "ojoia-models.service": {"name": "OjoIA Models (arranque ordenado)", "port": 0, "gpu": -1, "kind": "infra"},
    "redis-ojoia.service": {"name": "Redis (OjoIA)", "port": 6379, "gpu": -1, "kind": "infra"},
    "project-server.service": {"name": "Project Server", "port": 8012, "gpu": -1, "kind": "api"},
    "ui-server.service":   {"name": "UI Server", "port": 8080, "gpu": -1, "kind": "api"},
    "ai-arranque.service": {"name": "AI Arranque (legacy)", "port": 0, "gpu": -1, "kind": "infra"},
}

# Metadata de contenedores Docker conocidos (nombre bonito + puerto + gpu)
# Layout producción (post reorganización GPU):
#   GPU 0: qwen-7b (sglang), qwen3vl8b (visión rápida), whisper-turbo, yolo-pose
#   GPU 1: qwen38-syv (27B kvarn), qwen-9b (manual), qwen-35b-a3b (frío)
_KNOWN_DOCKER_CONTAINERS = {
    "qwen-7b":         {"name": "Qwen VL-7B (SGLang)", "port": 8004, "gpu": 0, "kind": "llm", "id": "qwen7b"},
    "qwen3vl8b":      {"name": "Qwen3-VL-8B (Visión)", "port": 8019, "gpu": 0, "kind": "vision", "id": "qwen3vl8b"},
    "whisper-turbo":  {"name": "Whisper Turbo ASR", "port": 8008, "gpu": 0, "kind": "asr", "id": "whisper"},
    "yolo-pose":      {"name": "YOLO Pose", "port": 8002, "gpu": 0, "kind": "vision", "id": "yolo"},
    "qwen38-syv":     {"name": "Qwen 3.8 27B (vLLM kvarn)", "port": 18020, "gpu": 1, "kind": "llm", "id": "qwen38"},
    "qwen-9b":        {"name": "Qwen VL-9B (vLLM, manual)", "port": 8018, "gpu": 1, "kind": "llm", "id": "qwen9b"},
    "qwen-35b-a3b":   {"name": "Qwen 35B (frío)", "port": 8019, "gpu": 1, "kind": "llm", "id": "qwen36-35b-a3b"},
}


def _discover_systemd_services() -> list:
    """Descubre servicios systemd instalados en /etc/systemd/system/.

    Filtra para mostrar solo servicios relevantes de OjoIA.
    """
    _RELEVANT_KEYWORDS = ("ojoia", "qwen", "eva", "tunnel", "chatrd", "admin_panel",
                          "comfyui", "movie", "cineia", "post", "audio", "f5",
                          "health", "megapanel", "project", "ui-server", "ai-arranque",
                          "redis", "whisper", "yolo", "ojoia-bus")
    services = []
    systemd_dir = Path("/etc/systemd/system")
    if not systemd_dir.exists():
        return services
    for f in systemd_dir.glob("*.service"):
        sid = f.name
        sid_lower = sid.lower()
        if sid in _KNOWN_SYSTEMD_SERVICES:
            meta = _KNOWN_SYSTEMD_SERVICES[sid]
        elif any(kw == sid_lower or sid_lower.startswith(kw + ".") or sid_lower.startswith(kw + "-") or sid_lower.startswith(kw + "_")
                 for kw in _RELEVANT_KEYWORDS):
            meta = {"name": sid.replace(".service", "").replace("-", " ").title(),
                    "port": 0, "gpu": -1, "kind": "service"}
        else:
            continue
        services.append({
            "id": sid, "node": NODE_ID,
            "name": meta.get("name", sid.replace(".service", "").replace("-", " ").title()),
            "port": meta.get("port", 0), "gpu": meta.get("gpu", -1),
            "kind": meta.get("kind", "service"), "level": "system",
        })
    return sorted(services, key=lambda s: s["id"])


def _discover_docker_containers() -> list:
    """Descubre contenedores Docker que existen en este nodo."""
    services = []
    out = run_cmd("docker ps -a --format '{{.Names}}' 2>/dev/null", timeout=10)
    if not out or "ERROR" in out:
        return services
    for line in out.splitlines():
        container = line.strip()
        if not container:
            continue
        meta = _KNOWN_DOCKER_CONTAINERS.get(container, {})
        services.append({
            "id": meta.get("id", container), "node": NODE_ID,
            "name": meta.get("name", container),
            "port": meta.get("port", 0), "gpu": meta.get("gpu", -1),
            "kind": meta.get("kind", "docker"), "level": "docker",
            "container": container, "managed": "docker",
        })
    return sorted(services, key=lambda s: s["id"])


# SERVICIOS: lista combinada de systemd + docker descubiertos dinámicamente.
SERVICES = _discover_systemd_services() + _discover_docker_containers()

# DOCKER_MAP: service_id -> container name (para start/stop via docker)
DOCKER_MAP = {}
for s in SERVICES:
    if s.get("level") == "docker" and "container" in s:
        DOCKER_MAP[s["id"]] = s["container"]
# Mapeo legacy por si el container no se llama igual que el id
_LEGACY_DOCKER_MAP = {
    "qwen.service":    "qwen-7b",
    "qwen9b.service":   "qwen-9b",
    "qwen35b.service":  "qwen-35b-a3b",
    "whisper.service":  "whisper-turbo",
    "yolo-server.service": "yolo-pose",
    "qwen7b":           "qwen-7b",
    "qwen38":           "qwen38-syv",
    "qwen9b":           "qwen-9b",
    "qwen3vl8b":        "qwen3vl8b",
    "whisper":          "whisper-turbo",
    "yolo":             "yolo-pose",
}
for sid, container in _LEGACY_DOCKER_MAP.items():
    DOCKER_MAP.setdefault(sid, container)


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
    return run_cmd(f"{cmd} is-active --quiet {sid}") == ""


def service_enabled(sid: str, level: str) -> str:
    cmd = "systemctl"
    if level == "user":
        cmd += " --user"
    return run_cmd(f"{cmd} is-enabled {sid} 2>/dev/null")


# ── Medición de energía ──────────────────────────────────────────────────────
# CPU: se lee el RAPL del paquete (energía acumulada en µJ) en dos instantes y
# se calcula la potencia instantánea. Requiere lectura de energy_uj (ver regla
# udev: udevadm info; ver /etc/udev/rules.d/99-rapl-power.rules). Si no hay
# acceso (permiso denegado), se reporta CPU en 0 y solo se muestran las GPUs.
_RAPL_PATHS = [
    "/sys/class/powercap/intel-rapl:0/energy_uj",
    "/sys/class/powercap/intel-rapl/intel-rapl:0/energy_uj",
]

def _read_rapl_uj() -> float:
    """Lee la energía acumulada del paquete CPU en microjulios."""
    for p in _RAPL_PATHS:
        try:
            with open(p) as f:
                return float(f.read().strip())
        except Exception:
            continue
    return 0.0

def measure_cpu_power_w(sample_s: float = 1.0) -> float:
    """Calcula el consumo del CPU en Watts usando dos lecturas RAPL."""
    e1 = _read_rapl_uj()
    if e1 <= 0:
        # Fallback: estimar desde carga (rough). Loadavg * factor por core.
        # Valor orientativo: la potencia media del CPU suele escalar con la
        # carga; si no hay RAPL accesible, reportamos estimación conservadora.
        try:
            l1 = os.getloadavg()[0]
            return round(l1 * 12, 2)  # aprox W por unidad de load
        except Exception:
            return 0.0
    time.sleep(sample_s)
    e2 = _read_rapl_uj()
    if e2 <= 0 or e2 <= e1:
        return 0.0
    d_uj = (e2 - e1) % (2 ** 64)  # manejo de wrap-around del contador
    return round(d_uj / 1_000_000 / sample_s, 2)  # uJ/s -> W

# ── Mapeo de servicios del panel → contenedores Docker ───────────────────────
# Construido dinámicamente en DOCKER_MAP arriba. Los modelos se ejecutan como
# contenedores Docker, no como systemd units. El mapeo se usa para que el botón
# start/stop/restart funcione correctamente.

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
    # Estado de contenedores Docker (para modelos) en un solo snapshot
    docker_state = {}
    try:
        d_out = run_cmd("docker ps -a --format '{{.Names}}|{{.State}}' 2>/dev/null", timeout=10)
        for line in d_out.splitlines():
            if "|" in line:
                name, state = line.split("|", 1)
                docker_state[name.strip()] = state.strip()
    except Exception:
        pass
    for s in SERVICES:
        # Si es un servicio Docker, usar el estado real del contenedor
        if s["id"] in DOCKER_MAP:
            container = DOCKER_MAP[s["id"]]
            state = docker_state.get(container, "absent")
            active = state in ("running", "Up", "restarting")
            enabled = state in ("running", "Up", "restarting") or "true"
            svcs.append({
                **s, "active": active, "enabled": state if state != "absent" else "-",
                "docker": True, "container": container, "docker_state": state,
            })
            continue
        active = service_active(s["id"], s["level"])
        enabled = service_enabled(s["id"], s["level"])
        svcs.append({
            **s,
            "active": active,
            "enabled": enabled,
        })

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

    # ── Energía: CPU (RAPL) + GPUs (power.draw) = total ────────────────────
    try:
        import asyncio as _asyncio
        cpu_power = await _asyncio.to_thread(measure_cpu_power_w, 1.0)
    except Exception:
        cpu_power = 0.0
    gpu_power = sum(g.get("power_w") or 0 for g in gpus)
    total_power = round(cpu_power + gpu_power, 2)
    per_gpu = [{"index": g["index"], "name": g["name"], "w": round(g.get("power_w") or 0, 2)} for g in gpus]

    return {
        "timestamp": datetime.now().isoformat(),
        "hostname": platform.node(),
        "uptime_s": uptime_s,
        "load": {"1": load1, "5": load5, "15": load15},
        "ram": {"total_mb": mem_total, "used_mb": mem_used, "free_mb": mem_free, "cache_mb": ram_cache, "pct": round(mem_used / max(mem_total, 1) * 100, 1)},
        "swap": {"used_mb": swap_used},
        "disk": {"total": disk_total, "used": disk_used, "free": disk_free, "pct": disk_pct},
        "power": {
            "cpu_w": cpu_power,
            "gpu_w": round(gpu_power, 2),
            "gpus": per_gpu,
            "total_w": total_power,
        },
        "gpus": gpus,
        "services": svcs,
        "incidents": incidents,
        "maintenance_mode": maintenance,
        "health_monitor_url": HEALTH_MONITOR_URL,
    }


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
    """Controlar servicio: start | stop | restart | enable | disable.

    Para servicios Docker, coordina con el health-monitor via HTTP para:
    - stop: marca `paused=True` (no se auto-reinicia)
    - start: marca `paused=False` y resetea failures
    - restart: marca `paused=False` antes del docker restart
    """
    if action not in ("start", "stop", "restart", "enable", "disable"):
        raise HTTPException(400, "action must be start|stop|restart|enable|disable")
    if not service_id.endswith(".service"):
        service_id += ".service"

    # ── Servicio Docker: mapear y ejecutar docker directamente ──────────────
    if service_id in DOCKER_MAP:
        container = DOCKER_MAP[service_id]
        # Sincronizar el flag paused con el health-monitor
        async with httpx.AsyncClient(timeout=5) as c:
            if action == "stop" or action == "disable":
                try:
                    await c.post(f"{HEALTH_MONITOR_URL}/stop/{service_id}")
                except Exception:
                    pass
                cmd = f"docker stop {container}"
            elif action == "start" or action == "enable":
                try:
                    await c.post(f"{HEALTH_MONITOR_URL}/start/{service_id}")
                except Exception:
                    pass
                cmd = f"docker start {container}"
            elif action == "restart":
                try:
                    await c.post(f"{HEALTH_MONITOR_URL}/start/{service_id}")
                except Exception:
                    pass
                cmd = f"docker restart {container}"
            else:
                raise HTTPException(400, f"action invalida: {action}")
        out = run_cmd(cmd, timeout=60)
        result = out or "ok"
        return {"service": service_id, "action": action,
                "type": "docker", "container": container, "result": result}

    # Para servicios systemd (level=system|user), el endpoint /stop y /start
    # del health-monitor los maneja también.
    if action == "stop":
        async with httpx.AsyncClient(timeout=5) as c:
            try:
                await c.post(f"{HEALTH_MONITOR_URL}/stop/{service_id}")
            except Exception:
                pass
    elif action == "start" or action == "restart":
        async with httpx.AsyncClient(timeout=5) as c:
            try:
                await c.post(f"{HEALTH_MONITOR_URL}/start/{service_id}")
            except Exception:
                pass

    level = "user"
    node = "ojoia"
    for s in SERVICES:
        if s["id"] == service_id:
            level = s["level"]
            node = s.get("node", "ojoia")
            break
    # Si el servicio vive en CINEIA, proxy al agente remoto
    if node == "cineia":
        try:
            headers = {}
            agent_tok = os.environ.get("CINEIA_AGENT_TOKEN", "")
            if agent_tok:
                headers["Authorization"] = f"Bearer {agent_tok}"
            async with httpx.AsyncClient(timeout=30) as c:
                r = await c.post(f"{CINEIA_AGENT_URL}/api/control",
                                 json={"service": service_id, "action": action},
                                 headers=headers)
                return r.json()
        except Exception as e:
            return {"service": service_id, "action": action, "result": f"ERROR agente cineia: {e}"}
    # level=system: el recovery lo hace systemd solo (Restart=on-failure). No intentamos
    # reiniciar desde el panel (requeriria sudo/polkit -> popup molesto). Se informa al
    # operador para que lo haga via SSH si es necesario forzar un reinicio manual.
    if level == "system" and action in ("restart", "start", "stop"):
        return {
            "service": service_id,
            "action": action,
            "result": (
                f"servicio system-level: systemd ya lo reinicia solo (Restart=on-failure). "
                f"Para forzar manualmente: sudo systemctl {action} {service_id}"
            ),
        }
    # level=user se ejecuta como sam sin sudo (systemd --user).
    flag = "--user" if level == "user" else ""
    cmd = f"systemctl {flag} {action} {service_id}"
    out = run_cmd(cmd, timeout=30)
    return {"service": service_id, "action": action, "result": out or "ok"}


@app.get("/api/nodes")
async def nodes():
    """Estado de todos los nodos (ojoia local + cineia remoto)."""
    result = {"ojoia": None, "cineia": None}
    # local
    try:
        result["ojoia"] = {"status": "online", "host": "localhost"}
    except Exception:
        pass
    # remoto cineia
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.get(f"{CINEIA_AGENT_URL}/api/metrics")
            if r.status_code == 200:
                result["cineia"] = r.json()
    except Exception as e:
        result["cineia"] = {"status": "offline", "error": str(e)}
    return result


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


# ─── Billing / API Keys (admin) ──────────────────────────────────────────────

class KeyCmd(BaseModel):
    client_id: str
    label: str = ""
    plan: str = "free"


class RevokeCmd(BaseModel):
    key: str


class PriceCmd(BaseModel):
    model: str
    input_price: float
    output_price: float
    unit: str = "tokens"


class PlanCmd(BaseModel):
    plan: str
    tokens_quota: int
    rpm: int
    name: str = ""


class RatingCmd(BaseModel):
    rating: int  # 1=up, -1=down, 0=neutral


class PlanDeleteCmd(BaseModel):
    plan: str


_billing = None
try:
    _billing = BillingStore.instance()
except Exception as e:
    print(f"[megapanel] billing no disponible: {e}")


@app.get("/api/billing/clients")
async def billing_clients():
    """Lista el uso de todos los clientes (admin)."""
    if not _billing:
        return JSONResponse({"error": "billing no disponible"}, status_code=503)
    out = _billing.get_all_clients_usage("month")
    # Enriquecer con quota
    for c in out:
        cid = c["client_id"]
        # Obtener plan del primer key del cliente
        keys = _billing.list_keys(cid)
        plan = keys[0]["plan"] if keys else "free"
        q = _billing.get_quota_status(cid, plan)
        c["plan"] = plan
        c["quota"] = q
    return {"count": len(out), "clients": out}


@app.post("/admin/keys")
async def admin_create_key(cmd: KeyCmd):
    """Crea una API key para un cliente (admin)."""
    if not _billing:
        return JSONResponse({"error": "billing no disponible"}, status_code=503)
    if cmd.plan not in ("free", "dev", "pro", "enterprise"):
        return JSONResponse({"error": "plan invalido"}, status_code=400)
    r = _billing.create_key(cmd.client_id, cmd.label, cmd.plan)
    return {"key": r["key"], "client_id": r["client_id"], "plan": r["plan"],
            "label": r["label"], "created_at": r["created_at"]}


@app.get("/admin/keys")
async def admin_list_keys():
    """Lista todas las API keys (admin)."""
    if not _billing:
        return JSONResponse({"error": "billing no disponible"}, status_code=503)
    return {"keys": _billing.list_keys()}


@app.post("/admin/keys/revoke")
async def admin_revoke_key(cmd: RevokeCmd):
    """Revoca una API key (admin)."""
    if not _billing:
        return JSONResponse({"error": "billing no disponible"}, status_code=503)
    ok = _billing.revoke_key(cmd.key)
    return {"revoked": ok}


# ─── Billing config: precios y planes editables en caliente ─────────────────

@app.get("/api/billing/config")
async def billing_config():
    """Retorna precios y planes actuales (live)."""
    if not _billing:
        return JSONResponse({"error": "billing no disponible"}, status_code=503)
    return _billing.get_config()


@app.put("/api/billing/prices")
async def billing_update_price(cmd: PriceCmd):
    """Actualiza el precio de un modelo (live)."""
    if not _billing:
        return JSONResponse({"error": "billing no disponible"}, status_code=503)
    return {"updated": _billing.update_price(
        cmd.model, cmd.input_price, cmd.output_price, cmd.unit)}


@app.put("/api/billing/plans")
async def billing_update_plan(cmd: PlanCmd):
    """Actualiza o crea un plan (live)."""
    if not _billing:
        return JSONResponse({"error": "billing no disponible"}, status_code=503)
    return {"updated": _billing.update_plan(
        cmd.plan, cmd.tokens_quota, cmd.rpm, cmd.name)}


@app.post("/api/billing/plans/delete")
async def billing_delete_plan(cmd: PlanDeleteCmd):
    """Elimina un plan (si no tiene keys asignadas)."""
    if not _billing:
        return JSONResponse({"error": "billing no disponible"}, status_code=503)
    ok = _billing.delete_plan(cmd.plan)
    if not ok:
        return JSONResponse({"error": "no se pudo eliminar (tiene keys o no existe)"},
                            status_code=400)
    return {"deleted": True}


# ─── Request log (SQLite) ───────────────────────────────────────────────────

@app.get("/api/billing/log")
async def billing_log(limit: int = 50, offset: int = 0,
                      client_id: str = "", model: str = "",
                      only_errors: bool = False, min_cost: float = 0.0):
    """Lista requests del log con filtros."""
    return {"requests": get_requests(
        limit=limit, offset=offset, client_id=client_id, model=model,
        only_errors=only_errors, min_cost=min_cost)}


@app.get("/api/billing/log/{request_id}")
async def billing_log_detail(request_id: int):
    """Detalle de un request (prompt + response completos)."""
    d = get_request_detail(request_id)
    if not d:
        return JSONResponse({"error": "request no encontrado"}, status_code=404)
    return d


@app.put("/api/billing/log/{request_id}/rating")
async def billing_log_rating(request_id: int, cmd: RatingCmd):
    """Setea el rating de un request (1=up, -1=down, 0=neutral)."""
    ok = set_rating(request_id, cmd.rating)
    return {"updated": ok}


# ─── Stats y storage ────────────────────────────────────────────────────────

@app.get("/api/billing/stats")
async def billing_stats(hours: int = 24):
    """Estadisticas agregadas para el dashboard."""
    return get_stats(hours)


@app.get("/api/billing/alerts")
async def billing_alerts():
    """Alertas activas: abuso de rate (>20 req/min), costo (>$5/h) y
    modelos con respuestas vacías. Vacío = todo normal."""
    return {"alerts": get_alerts()}


@app.get("/api/billing/capacity")
async def billing_capacity():
    """Capacidad de tokens/día del sistema, medida en producción."""
    return get_capacity_report()


@app.get("/api/billing/storage")
async def billing_storage():
    """Info de almacenamiento del log SQLite."""
    return get_storage_info()


@app.post("/api/billing/purge")
async def billing_purge():
    """Purge manual del log (registros >retention_days)."""
    n = purge_old()
    return {"purged": n, "storage": get_storage_info()}

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
/* Header compacto */
header { padding: 10px 20px; border-bottom: 1px solid var(--border); display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 8px; }
header h1 { margin: 0; font-size: 16px; }
.header-auth { display: flex; align-items: center; gap: 8px; font-size: 12px; }
.badge { padding: 3px 8px; border-radius: 12px; font-size: 11px; cursor: pointer; }
.badge-ok { background: rgba(63,185,80,.2); color: var(--green); }
.badge-err { background: rgba(248,81,73,.2); color: var(--red); }
/* Nav top-level */
.main-nav { display: flex; gap: 0; border-bottom: 1px solid var(--border); padding: 0 20px; overflow-x: auto; }
.nav-btn { background: transparent; border: 0; border-bottom: 2px solid transparent; color: var(--muted); padding: 12px 16px; cursor: pointer; font-size: 13px; white-space: nowrap; }
.nav-btn:hover { color: var(--text); background: rgba(255,255,255,.03); }
.nav-btn.active { color: var(--blue); border-bottom-color: var(--blue); font-weight: 600; }
.nav-btn.err-dot::after { content:''; display:inline-block; width:7px; height:7px; background:var(--red); border-radius:50%; margin-left:6px; vertical-align:middle; }
/* Contenido */
.page { padding: 20px; max-width: 1500px; margin: 0 auto; display: none; }
.page.active { display: block; }
.grid { display: grid; gap: 16px; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); }
.grid2 { display: grid; gap: 12px; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); }
.card { background: var(--card); border: 1px solid var(--border); border-radius: 8px; padding: 16px; }
.card h2 { margin: 0 0 12px; font-size: 14px; color: var(--muted); text-transform: uppercase; letter-spacing: .5px; }
.card h3 { margin: 0 0 8px; font-size: 12px; color: var(--muted); }
.gpu-bar { background: var(--border); height: 8px; border-radius: 4px; margin: 4px 0 12px; overflow: hidden; }
.gpu-bar > div { height: 100%; background: linear-gradient(90deg, var(--green), var(--yellow), var(--red)); }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
tr { border-bottom: 1px solid var(--border); }
td, th { padding: 6px 8px; text-align: left; }
th { color: var(--muted); font-weight: normal; font-size: 11px; text-transform: uppercase; }
.dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 6px; }
.dot-on { background: var(--green); }
.dot-off { background: var(--red); }
.dot-paused { background: var(--yellow); animation: pulse 1.5s infinite; }
@keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: 0.3; } }
.tag { padding: 2px 6px; border-radius: 3px; background: rgba(88,166,255,.15); color: var(--blue); font-size: 11px; margin-right: 4px; }
.tag.g0 { background: rgba(188,140,255,.15); color: var(--purple); }
.tag.g1 { background: rgba(63,185,80,.15); color: var(--green); }
.tag.g2 { background: rgba(210,153,34,.15); color: var(--yellow); }
.tag.cpu { background: rgba(139,148,158,.15); color: var(--muted); }
button { background: var(--border); color: var(--text); border: 0; padding: 5px 11px; border-radius: 4px; cursor: pointer; font-size: 12px; margin: 2px; }
button:hover { background: #444c56; }
button.r { color: var(--red); }
button.s { color: var(--green); }
.maint { border: 1px solid var(--yellow); color: var(--yellow); }
.mono { font-family: monospace; }
.log { background: #010409; border: 1px solid var(--border); border-radius: 6px; padding: 12px; font-family: monospace; font-size: 11px; max-height: 300px; overflow: auto; white-space: pre; }
input { background: var(--border); color: var(--text); border:0; padding:6px; border-radius:4px; }
select { background: var(--border); color: var(--text); border:0; padding:6px; border-radius:4px; }
.pill { padding: 1px 6px; border-radius: 3px; font-size: 11px; }
.pill.L1 { background: rgba(63,185,80,.15); color: var(--green); }
.pill.L5 { background: rgba(210,153,34,.15); color: var(--yellow); }
.pill.L15{ background: rgba(248,81,73,.15); color: var(--red); }
.usage-bar { background: var(--border); height: 6px; border-radius: 3px; margin: 4px 0 8px; overflow: hidden; }
.usage-bar > div { height: 100%; background: linear-gradient(90deg, var(--blue), var(--purple)); }
.key-row { display: flex; justify-content: space-between; align-items: center; padding: 6px 0; border-bottom: 1px solid var(--border); font-size: 12px; }
.copy-btn { font-size: 10px; padding: 2px 6px; }
.kpi { font-size: 26px; font-weight: 700; color: var(--blue); }
.kpi-grid { display:grid; gap:12px; grid-template-columns:repeat(auto-fit,minmax(130px,1fr)); margin-bottom:16px; }
.kpi-card { background:var(--card); border:1px solid var(--border); border-radius:8px; padding:12px; text-align:center; }
.kpi-label { font-size:11px; color:var(--muted); margin-top:2px; }
.row-err { color: var(--red); }
.rating-up { color: var(--green); } .rating-down { color: var(--red); } .rating-none { color: var(--muted); }
.price-row { display:flex; gap:6px; align-items:center; padding:6px 0; border-bottom:1px solid var(--border); font-size:12px; flex-wrap:wrap; }
.price-row input { width: 70px; }
canvas { max-width: 100%; }
.toolbar { display:flex; gap:8px; flex-wrap:wrap; align-items:center; margin-bottom:12px; }
</style>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
</head>
<body>
<header>
  <h1>🖥️ OjoIA Control — <span id="host" style="color:var(--muted);font-size:13px"></span></h1>
  <div class="header-auth">
    <input id="tok" type="password" placeholder="Bearer token..." style="width:200px;font-size:12px">
    <button onclick="saveTok()">Guardar</button>
    <span id="tok-status" style="color:var(--muted);font-size:11px"></span>
    <span id="maint-badge" class="badge badge-ok">NORMAL</span>
    <button class="maint" onclick="toggleMaint(true)" style="font-size:11px">🔧 Mant.</button>
    <button onclick="toggleMaint(false)" style="font-size:11px">▶ Salir</button>
    <button onclick="refreshAll()">⟳</button>
  </div>
</header>

<nav class="main-nav">
  <button class="nav-btn active" onclick="goPage('overview')">📊 Overview</button>
  <button class="nav-btn" onclick="goPage('infra')">🏗 Infraestructura</button>
  <button class="nav-btn" onclick="goPage('billing')">💳 Billing</button>
  <button class="nav-btn" onclick="goPage('clients')">👥 Clientes</button>
  <button class="nav-btn" onclick="goPage('reqlog')">📋 Request Log</button>
  <button class="nav-btn" onclick="goPage('prices')">⚙ Precios/Planes</button>
</nav>

<!-- ═══ Overview: resumen rápido de todo ═══ -->
<div id="page-overview" class="page active">
  <div class="kpi-grid">
    <div class="kpi-card"><div class="kpi" id="ov-reqs">—</div><div class="kpi-label">Requests 24h</div></div>
    <div class="kpi-card"><div class="kpi" id="ov-tokens">—</div><div class="kpi-label">Tokens 24h</div></div>
    <div class="kpi-card"><div class="kpi" id="ov-cost">—</div><div class="kpi-label">Costo 24h</div></div>
    <div class="kpi-card"><div class="kpi" id="ov-errs">—</div><div class="kpi-label">Errores 24h</div></div>
    <div class="kpi-card"><div class="kpi" id="ov-svcs">—</div><div class="kpi-label">Servicios OK</div></div>
    <div class="kpi-card"><div class="kpi" id="ov-storage">—</div><div class="kpi-label">DB Log size</div></div>
  </div>
  <div class="grid">
    <div class="card"><h2>GPUs</h2><div id="ov-gpus"></div></div>
    <div class="card"><h2>⚡ Energía</h2><div id="ov-power"></div></div>
    <div class="card"><h2>Servicios críticos</h2><div id="ov-svcs-list" style="font-size:12px;line-height:1.8"></div></div>
    <div class="card"><h2>Tokens por modelo (24h)</h2><div id="ov-models" style="font-size:12px;line-height:1.8"></div></div>
  </div>
</div>

<!-- ═══ Infraestructura ═══ -->
<div id="page-infra" class="page">
  <div class="grid">
    <div class="card"><h2>⚡ Energía</h2><div id="power"></div></div>
    <div class="card"><h2>GPUs</h2><div id="gpus"></div></div>
    <div class="card"><h2>Sistema</h2><div id="sys"></div></div>
    <div class="card"><h2>Colas CineIA</h2><div id="queues"></div></div>
  </div>
  <div class="card" style="margin-top:16px">
    <h2>Servicios <span id="svc-count"></span></h2>
    <table><thead><tr><th>Servicio</th><th>Puerto</th><th>GPU</th><th>Estado</th><th>Enabled</th><th></th></tr></thead>
    <tbody id="services"></tbody></table>
  </div>
  <div class="grid" style="margin-top:16px">
    <div class="card"><h2>Incidentes (Health Monitor)</h2><div id="incidents" class="log"></div></div>
    <div class="card"><h2>Logs <select id="log-svc" onchange="loadSvcLog()"></select></h2><div id="svc-logs" class="log"></div></div>
  </div>
</div>

<!-- ═══ Billing Dashboard ═══ -->
<div id="page-billing" class="page">
  <div class="toolbar">
    <button onclick="loadDashboard()">⟳ Actualizar</button>
    <select id="dash-hours" onchange="loadDashboard()">
      <option value="1">1h</option><option value="6">6h</option>
      <option value="24" selected>24h</option><option value="168">7d</option><option value="720">30d</option>
    </select>
  </div>
  <div class="kpi-grid">
    <div class="kpi-card"><div class="kpi" id="kpi-reqs">0</div><div class="kpi-label">Requests</div></div>
    <div class="kpi-card"><div class="kpi" id="kpi-tokens">0</div><div class="kpi-label">Tokens</div></div>
    <div class="kpi-card"><div class="kpi" id="kpi-cost">$0</div><div class="kpi-label">Costo</div></div>
    <div class="kpi-card"><div class="kpi" id="kpi-lat">0ms</div><div class="kpi-label">Lat avg</div></div>
    <div class="kpi-card"><div class="kpi" id="kpi-err">0</div><div class="kpi-label">Errores</div></div>
    <div class="kpi-card"><div class="kpi" id="kpi-rating">—</div><div class="kpi-label">👍/👎</div></div>
  </div>
  <div class="grid2">
    <div class="card"><h3>Uso por hora (tokens)</h3><canvas id="chart-hours" height="120"></canvas></div>
    <div class="card"><h3>Tokens por modelo</h3><div id="by-model"></div></div>
    <div class="card"><h3>Tokens por cliente</h3><div id="by-client"></div></div>
  </div>
  <div class="card" style="margin-top:12px">
    <h3>Almacenamiento del log</h3>
    <div id="storage-info" style="font-size:12px;color:var(--muted)"></div>
    <button class="r" style="margin-top:8px" onclick="purgeLog()">🗑 Purge manual (>30 dias)</button>
  </div>
</div>

<!-- ═══ Clientes + Keys ═══ -->
<div id="page-clients" class="page">
  <div class="toolbar">
    <button onclick="loadBilling()">⟳ Actualizar uso</button>
    <button onclick="showCreateKey()">+ Crear API Key</button>
  </div>
  <div id="key-create" style="display:none; margin-bottom:12px; padding:10px; border:1px solid var(--border); border-radius:6px">
    <input id="kc-client" placeholder="client_id (ej: acme_corp)" style="width:30%">
    <input id="kc-label" placeholder="label (ej: produccion)" style="width:25%">
    <select id="kc-plan"></select>
    <button class="s" onclick="createKey()">Crear</button>
    <button onclick="document.getElementById('key-create').style.display='none'">Cancelar</button>
    <div id="kc-result" style="margin-top:8px;font-size:12px;color:var(--green)"></div>
  </div>
  <div class="grid2">
    <div><h3 style="color:var(--muted);font-size:12px;margin:0 0 8px">Clientes (uso mensual)</h3><div id="billing-clients"></div></div>
    <div><h3 style="color:var(--muted);font-size:12px;margin:0 0 8px">API Keys</h3><div id="billing-keys"></div></div>
  </div>
</div>

<!-- ═══ Request Log ═══ -->
<div id="page-reqlog" class="page">
  <div class="toolbar">
    <input id="log-filter-client" placeholder="cliente" style="width:15%">
    <input id="log-filter-model" placeholder="modelo" style="width:15%">
    <label style="font-size:13px;display:flex;align-items:center;gap:4px"><input type="checkbox" id="log-filter-errors"> Solo errores</label>
    <button onclick="loadReqLog(0)">Buscar</button>
    <button onclick="loadReqLog(offset-50)">← Anterior</button>
    <button onclick="loadReqLog(offset+50)">Siguiente →</button>
    <span id="log-page" style="font-size:12px;color:var(--muted)"></span>
  </div>
  <table id="log-table">
    <thead><tr><th>Hora</th><th>Cliente</th><th>Modelo</th><th>Tokens</th><th>Costo</th><th>Lat</th><th>Status</th><th>Rating</th><th></th></tr></thead>
    <tbody id="log-body"></tbody>
  </table>
</div>

<!-- ═══ Precios y Planes ═══ -->
<div id="page-prices" class="page">
  <div class="toolbar"><button onclick="loadConfig()">⟳ Recargar</button></div>
  <div class="grid2">
    <div><h3 style="font-size:12px;color:var(--muted);margin:0 0 8px">Precios por modelo (por 1M tokens)</h3><div id="prices-editor"></div></div>
    <div>
      <h3 style="font-size:12px;color:var(--muted);margin:0 0 8px">Planes</h3>
      <div id="plans-editor"></div>
      <div style="margin-top:12px;padding:10px;border:1px solid var(--border);border-radius:6px">
        <h3 style="font-size:11px;color:var(--muted)">Nuevo plan</h3>
        <input id="np-name" placeholder="plan (ej: trial)" style="width:20%">
        <input id="np-display" placeholder="display" style="width:20%">
        <input id="np-quota" type="number" placeholder="quota tokens" style="width:20%">
        <input id="np-rpm" type="number" placeholder="rpm" style="width:10%">
        <button class="s" onclick="createPlan()">+ Crear plan</button>
      </div>
    </div>
  </div>
</div>

<!-- ═══ Modal de detalle de request ═══ -->
<div id="req-modal" style="display:none;position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,.7);z-index:100;padding:40px">
  <div class="card" style="max-width:800px;margin:0 auto;max-height:80vh;overflow:auto">
    <div style="display:flex;justify-content:space-between;margin-bottom:12px">
      <h2 style="margin:0">Request #<span id="md-id"></span></h2>
      <div>
        <button class="s" onclick="rateReq(1)">👍 Up</button>
        <button class="r" onclick="rateReq(-1)">👎 Down</button>
        <button onclick="document.getElementById('req-modal').style.display='none'">✕ Cerrar</button>
      </div>
    </div>
    <div id="md-meta" style="font-size:12px;color:var(--muted);margin-bottom:12px"></div>
    <h3 style="font-size:12px;color:var(--muted)">Prompt</h3>
    <div id="md-prompt" class="log" style="white-space:pre-wrap;margin-bottom:12px"></div>
    <h3 style="font-size:12px;color:var(--muted)">Response</h3>
    <div id="md-response" class="log" style="white-space:pre-wrap"></div>
  </div>
</div>

<script>
const API = '';
let TOKEN = localStorage.getItem('megapanel_token') || '';
let _currentStatus = null;
function saveTok(){
  TOKEN = document.getElementById('tok').value.trim();
  localStorage.setItem('megapanel_token', TOKEN);
  document.getElementById('tok-status').textContent = TOKEN ? 'guardado ✓' : 'vacio';
  refreshAll();
}
if(TOKEN) document.getElementById('tok-status').textContent = 'cargado ✓';
async function get(url){
  const r = await fetch(API + url, {headers: TOKEN ? {'Authorization':'Bearer '+TOKEN} : {}});
  if(r.status === 401){ document.getElementById('tok-status').textContent = '⚠ token invalido/falta'; }
  return await r.json();
}
async function post(url, body){
  const r = await fetch(API + url, {method:'POST',
    headers:{'Content-Type':'application/json', ...(TOKEN ? {'Authorization':'Bearer '+TOKEN} : {})},
    body: body ? JSON.stringify(body) : undefined});
  if(r.status === 401){ document.getElementById('tok-status').textContent = '⚠ token invalido/falta'; }
  return await r.json();
}
async function putJSON(url, body){
  const r = await fetch(API + url, {method:'PUT',
    headers:{'Content-Type':'application/json', ...(TOKEN ? {'Authorization':'Bearer '+TOKEN} : {})},
    body: body ? JSON.stringify(body) : undefined});
  if(r.status === 401){ document.getElementById('tok-status').textContent = '⚠ token invalido/falta'; }
  return {ok:r.ok, data: await r.json()};
}
function pct(p){ return `<span class="pill ${p<70?'L1':p<90?'L5':'L15'}">${p}%</span>`; }
function gpuTag(g){ if(g<0) return '<span class="tag cpu">CPU</span>'; return `<span class="tag g${g}">GPU ${g}</span>`; }

// ── Navegación top-level ───────────────────────────────────────────────────
function goPage(name){
  document.querySelectorAll('.nav-btn').forEach(b=>b.classList.remove('active'));
  event && event.target && event.target.classList.add('active');
  document.querySelectorAll('.page').forEach(p=>p.classList.remove('active'));
  document.getElementById('page-'+name).classList.add('active');
  if(name==='overview') loadOverview();
  if(name==='billing') loadDashboard();
  if(name==='clients') loadBilling();
  if(name==='reqlog') loadReqLog(0);
  if(name==='prices') loadConfig();
}
function refreshAll(){ refresh(); loadOverview(); }

// ── Infraestructura: refresh ────────────────────────────────────────────────
async function refresh(){
  try {
    const s = await get('/api/status');
    _currentStatus = s;
    document.getElementById('host').textContent = s.hostname + ' · uptime ' + Math.floor(s.uptime_s/3600) + 'h';
    // gpus (infra + overview comparten elemento gpus)
    const gpuHtml = s.gpus.map(g=>`
      <div><b>GPU ${g.index}</b> ${g.name} · ${pct(g.mem_pct)} · ${g.util_pct}% util · ${g.temp_c}°C
      ${g.power_w?'· '+g.power_w+'W':''}</div>
      <div class="gpu-bar"><div style="width:${g.mem_pct}%"></div></div>
      <div style="color:var(--muted);font-size:12px">${g.mem_used_mb}/${g.mem_total_mb} MB · ${g.mem_free_mb} free</div>
    `).join('');
    document.getElementById('gpus').innerHTML = gpuHtml;
    if(document.getElementById('ov-gpus')) document.getElementById('ov-gpus').innerHTML = gpuHtml;
    // ⚡ Energía
    if(s.power){
      const pw = s.power;
      const rows = pw.gpus.map(g=>`
        <div style="display:flex;justify-content:space-between;padding:4px 0;border-bottom:1px solid var(--border)">
          <span>GPU ${g.index} <span style="color:var(--muted);font-size:11px">${g.name}</span></span>
          <b>${g.w} W</b>
        </div>`).join('');
      const powerHtml = `
        <div style="font-size:28px;font-weight:700;line-height:1.2">
          ⚡ <span style="color:var(--yellow)">${pw.total_w}</span> W
        </div>
        <div style="color:var(--muted);font-size:12px;margin:4px 0 8px">Consumo total del sistema</div>
        <div style="display:flex;justify-content:space-between;padding:3px 0">
          <span>🖥 CPU (RAPL)</span><b>${pw.cpu_w} W</b>
        </div>
        <div style="display:flex;justify-content:space-between;padding:3px 0">
          <span>🎮 Total GPUs (${pw.gpus.length})</span><b>${pw.gpu_w} W</b>
        </div>
        <div style="margin-top:6px;border-top:1px solid var(--border);padding-top:6px;font-size:12px;color:var(--muted);font-weight:600">Desglose por GPU</div>
        ${rows}
      `;
      document.getElementById('power').innerHTML = powerHtml;
      if(document.getElementById('ov-power')) document.getElementById('ov-power').innerHTML = `
        <div style="font-size:24px;font-weight:700">⚡ <span style="color:var(--yellow)">${pw.total_w}</span> W</div>
        <div style="color:var(--muted);font-size:12px">CPU ${pw.cpu_w}W · GPUs ${pw.gpu_w}W</div>
      `;
    }
    // system
    document.getElementById('sys').innerHTML = `
      <div>Load: ${s.load['1'].toFixed(1)} / ${s.load['5'].toFixed(1)} / ${s.load['15'].toFixed(1)}</div>
      <div>RAM: ${pct(s.ram.pct)} · ${s.ram.used_mb}/${s.ram.total_mb} MB</div>
      <div>Swap: ${s.swap.used_mb} MB</div>
      <div>Disk: ${pct(s.disk.pct)} · ${s.disk.used}/${s.disk.total}</div>
    `;
    // services
    document.getElementById('svc-count').textContent = '(' + s.services.length + ')';
    document.getElementById('services').innerHTML = s.services.map(sv=>{
      const isPaused = sv.paused;
      const state = isPaused ? 'PAUSADO' : (sv.active ? 'OK' : (sv.docker ? (sv.docker_state || 'DOWN') : 'DOWN'));
      const dotClass = isPaused ? 'dot-paused' : (sv.active ? 'dot-on' : 'dot-off');
      return `<tr ${isPaused?'style="opacity:0.6"':''}>
        <td>${sv.name}${isPaused?' <span class="pill L5" style="font-size:9px">⏸ paused</span>':''}<div style="color:var(--muted);font-size:11px">${sv.id}</div></td>
        <td>${sv.port||'-'}</td>
        <td>${gpuTag(sv.gpu)}</td>
        <td><span class="dot ${dotClass}"></span>${state}</td>
        <td style="color:var(--muted);font-size:11px">${sv.enabled||'-'}</td>
        <td>
          <button class="s" onclick="control('${sv.id}','start')" title="Iniciar${isPaused?' (resume)':''}">▶</button>
          <button class="r" onclick="control('${sv.id}','stop')" title="${isPaused?'Ya pausado':'Parar y pausar'}">■</button>
          <button onclick="control('${sv.id}','restart')" title="${isPaused?'Inicia primero':'Reiniciar'}">↻</button>
          <button onclick="loadSvcLog('${sv.id}')">logs</button>
        </td>
      </tr>`;
    }).join('');
    // overview: servicios criticos resumidos
    if(document.getElementById('ov-svcs-list')){
      const crit = s.services.filter(sv=>sv.id.includes('qwen')||sv.id.includes('tunnel')||sv.id.includes('eva'));
      document.getElementById('ov-svcs').textContent = s.services.filter(x=>x.active).length + '/' + s.services.length;
      document.getElementById('ov-svcs-list').innerHTML = crit.map(sv=>
        `<div><span class="dot ${sv.active?'dot-on':'dot-off'}"></span>${sv.name}</div>`).join('');
    }
    // log svc select
    const sel = document.getElementById('log-svc');
    if(sel.options.length === 0){ s.services.forEach(sv => sel.add(new Option(sv.name, sv.id))); }
    // maintenance
    const m = document.getElementById('maint-badge');
    if(s.maintenance_mode){ m.className='badge badge-err'; m.textContent='MANTENIMIENTO'; }
    else { m.className='badge badge-ok'; m.textContent='NORMAL'; }
    // incidents
    document.getElementById('incidents').textContent = s.incidents.map(i=>`[${i.t}] [${i.level}] ${i.msg}`).join('\\n');
  } catch(e) { console.error(e); }
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
async function control(id, action){ await post(`/api/control/${id}/${action}`); setTimeout(refresh, 800); }
async function loadSvcLog(svc){
  svc = svc || document.getElementById('log-svc').value;
  if(!svc) return;
  const r = await get(`/api/logs/${svc.replace('.service','')}?lines=80`);
  document.getElementById('svc-logs').textContent = r.lines.join('\\n');
  document.getElementById('svc-logs').scrollTop = 9999;
}
async function toggleMaint(enable){
  if(enable){ ext = prompt('URL de API externa para fallback (dejar vacío si ninguna):') || ''; }
  await post('/api/maintenance', {enable, external_api_url: ext||''});
  refresh();
}

// ── Overview: KPIs rápidos ──────────────────────────────────────────────────
async function loadOverview(){
  try {
    const s = await get('/api/billing/stats?hours=24');
    document.getElementById('ov-reqs').textContent = (s.total_requests||0).toLocaleString();
    document.getElementById('ov-tokens').textContent = (s.total_tokens||0).toLocaleString();
    document.getElementById('ov-cost').textContent = '$'+(s.total_cost||0).toFixed(4);
    document.getElementById('ov-errs').textContent = (s.errors||0);
    const byM = Object.entries(s.by_model||{}).map(([m,v])=>
      `<div><span class="tag g1">${m}</span> <b>${v.tokens.toLocaleString()}</b> tok · ${v.requests} req</div>`).join('');
    if(document.getElementById('ov-models')) document.getElementById('ov-models').innerHTML = byM || '<span style="color:var(--muted)">sin datos</span>';
  } catch(e) { console.error('overview billing',e); }
  try {
    const st = await get('/api/billing/storage');
    if(document.getElementById('ov-storage')) document.getElementById('ov-storage').textContent = st.db_size_mb + 'MB';
    if(document.getElementById('storage-info')) document.getElementById('storage-info').innerHTML =
      `DB: <b>${st.db_size_mb} MB</b> · ${st.total_records.toLocaleString()} registros · Free: <b>${st.disk_free_mb.toLocaleString()} MB</b> · Retención: <b>${st.retention_days} dias</b><br><span style="font-size:11px">${st.db_path}</span>`;
  } catch(e) {}
}

// ── Dashboard billing completo ─────────────────────────────────────────────
let _chart = null;
async function loadDashboard(){
  const h = parseInt(document.getElementById('dash-hours').value);
  try {
    const s = await get('/api/billing/stats?hours='+h);
    document.getElementById('kpi-reqs').textContent = (s.total_requests||0).toLocaleString();
    document.getElementById('kpi-tokens').textContent = (s.total_tokens||0).toLocaleString();
    document.getElementById('kpi-cost').textContent = '$'+(s.total_cost||0).toFixed(4);
    document.getElementById('kpi-lat').textContent = (s.avg_latency_ms||0)+'ms';
    document.getElementById('kpi-err').textContent = (s.errors||0);
    document.getElementById('kpi-rating').textContent = `${s.up_votes||0}/${s.down_votes||0}`;
    const byM = Object.entries(s.by_model||{}).map(([m,v])=>
      `<div style="display:flex;justify-content:space-between;font-size:12px;padding:3px 0"><span><span class="tag g1">${m}</span></span><span><b>${v.tokens.toLocaleString()}</b> tok · $${v.cost.toFixed(4)} · ${v.requests} req</span></div>`).join('');
    document.getElementById('by-model').innerHTML = byM || '<span style="color:var(--muted)">sin datos</span>';
    const byC = Object.entries(s.by_client||{}).map(([c,v])=>
      `<div style="display:flex;justify-content:space-between;font-size:12px;padding:3px 0"><span><b>${c}</b></span><span><b>${v.tokens.toLocaleString()}</b> tok · $${v.cost.toFixed(4)} · ${v.requests} req</span></div>`).join('');
    document.getElementById('by-client').innerHTML = byC || '<span style="color:var(--muted)">sin datos</span>';
    const labels = (s.hourly||[]).map(h=>new Date(h.ts*1000).toLocaleTimeString('es',{hour:'2-digit',minute:'2-digit'}));
    const tokens = (s.hourly||[]).map(h=>h.tokens);
    if(_chart) _chart.destroy();
    const ctx = document.getElementById('chart-hours');
    if(ctx && labels.length){
      _chart = new Chart(ctx, {type:'line', data:{labels,datasets:[{label:'Tokens',data:tokens,borderColor:'#58a6ff',backgroundColor:'rgba(88,166,255,.1)',fill:true}]},
        options:{responsive:true,plugins:{legend:{display:false}},scales:{x:{ticks:{color:'#8b949e',maxTicksLimit:8}},y:{ticks:{color:'#8b949e'}}}}});
    }
  } catch(e) { console.error('dashboard',e); }
}
async function purgeLog(){
  if(!confirm('Purgar registros >30 dias?')) return;
  const r = await post('/api/billing/purge', {});
  alert('Purgados: '+r.purged);
  loadDashboard();
}

// ── Clientes + Keys ─────────────────────────────────────────────────────────
function showCreateKey(){
  const el = document.getElementById('key-create');
  el.style.display = el.style.display === 'none' ? 'block' : 'none';
  document.getElementById('kc-result').textContent = '';
}
async function createKey(){
  const client = document.getElementById('kc-client').value.trim();
  const label = document.getElementById('kc-label').value.trim();
  const plan = document.getElementById('kc-plan').value;
  if(!client){ alert('client_id requerido'); return; }
  const r = await post('/admin/keys', {client_id: client, label, plan});
  if(r.key){
    document.getElementById('kc-result').innerHTML = `Key creada: <code style="color:var(--green)">${r.key}</code> <button class="copy-btn" onclick="navigator.clipboard.writeText('${r.key}')">copiar</button>`;
    loadBilling();
  } else { document.getElementById('kc-result').innerHTML = `<span style="color:var(--red)">Error: ${JSON.stringify(r)}</span>`; }
}
async function revokeKey(key){
  if(!confirm('Revocar key ' + key.slice(0,20) + '...?')) return;
  await post('/admin/keys/revoke', {key});
  loadBilling();
}
async function loadBilling(){
  try { const cfg = await get('/api/billing/config');
    const sel = document.getElementById('kc-plan');
    if(sel && cfg.plans) sel.innerHTML = Object.entries(cfg.plans).map(([k,v])=>`<option value="${k}">${k} (${(v.tokens_quota/1e6).toFixed(0)}M tok/mes)</option>`).join('');
  } catch(e) {}
  try {
    const c = await get('/api/billing/clients');
    const html = (c.clients || []).map(cl => {
      const q = cl.quota || {}; const pctUsed = q.pct_used || 0;
      const models = Object.entries(cl.usage?.by_model || {}).map(([m,t]) => `<span class="tag g1">${m}: ${t.tokens} tok</span>`).join(' ');
      return `<div style="padding:8px 0;border-bottom:1px solid var(--border)">
        <div><b>${cl.client_id}</b> <span class="tag cpu">${cl.plan}</span></div>
        <div style="font-size:12px;color:var(--muted)">${cl.tokens.toLocaleString()} tokens · $${cl.cost_usd.toFixed(4)} · ${cl.requests} reqs</div>
        <div class="usage-bar"><div style="width:${Math.min(100,pctUsed)}%"></div></div>
        <div style="font-size:11px;color:var(--muted)">${pctUsed.toFixed(1)}% de ${(q.tokens_quota||0).toLocaleString()} tok · ${(q.tokens_remaining||0).toLocaleString()} libres</div>
        <div style="margin-top:4px">${models}</div></div>`;
    }).join('') || '<span style="color:var(--muted)">Sin clientes con uso</span>';
    document.getElementById('billing-clients').innerHTML = html;
  } catch(e) { document.getElementById('billing-clients').innerHTML = '<span style="color:var(--red)">Error</span>'; }
  try {
    const k = await get('/admin/keys');
    const html = (k.keys || []).map(rec => `<div class="key-row">
      <div><div><b>${rec.client_id}</b> <span class="tag cpu">${rec.plan}</span> ${rec.revoked?'<span class="pill L15">REVOKED</span>':''}</div>
      <div style="font-size:11px;color:var(--muted)">${rec.key_masked} ${rec.label?'· '+rec.label:''}</div></div>
      ${rec.revoked?'':`<button class="r copy-btn" onclick="revokeKey('${rec.key}')">revocar</button>`}</div>`).join('') || '<span style="color:var(--muted)">Sin keys</span>';
    document.getElementById('billing-keys').innerHTML = html;
  } catch(e) { document.getElementById('billing-keys').innerHTML = '<span style="color:var(--red)">Error</span>'; }
}

// ── Request Log ─────────────────────────────────────────────────────────────
let offset = 0;
async function loadReqLog(off){
  offset = Math.max(0, off||0);
  const client = document.getElementById('log-filter-client').value.trim();
  const model = document.getElementById('log-filter-model').value.trim();
  const errors = document.getElementById('log-filter-errors').checked;
  let url = `/api/billing/log?limit=50&offset=${offset}`;
  if(client) url += `&client_id=${encodeURIComponent(client)}`;
  if(model) url += `&model=${encodeURIComponent(model)}`;
  if(errors) url += `&only_errors=true`;
  try {
    const r = await get(url);
    const rows = (r.requests||[]).map(req=>{
      const ts = new Date(req.ts*1000).toLocaleString('es',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit'});
      const errCls = req.status_code>=400 ? 'row-err' : '';
      const rat = req.rating>0?'👍':req.rating<0?'👎':'<span class="rating-none">—</span>';
      return `<tr class="${errCls}"><td style="font-size:11px;color:var(--muted)">${ts}</td><td>${req.client_id}</td><td><span class="tag g1">${req.model}</span></td><td>${(req.prompt_tokens+req.completion_tokens).toLocaleString()}</td><td>$${req.cost_usd.toFixed(6)}</td><td>${req.latency_ms}ms</td><td>${req.status_code}</td><td>${rat}</td><td><button onclick="showReq(${req.id})">ver</button></td></tr>`;
    }).join('');
    document.getElementById('log-body').innerHTML = rows || '<tr><td colspan="9" style="color:var(--muted)">sin resultados</td></tr>';
    document.getElementById('log-page').textContent = `offset ${offset}`;
  } catch(e) { console.error('log',e); }
}
async function showReq(id){
  try {
    const r = await get(`/api/billing/log/${id}`);
    document.getElementById('md-id').textContent = id;
    document.getElementById('md-meta').innerHTML = `Cliente: <b>${r.client_id}</b> · Modelo: <b>${r.model}</b> · Tokens: ${r.prompt_tokens}+${r.completion_tokens} · Costo: $${r.cost_usd.toFixed(6)} · Lat: ${r.latency_ms}ms · Status: ${r.status_code} · ${r.stream?'stream':'non-stream'} · Rating: ${r.rating>0?'👍 up':r.rating<0?'👎 down':'—'}`;
    document.getElementById('md-prompt').textContent = r.prompt || '(vacio)';
    document.getElementById('md-response').textContent = r.response || '(vacio)';
    document.getElementById('req-modal').style.display = 'block';
  } catch(e) { console.error('showReq',e); }
}
async function rateReq(rating){
  const id = document.getElementById('md-id').textContent;
  await putJSON(`/api/billing/log/${id}/rating`, {rating});
  showReq(parseInt(id)); loadReqLog(offset);
}

// ── Precios y Planes ─────────────────────────────────────────────────────────
async function loadConfig(){
  try {
    const cfg = await get('/api/billing/config');
    const prices = Object.entries(cfg.prices||{}).map(([m,p])=>
      `<div class="price-row"><span class="tag g1" style="min-width:80px">${m}</span><input type="number" step="0.01" value="${p.input}" id="pr-in-${m}" placeholder="in"><input type="number" step="0.01" value="${p.output}" id="pr-out-${m}" placeholder="out"><input type="text" value="${p.unit}" id="pr-unit-${m}" style="width:60px" placeholder="unit"><button class="s" onclick="savePrice('${m}')">guardar</button></div>`).join('');
    document.getElementById('prices-editor').innerHTML = prices || '<span style="color:var(--muted)">sin modelos</span>';
    const plans = Object.entries(cfg.plans||{}).map(([k,v])=>
      `<div class="price-row"><span class="tag cpu" style="min-width:80px">${k}</span><input type="text" value="${v.name}" id="pl-name-${k}" style="width:80px"><input type="number" value="${v.tokens_quota}" id="pl-quota-${k}" placeholder="quota"><input type="number" value="${v.rpm}" id="pl-rpm-${k}" style="width:60px" placeholder="rpm"><button class="s" onclick="savePlan('${k}')">guardar</button><button class="r" onclick="deletePlan('${k}')">eliminar</button></div>`).join('');
    document.getElementById('plans-editor').innerHTML = plans || '<span style="color:var(--muted)">sin planes</span>';
  } catch(e) { console.error('config',e); }
}
async function savePrice(model){
  const inp = document.getElementById('pr-in-'+model).value;
  const out = document.getElementById('pr-out-'+model).value;
  const unit = document.getElementById('pr-unit-'+model).value;
  const r = await putJSON('/api/billing/prices', {model, input_price:parseFloat(inp), output_price:parseFloat(out), unit});
  if(r.ok) alert('Precio actualizado'); else alert('Error: '+JSON.stringify(r.data));
}
async function savePlan(plan){
  const name = document.getElementById('pl-name-'+plan).value, quota = document.getElementById('pl-quota-'+plan).value, rpm = document.getElementById('pl-rpm-'+plan).value;
  const r = await putJSON('/api/billing/plans', {plan, tokens_quota:parseInt(quota), rpm:parseInt(rpm), name});
  if(r.ok) alert('Plan actualizado'); else alert('Error: '+JSON.stringify(r.data));
}
async function deletePlan(plan){
  if(!confirm('Eliminar plan '+plan+'? (solo si no tiene keys)')) return;
  const r = await post('/api/billing/plans/delete', {plan});
  if(r.deleted) loadConfig(); else alert('Error: '+JSON.stringify(r));
}
async function createPlan(){
  const name = document.getElementById('np-name').value.trim(), display = document.getElementById('np-display').value.trim(), quota = document.getElementById('np-quota').value, rpm = document.getElementById('np-rpm').value;
  if(!name||!quota||!rpm){ alert('Completa todos los campos'); return; }
  const r = await putJSON('/api/billing/plans', {plan:name, tokens_quota:parseInt(quota), rpm:parseInt(rpm), name:display||name});
  if(r.ok){ document.getElementById('np-name').value=''; loadConfig(); } else alert('Error: '+JSON.stringify(r.data));
}

// ── Init ─────────────────────────────────────────────────────────────────────
refresh(); loadOverview();
setInterval(()=>{ refresh(); if(document.getElementById('page-overview').classList.contains('active')) loadOverview(); }, 10000);

</script>
</body>
</html>
"""

if __name__ == "__main__":
    import uvicorn
    import threading as _threading
    # ── Sincronización multi-nodo vía Firestore ─────────────────────────────
    # Si OJOIA_SYNC=0 o firebase-key.json no está disponible, NO se hace sync.
    # Esto es importante porque el free tier de Firestore tiene 429 quota
    # que rompe el health-monitor si se supera.
    # Por defecto está DESHABILITADO para evitar 429. Activar con OJOIA_SYNC=1.
    OJOIA_SYNC_ENABLED = os.environ.get("OJOIA_SYNC", "0") == "1"

    if not OJOIA_SYNC_ENABLED:
        print("[megapanel] ojoia_sync DESHABILITADO (OJOIA_SYNC=0). "
              "El panel web no se sincronizará con Firestore hasta que se active explícitamente.")
    else:
        # ── Sincronización multi-nodo vía Firestore ─────────────────────────────
        try:
            from ojoia_sync import OjoiaSync
            _sync = OjoiaSync(node_id=NODE_ID)
            if _sync.enabled:
                def _sync_worker():
                    import asyncio
                    async def _loop():
                        SYNC_INTERVAL = 60   # status cada 60s (conservador)
                        BILLING_INTERVAL = 600  # billing cada 10min
                        last_billing = 0.0
                        last_status = 0.0
                        while True:
                            now = asyncio.get_event_loop().time()
                            if (now - last_status) >= SYNC_INTERVAL:
                                try:
                                    status_data = status() if callable(status) else {}
                                    if asyncio.iscoroutine(status_data):
                                        status_data = await status_data
                                    _sync.push_status(status_data)
                                    last_status = now
                                except Exception as _e:
                                    print(f"[ojoia_sync] push error: {_e}")
                            try:
                                ctrl = control if callable(control) else None
                                if ctrl and asyncio.iscoroutinefunction(ctrl):
                                    await _sync.poll_control(ctrl)
                                elif ctrl:
                                    _sync.poll_control(ctrl)
                            except Exception as _e:
                                print(f"[ojoia_sync] poll error: {_e}")
                            if (now - last_billing) >= BILLING_INTERVAL:
                                try:
                                    _sync.push_billing(_sync.billing_provider())
                                    last_billing = now
                                except Exception as _e:
                                    print(f"[ojoia_sync] billing sync error: {_e}")
                            await asyncio.sleep(10)
                    asyncio.run(_loop())
                t = _threading.Thread(target=_sync_worker, daemon=True)
                t.start()
                print(f"[megapanel] ojoia_sync iniciado para nodo={NODE_ID} "
                      f"(status=60s, billing=10min, con backoff)")
        except Exception as e:
            print(f"[megapanel] ojoia_sync no disponible: {e}")

    # A2 (defense in depth): bind 127.0.0.1 unicamente. El panel se expone
    # exclusivamente via el tunel Cloudflare -> nginx 19001 -> 127.0.0.1:9001.
    # Antes bind="0.0.0.0" lo dejaba alcanzable en la LAN 10.0.0.44 sin pasar
    # por nginx/CF (bypass de toda proteccion).
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("MEGAPANEL_PORT", "9001")),
                log_level="info")
