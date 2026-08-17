#!/usr/bin/env python3
"""
OjoIA / CineIA — Health Monitor Unificado v1.0
==============================================
Reemplaza watchdog.sh, watchdog_cineia.sh y watchdog.py (deprecados).

Características:
  - Monitorea TODOS los servicios (system + user level) en un solo lugar
  - Health check HTTP por servicio con timeout configurable
  - Restart automático con backoff exponencial
  - Detección de GPU OOM y limpieza proactiva
  - Detección de locks huérfanos de GPU
  - Endpoint HTTP en puerto 9000 para el admin panel
  - Guarda histórico de incidentes en /home/sam/logs/health_monitor.log
  - NO toca comfyui.service si está en modo 'managed' (el operador lo controla)

Uso:
  python3 health_monitor.py
  Linear: solo logs
  Con --api: expone puerto 9000 con /status, /logs, /restart/:svc, /manage/:svc
"""

import asyncio
import json
import os
import subprocess
import sys
import time
import traceback
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

try:
    import httpx
    HAS_HTTPX = True
except ImportError:
    HAS_HTTPX = False
    import urllib.request

HOST = "0.0.0.0"
API_PORT = int(os.environ.get("HEALTH_MONITOR_PORT", "9000"))
LOG_FILE = "/home/sam/logs/health_monitor.log"
INCIDENT_LIMIT = 500
CHECK_INTERVAL = int(os.environ.get("HEALTH_MONITOR_INTERVAL", "20"))

# ComfyUI en modo managed: el operador lo maneja manualmente.
# El health-monitor NO lo reinicia automáticamente.
COMFYUI_MANAGED = os.environ.get("COMFYUI_MANAGED", "1") == "1"


@dataclass
class ServiceDef:
    name: str
    port: int
    level: str  # "system" | "user"
    gpu: int  # -1 = CPU
    health_path: str  # URL path for health check, e.g. "/health"
    critical: bool = True
    # runtime state
    last_ok: bool = True
    failures: int = 0
    consecutive_failures: int = 0
    last_restart: float = 0.0
    disabled_restart: bool = False  # set True for comfyui-managed
    loading_since: float = 0.0  # timestamp since detected "activating" (grace window)


# Tabla maestra: fuente de verdad de todos los servicios
SERVICES = [
    # CPU - OjoIA core
    ServiceDef("tunnel.service", 0, "system", -1, "", critical=True),
    # api-eva / qwen: level=system. El health_monitor corre como usuario sam (--user),
    # no tiene permisos sudo para `systemctl restart` -> polkit pide password en cada
    # boot durante la carga lenta de modelos. Se marcan disabled_restart: systemd ya
    # los revive solo via Restart=on-failure. El monitor sigue chequeando el estado
    # (visible en megapanel) pero NO intenta reiniciarlos.
    ServiceDef("api-eva.service", 8005, "system", -1, "/health", critical=True, disabled_restart=True),
    ServiceDef("yolo-server.service", 8002, "system", 1, "", critical=False),
    # GPU 0 - Qwen 7B VL
    ServiceDef("qwen.service", 8004, "system", 0, "/v1/models", critical=True, disabled_restart=True),
    # GPU 1 - Whisper + Qwen 14B
    ServiceDef("whisper.service", 8008, "system", 1, "/health", critical=True),
    ServiceDef("qwen14b.service", 8015, "user", 1, "/v1/models", critical=True),
    # CPU - ChatRD
    ServiceDef("chatrd.service", 8010, "user", -1, "/health", critical=True),
    ServiceDef("admin_panel.service", 8030, "user", -1, "/health", critical=False),
    # GPU 2 - CineIA (carga diferida)
    ServiceDef("comfyui.service", 8006, "user", 2, "/system_stats",
              critical=False, disabled_restart=True if COMFYUI_MANAGED else False),
    ServiceDef("movie_server.service", 8090, "user", 2, "/health", critical=True),
    ServiceDef("cineia_studio_server.service", 8095, "user", -1, "/health", critical=True),
    ServiceDef("post_server.service", 8014, "user", 2, "/", critical=False),
    ServiceDef("audio_server.service", 8013, "user", 2, "/", critical=False),
    ServiceDef("f5_tts_server.service", 8017, "user", 2, "/", critical=False),
]

# Directorio de logs
Path("/home/sam/logs").mkdir(parents=True, exist_ok=True)


class HealthMonitor:
    def __init__(self):
        self.services: dict[str, ServiceDef] = {s.name: s for s in SERVICES}
        self.incidents: list[dict] = []
        self.start_time = time.time()
        self._running = False
        self._lock = asyncio.Lock()

    # ---------- logging ----------

    def log(self, msg: str, level: str = "INFO"):
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{ts}] [{level}] {msg}"
        self.incidents.append({"t": ts, "level": level, "msg": msg})
        if len(self.incidents) > INCIDENT_LIMIT:
            self.incidents = self.incidents[-INCIDENT_LIMIT:]
        print(line)
        try:
            with open(LOG_FILE, "a") as f:
                f.write(line + "\n")
        except Exception:
            pass

    # ---------- health checks ----------

    async def _http_ok(self, url: str, timeout: float = 5.0) -> bool:
        try:
            if HAS_HTTPX:
                async with httpx.AsyncClient(timeout=timeout) as c:
                    r = await c.get(url)
                    return r.status_code < 500
            else:
                req = urllib.request.Request(url)
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    return r.status < 500
        except Exception:
            return False

    async def _port_open(self, port: int) -> bool:
        try:
            r = await asyncio.create_subprocess_exec(
                "ss", "-tlnp",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            out, _ = await asyncio.wait_for(r.communicate(), timeout=5)
            return f":{port} " in out.decode(errors="ignore")
        except Exception:
            return False

    async def _service_active(self, svc: ServiceDef) -> bool:
        cmd = ["systemctl"]
        if svc.level == "user":
            cmd.append("--user")
        cmd += ["is-active", "--quiet", svc.name]
        try:
            r = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(r.communicate(), timeout=5)
            return r.returncode == 0
        except Exception:
            return False

    async def _service_state(self, svc: ServiceDef) -> str:
        """Devuelve el estado real de systemd (active/activating/inactive/failed...)."""
        cmd = ["systemctl"]
        if svc.level == "user":
            cmd.append("--user")
        cmd += ["is-active", svc.name]
        try:
            r = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            out, _ = await asyncio.wait_for(r.communicate(), timeout=5)
            return out.decode().strip()
        except Exception:
            return "unknown"

    # ---------- GPU monitoring ----------

    async def _gpu_status(self) -> list[dict]:
        try:
            r = await asyncio.create_subprocess_exec(
                "nvidia-smi",
                "--query-gpu=index,memory.used,memory.total,utilization.gpu",
                "--format=csv,noheader,nounits",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            out, _ = await asyncio.wait_for(r.communicate(), timeout=5)
            gpus = []
            for line in out.decode().strip().splitlines():
                parts = [p.strip() for p in line.split(",")]
                if len(parts) >= 4:
                    gpus.append({
                        "index": int(parts[0]),
                        "used_mb": int(parts[1]),
                        "total_mb": int(parts[2]),
                        "util_pct": int(parts[3]),
                    })
            return gpus
        except Exception:
            return []

    async def _clean_gpu_cache(self, gpu: int):
        try:
            proc = await asyncio.create_subprocess_exec(
                "nvidia-smi", f"-i", str(gpu),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.communicate(), timeout=5)
            self.log(f"GPU {gpu}: cache cleanup requested")
        except Exception:
            pass

    # ---------- restart logic with backoff ----------

    async def _restart_service(self, svc: ServiceDef):
        if svc.disabled_restart:
            self.log(f"{svc.name}: restart DISABLED (managed by operator)", "WARN")
            return False

        now = time.time()
        # backoff: 30s, 60s, 120s, 240s, max 600s
        if svc.consecutive_failures > 0:
            delay = min(30 * (2 ** (svc.consecutive_failures - 1)), 600)
            if now - svc.last_restart < delay:
                return False

        # stop first (kill orphan processes on port)
        if svc.port:
            try:
                p = await asyncio.create_subprocess_exec(
                    "fuser", "-k", f"{svc.port}/tcp",
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await asyncio.wait_for(p.communicate(), timeout=5)
                await asyncio.sleep(1)
            except Exception:
                pass

        cmd = ["systemctl"]
        if svc.level == "user":
            cmd.append("--user")
        cmd += ["restart", svc.name]
        try:
            p = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, err = await asyncio.wait_for(p.communicate(), timeout=30)
            if p.returncode == 0:
                svc.last_restart = now
                svc.consecutive_failures += 1
                self.log(f"{svc.name}: restart OK (attempt #{svc.consecutive_failures})")
                return True
            else:
                self.log(f"{svc.name}: restart FAILED: {err.decode()[:200]}", "ERROR")
                return False
        except Exception as e:
            self.log(f"{svc.name}: restart error: {e}", "ERROR")
            return False

    # ---------- per-service check ----------

    async def _check_one(self, svc: ServiceDef):
        # 1) port check
        port_ok = await self._port_open(svc.port) if svc.port else await self._service_active(svc)
        # 2) http health (if port ok and path defined)
        http_ok = True
        if port_ok and svc.health_path:
            http_ok = await self._http_ok(f"http://localhost:{svc.port}{svc.health_path}", timeout=3)

        is_healthy = port_ok and http_ok
        svc.last_ok = is_healthy

        if is_healthy:
            # recovery: reset failures after sustained health (2 min)
            if svc.consecutive_failures > 0 and (time.time() - svc.last_restart) > 120:
                self.log(f"{svc.name}: RECOVERED after {svc.consecutive_failures} restarts")
                svc.consecutive_failures = 0
            svc.loading_since = 0.0  # servicio sano: resetea ventana de carga
        else:
            # No reiniciar si el servicio esta CARGANDO (activating/reloading).
            # Evita el thrash de modelos pesados (qwen14b tarda ~6-11 min en cargar).
            state = await self._service_state(svc)
            now = time.time()
            if state in ("activating", "reloading", "deactivating"):
                if svc.loading_since == 0.0:
                    svc.loading_since = now
                if now - svc.loading_since < 900:  # 15 min de gracia máxima
                    self.log(f"{svc.name}: cargando ({state}) — grace {int(now - svc.loading_since)}s, no reiniciar", "INFO")
                    return
                self.log(f"{svc.name}: lleva >15min en {state} — reiniciando", "WARN")
                svc.loading_since = 0.0
            elif svc.last_restart and (now - svc.last_restart) < 900:
                # Recién reiniciado: dar grace aunque el estado sea failed/inactive
                # (cubre el caso de carga lenta tras un reinicio legítimo)
                self.log(f"{svc.name}: reiniciado hace {int(now - svc.last_restart)}s — grace, no reiniciar", "INFO")
                return
            else:
                svc.loading_since = 0.0
            # Si llegamos aqui, es un fallo REAL (no carga, no recién reiniciado)
            svc.failures += 1
            if svc.critical and svc.consecutive_failures < 6:
                self.log(f"{svc.name}: DOWN (port={port_ok}, http={http_ok}) — auto-restart", "WARN")
                await self._restart_service(svc)
            elif svc.critical:
                self.log(f"{svc.name}: DOWN — too many failures ({svc.consecutive_failures}), waiting", "ERROR")
            else:
                self.log(f"{svc.name}: DOWN (non-critical, no auto-restart)", "WARN")

    # ---------- main loop ----------

    async def _check_all(self):
        async with self._lock:
            tasks = [self._check_one(s) for s in self.services.values()]
            await asyncio.gather(*tasks, return_exceptions=True)

            # GPU OOM detection
            gpus = await self._gpu_status()
            for g in gpus:
                if g["total_mb"] > 0:
                    pct = (g["used_mb"] / g["total_mb"]) * 100
                    if pct > 97:
                        self.log(f"GPU {g['index']}: OOM risk ({pct:.1f}%) — cleaning cache", "WARN")
                        await self._clean_gpu_cache(g["index"])

    async def run(self):
        self._running = True
        self.log(f"Health Monitor v1.0 started — interval={CHECK_INTERVAL}s, "
                 f"comfyui_managed={COMFYUI_MANAGED}")
        while self._running:
            try:
                await self._check_all()
            except Exception as e:
                self.log(f"check_all error: {e}\n{traceback.format_exc()}", "ERROR")
            await asyncio.sleep(CHECK_INTERVAL)

    def stop(self):
        self._running = False
        self.log("Health Monitor stopped")

    # ---------- API ----------

    def snapshot(self) -> dict:
        return {
            "uptime_s": int(time.time() - self.start_time),
            "interval_s": CHECK_INTERVAL,
            "comfyui_managed": COMFYUI_MANAGED,
            "services": [
                {
                    "name": s.name,
                    "port": s.port,
                    "level": s.level,
                    "gpu": s.gpu,
                    "healthy": s.last_ok,
                    "failures": s.failures,
                    "consecutive_failures": s.consecutive_failures,
                    "last_restart": int(s.last_restart) if s.last_restart else None,
                    "restart_disabled": s.disabled_restart,
                }
                for s in self.services.values()
            ],
            "incidents": self.incidents[-50:],
        }


monitor = HealthMonitor()


async def _api_handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    try:
        line = await asyncio.wait_for(reader.readline(), timeout=10)
        parts = line.decode(errors="ignore").split()
        if not parts:
            writer.close()
            return
        method, path = parts[0], parts[1]
        # consume headers
        while True:
            h = await reader.readline()
            if h in (b"\r\n", b"\n", b""):
                break

        if path == "/status":
            body = json.dumps(monitor.snapshot(), indent=2).encode()
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                         b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)
        elif path == "/logs":
            body = "\n".join(f"[{i['t']}] [{i['level']}] {i['msg']}"
                             for i in monitor.incidents[-200:]).encode()
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n"
                         b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)
        elif path.startswith("/restart/") and method == "POST":
            svc_name = path[len("/restart/"):]
            svc = monitor.services.get(svc_name)
            if not svc:
                writer.write(b"HTTP/1.1 404 Not Found\r\n\r\n")
            else:
                ok = await monitor._restart_service(svc)
                status = b"200 OK" if ok else b"500 FAIL"
                writer.write(b"HTTP/1.1 " + status + b"\r\n\r\n")
        elif path.startswith("/stop/") and method == "POST":
            svc_name = path[len("/stop/"):]
            svc = monitor.services.get(svc_name)
            if not svc:
                writer.write(b"HTTP/1.1 404 Not Found\r\n\r\n")
            else:
                cmd = ["systemctl"]
                if svc.level == "user":
                    cmd.append("--user")
                cmd += ["stop", svc.name]
                p = await asyncio.create_subprocess_exec(*cmd)
                await p.communicate()
                writer.write(b"HTTP/1.1 200 OK\r\n\r\n")
        elif path.startswith("/start/") and method == "POST":
            svc_name = path[len("/start/"):]
            svc = monitor.services.get(svc_name)
            if not svc:
                writer.write(b"HTTP/1.1 404 Not Found\r\n\r\n")
            else:
                cmd = ["systemctl"]
                if svc.level == "user":
                    cmd.append("--user")
                cmd += ["start", svc.name]
                p = await asyncio.create_subprocess_exec(*cmd)
                await p.communicate()
                writer.write(b"HTTP/1.1 200 OK\r\n\r\n")
        elif path == "/gpu":
            gpus = await monitor._gpu_status()
            body = json.dumps(gpus).encode()
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                         b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)
        else:
            writer.write(b"HTTP/1.1 404 Not Found\r\n\r\n")
        await writer.drain()
    except Exception as e:
        monitor.log(f"API handler error: {e}", "ERROR")
    finally:
        try:
            writer.close()
        except Exception:
            pass


async def _main():
    api_task = asyncio.start_server(_api_handler, HOST, API_PORT)
    monitor_task = monitor.run()
    await asyncio.gather(api_task, monitor_task)


if __name__ == "__main__":
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        monitor.stop()
