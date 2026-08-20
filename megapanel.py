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
]


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

    return {
        "timestamp": datetime.now().isoformat(),
        "hostname": platform.node(),
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
    """Controlar servicio: start | stop | restart | enable | disable."""
    if action not in ("start", "stop", "restart", "enable", "disable"):
        raise HTTPException(400, "action must be start|stop|restart|enable|disable")
    if not service_id.endswith(".service"):
        service_id += ".service"
    level = "user"
    for s in SERVICES:
        if s["id"] == service_id:
            level = s["level"]
            break
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
      <thead><tr><th>Servicio</th><th>Puerto</th><th>GPU</th><th>Estado</th><th>Enabled</th><th></th></tr></thead>
      <tbody id="services"></tbody>
    </table>
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
    document.getElementById('host').textContent = s.hostname + ' · uptime ' + Math.floor(s.uptime_s/3600) + 'h';
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
    // services
    document.getElementById('svc-count').textContent = '(' + s.services.length + ')';
    document.getElementById('services').innerHTML = s.services.map(sv=>`
      <tr>
        <td>${sv.name}<div style="color:var(--muted);font-size:11px">${sv.id}</div></td>
        <td>${sv.port||'-'}</td>
        <td>${gpuTag(sv.gpu)}</td>
        <td><span class="dot ${sv.active?'dot-on':'dot-off'}"></span>${sv.active?'OK':'DOWN'}</td>
        <td style="color:var(--muted);font-size:11px">${sv.enabled||'-'}</td>
        <td>
          <button class="s" onclick="control('${sv.id}','start')">▶</button>
          <button class="r" onclick="control('${sv.id}','stop')">■</button>
          <button onclick="control('${sv.id}','restart')">↻</button>
          <button onclick="loadLog('${sv.id}')">logs</button>
        </td>
      </tr>`).join('');
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
    document.getElementById('incidents').textContent = s.incidents.map(i=>`[${i.t}] [${i.level}] ${i.msg}`).join('\\n');
  } catch(e) {
    console.error(e);
  }
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
  document.getElementById('logs').textContent = r.lines.join('\\n');
  document.getElementById('logs').scrollTop = 9999;
}
async function toggleMaint(enable){
  let ext = '';
  if(enable){ ext = prompt('URL de API externa para fallback (dejar vacío si ninguna):') || ''; }
  await post('/api/maintenance', {enable, external_api_url: ext});
  refresh();
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
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("MEGAPANEL_PORT", "9001")),
                log_level="info")
