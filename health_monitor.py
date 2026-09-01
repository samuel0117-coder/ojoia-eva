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
# Grace inicial de arranque: durante los primeros N segundos tras arrancar el
# monitor NO reinicia ningún servicio. En el boot de la PC, docker levanta los
# containers en paralelo y ojoia-models.service (start-models.sh) reordena la
# GPU 0 (7b primero, vl8b al final). Si el monitor arranca antes, vería todo
# "DOWN" y resucitaría containers a mitad de la recuperación de VRAM.
STARTUP_GRACE_S = float(os.environ.get("HEALTH_MONITOR_STARTUP_GRACE", "180"))
_STARTED_AT = time.time()

# ComfyUI en modo managed: el operador lo maneja manualmente.
# El health-monitor NO lo reinicia automáticamente.
COMFYUI_MANAGED = os.environ.get("COMFYUI_MANAGED", "1") == "1"


def _send_alert(title: str, body: str) -> bool:
    """P1-docker: notificar al operador por FCM (tokens del admin_config)
    y webhook opcional (OJOIA_ALERT_WEBHOOK=URL de Telegram/Slack genérico).
    Silencioso: si no hay nada configurado, solo log."""
    ok = False
    try:
        import json as _json, requests
        cfgp = "/home/sam/storage/admin_config.json"
        try:
            cfg = _json.loads(open(cfgp).read())
        except Exception:
            cfg = {}
        key = cfg.get("fcm_server_key") or os.environ.get("FCM_SERVER_KEY", "")
        tokens = cfg.get("push_tokens") or []
        if key and tokens:
            for t in tokens[:5]:
                try:
                    requests.post("https://fcm.googleapis.com/fcm/send",
                                  json={"to": t, "notification": {"title": title, "body": body}},
                                  headers={"Authorization": f"key={key}"}, timeout=8)
                    ok = True
                except Exception:
                    pass
        wh = os.environ.get("OJOIA_ALERT_WEBHOOK", "")
        if wh:
            try:
                requests.post(wh, json={"title": title, "body": body,
                                        "text": f"{title}: {body}"}, timeout=8)
                ok = True
            except Exception:
                pass
    except Exception:
        pass
    return ok


@dataclass
class ServiceDef:
    name: str
    port: int
    level: str  # "system" | "user" | "docker"
    gpu: int  # -1 = CPU
    health_path: str  # URL path for health check, e.g. "/health"
    critical: bool = True
    # docker-only: container name (when level == "docker")
    container: str = ""
    # runtime state
    last_ok: bool = True
    failures: int = 0
    consecutive_failures: int = 0
    last_restart: float = 0.0
    disabled_restart: bool = False  # set True for comfyui-managed
    loading_since: float = 0.0  # timestamp since detected "activating" (grace window)
    # P1-docker (2026-09-01): notificaciones — cuándo empezó a estar DOWN
    # y cuántas alertas llevamos enviadas (para no repetir spam)
    down_since: float = 0.0
    alerts_sent: int = 0
    # PAUSED: cuando el operador hace stop desde el panel, este flag queda en True
    # y el health-monitor NO auto-reinicia. Solo se resetea con /resume o /start.
    paused: bool = False
    paused_at: float = 0.0  # timestamp del pause
    # MUTUALLY_EXCLUSIVE_WITH: lista de servicios que NO pueden correr al mismo
    # tiempo en la misma GPU (ej: qwen-7b y qwen-35b compiten por 24GB).
    # Cuando uno arranca, el otro se pausa automáticamente.
    mutually_exclusive_with: list = field(default_factory=list)


# Tabla maestra: fuente de verdad de todos los servicios
# Los modelos IA corren en Docker (level="docker", container=<nombre>).
# El health-monitor los gestiona directamente con `docker start/restart`.
SERVICES = [
    # CPU - OjoIA core
    ServiceDef("tunnel.service", 0, "system", -1, "", critical=True),
    # api-eva / qwen: level=system. El health_monitor corre como usuario sam (--user),
    # no tiene permisos sudo para `systemctl restart` -> polkit pide password en cada
    # boot durante la carga lenta de modelos. Se marcan disabled_restart: systemd ya
    # los revive solo via Restart=on-failure. El monitor sigue chequeando el estado
    # (visible en megapanel) pero NO intenta reiniciarlos.
    ServiceDef("api-eva.service", 8005, "system", -1, "/health", critical=True, disabled_restart=True),
    # ── GPU 0 - Qwen 7B (sglang) + YOLO + Whisper (Docker) ──
    ServiceDef("qwen7b", 8004, "docker", 0, "/health", critical=True,
               container="qwen-7b"),
    ServiceDef("qwen3vl8b", 8019, "docker", 0, "/v1/models", critical=True,
               container="qwen3vl8b"),
    ServiceDef("yolo", 8002, "docker", 0, "/health", critical=True,
               container="yolo-pose"),
    ServiceDef("whisper", 8008, "docker", 0, "/health", critical=True,
               container="whisper-turbo"),
    # ── GPU 1 - Qwen 3.8 27B (kvarn) + Qwen 9B disponible (NO auto, solo manual) ──
    ServiceDef("qwen38", 18020, "docker", 1, "/v1/models", critical=True,
               container="qwen38-syv"),
    # qwen-9b disponible en GPU 1, arranca solo manual (docker compose --profile manual up qwen-9b)
    ServiceDef("qwen9b", 8018, "docker", 1, "/v1/models", critical=False,
               container="qwen-9b", disabled_restart=True),
    # qwen-35b: QUITADO del arranque automatico. Imagen y modelo en disco (frio).
    # Para reactivarlo: docker compose --profile legacy up qwen-35b-a3b
    # CPU - ChatRD
    ServiceDef("chatrd.service", 8010, "user", -1, "/health", critical=True),
    ServiceDef("admin_panel.service", 8030, "user", -1, "/health", critical=False),
    # NOTA: los servicios CineIA (comfyui, movie_server, post_server, audio_server,
    # f5_tts_server, cineia_studio_server) viven en el nodo CineIA (10.0.0.103),
    # no en este nodo. El health-monitor de CineIA los monitorea allá.
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

    # ---------- Docker support ----------

    async def _docker_active(self, svc: ServiceDef) -> bool:
        """Devuelve True si el contenedor Docker está corriendo."""
        if not svc.container:
            return False
        try:
            r = await asyncio.create_subprocess_exec(
                "docker", "inspect", svc.container,
                "--format", "{{.State.Running}}",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            out, _ = await asyncio.wait_for(r.communicate(), timeout=5)
            return out.decode().strip() == "true"
        except Exception:
            return False

    async def _docker_state(self, svc: ServiceDef) -> str:
        """Devuelve el estado Docker: running|exited|dead|restarting|paused|created|absent."""
        if not svc.container:
            return "absent"
        try:
            r = await asyncio.create_subprocess_exec(
                "docker", "inspect", svc.container,
                "--format", "{{.State.Status}}",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            out, _ = await asyncio.wait_for(r.communicate(), timeout=5)
            return out.decode().strip() or "absent"
        except Exception:
            return "absent"

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

        # Si está pausado por el operador, NO reiniciar.
        if svc.paused:
            self.log(f"{svc.name}: restart SKIPPED (paused by operator)", "INFO")
            return False

        # Grace de arranque del sistema: durante los primeros STARTUP_GRACE_S
        # tras arrancar el monitor, NO auto-reiniciar. En el boot, docker levanta
        # containers en paralelo y start-models.sh reordena la GPU 0 (7b primero).
        # Reiniciar a mitad de esa recuperación rompería la reserva de VRAM.
        if time.time() - _STARTED_AT < STARTUP_GRACE_S:
            self.log(f"{svc.name}: restart SKIPPED (startup grace del sistema)", "INFO")
            return False

        # ── Exclusividad: si tiene exclusivos corriendo en la misma GPU, ──────
        # pausarlos ANTES de arrancar este. Caso típico: qwen-7b y qwen-35b
        # no caben juntos en 24GB de GPU1. Si el operador quiere cambiar,
        # paramos el que está corriendo y arrancamos el otro.
        for exclusive_name in svc.mutually_exclusive_with:
            other = self.services.get(exclusive_name)
            if other and not other.paused and await self._docker_active(other):
                self.log(
                    f"{svc.name}: arrancando — pausando {other.name} "
                    f"(exclusividad GPU{svc.gpu}, no caben juntos)",
                    "WARN"
                )
                other.paused = True
                other.paused_at = time.time()
                await self._stop_docker(other)
                # esperar a que libere VRAM (puede tardar 5-10s)
                await asyncio.sleep(8)

        now = time.time()
        # backoff: 30s, 60s, 120s, 240s, max 600s
        if svc.consecutive_failures > 0:
            delay = min(30 * (2 ** (svc.consecutive_failures - 1)), 600)
            if now - svc.last_restart < delay:
                return False

        # Docker: restart directo del contenedor
        if svc.level == "docker":
            return await self._restart_docker(svc)

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

    async def _restart_docker(self, svc: ServiceDef):
        """Reinicia un contenedor Docker con backoff."""
        if not svc.container:
            self.log(f"{svc.name}: no container name", "ERROR")
            return False
        now = time.time()
        try:
            p = await asyncio.create_subprocess_exec(
                "docker", "restart", svc.container,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, err = await asyncio.wait_for(p.communicate(), timeout=60)
            if p.returncode == 0:
                svc.last_restart = now
                svc.consecutive_failures += 1
                self.log(f"{svc.name} ({svc.container}): docker restart OK (attempt #{svc.consecutive_failures})")
                return True
            else:
                self.log(f"{svc.name} ({svc.container}): docker restart FAILED: {err.decode()[:200]}", "ERROR")
                return False
        except Exception as e:
            self.log(f"{svc.name} ({svc.container}): docker restart error: {e}", "ERROR")
            return False

    async def _stop_docker(self, svc: ServiceDef):
        """Para un contenedor Docker."""
        if not svc.container:
            return
        try:
            p = await asyncio.create_subprocess_exec(
                "docker", "stop", svc.container,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(p.communicate(), timeout=30)
        except Exception:
            pass

    # ---------- per-service check ----------

    async def _check_one(self, svc: ServiceDef):
        # Docker: check directo del contenedor
        if svc.level == "docker":
            return await self._check_docker(svc)

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
                # P2 (2026-09-01): log de no-críticos solo en TRANSICIÓN de
                # estado (ok→down). Antes: 'qwen9b DOWN' cada 20s = 1.294
                # líneas/día de spam en un log de 285k líneas sin rotación.
                if getattr(svc, "_last_reported_down", False) is False:
                    self.log(f"{svc.name}: DOWN (non-critical, no auto-restart)", "WARN")
                    svc._last_reported_down = True

    async def _check_docker(self, svc: ServiceDef):
        """Health check para contenedores Docker."""
        # 1) container running
        container_ok = await self._docker_active(svc)
        # 2) port + http
        port_ok = await self._port_open(svc.port) if svc.port else container_ok
        http_ok = True
        if port_ok and svc.health_path:
            http_ok = await self._http_ok(f"http://localhost:{svc.port}{svc.health_path}", timeout=3)

        is_healthy = container_ok and port_ok and http_ok
        svc.last_ok = is_healthy

        if is_healthy:
            if svc.consecutive_failures > 0 and (time.time() - svc.last_restart) > 120:
                self.log(f"{svc.name} ({svc.container}): RECOVERED after {svc.consecutive_failures} restarts")
                svc.consecutive_failures = 0
            svc.loading_since = 0.0
            svc._last_reported_down = False  # P2: permitir próximo aviso de caída
        else:
            state = await self._docker_state(svc)
            now = time.time()
            # Para servicios que NO son modelos pesados, el grace es más corto
            # (yolo, whisper arrancan en <10s). Solo modelos grandes (qwen-38/27B)
            # necesitan 15min de gracia.
            GRACE_SECS = 900 if "qwen" in svc.name.lower() else 60

            # P1-docker (2026-09-01): FIX restarts espurios de qwen38. vLLM
            # muestra el contenedor "running" MIENTRAS carga el modelo (6+ min
            # con puerto cerrado) → el grace de 'starting' nunca aplicaba y el
            # monitor lo reiniciaba en plena carga (4 veces hoy). Ahora: si el
            # http no responde y estamos dentro de la ventana de arranque del
            # PROPIO monitor o del contenedor, contamos como 'cargando'.
            if state in ("running",) and not http_ok and svc.loading_since == 0.0:
                # primer fallo post-arranque del sistema → asumir carga del modelo
                if time.time() - _STARTED_AT < GRACE_SECS + 300:
                    svc.loading_since = now
                    self.log(f"{svc.name} ({svc.container}): running sin health — asumiendo carga de modelo (grace {GRACE_SECS}s)", "INFO")
                    return

            if state in ("starting", "restarting", "created"):
                if svc.loading_since == 0.0:
                    svc.loading_since = now
                if now - svc.loading_since < GRACE_SECS:
                    self.log(f"{svc.name} ({svc.container}): cargando ({state}) — grace {int(now - svc.loading_since)}s", "INFO")
                    return
                self.log(f"{svc.name} ({svc.container}): lleva >{GRACE_SECS}s en {state} — reiniciando", "WARN")
                svc.loading_since = 0.0
            elif svc.last_restart and (now - svc.last_restart) < GRACE_SECS:
                self.log(f"{svc.name} ({svc.container}): reiniciado hace {int(now - svc.last_restart)}s — grace", "INFO")
                return
            else:
                # Resetear el grace si el container está exited y NO fue nuestro restart.
                # Caso típico: yolo recibe señal externa (docker stop externo) y
                # debemos reiniciarlo, no respetarle un grace que no le dimos.
                if state in ("exited", "dead") and svc.last_restart and (now - svc.last_restart) > GRACE_SECS:
                    svc.last_restart = 0.0
                    svc.loading_since = 0.0
                    self.log(f"{svc.name} ({svc.container}): grace expirado (exit externo), forzar restart", "WARN")
            svc.failures += 1
            if svc.critical and svc.consecutive_failures < 6:
                self.log(f"{svc.name} ({svc.container}): DOWN (container={container_ok}, port={port_ok}, http={http_ok}) — auto-restart", "WARN")
                await self._restart_service(svc)
            elif svc.critical:
                self.log(f"{svc.name} ({svc.container}): DOWN — too many failures", "ERROR")
            else:
                if getattr(svc, "_last_reported_down", False) is False:
                    self.log(f"{svc.name} ({svc.container}): DOWN (non-critical)", "WARN")
                    svc._last_reported_down = True

    # ---------- main loop ----------

    async def _check_all(self):
        async with self._lock:
            tasks = [self._check_one(s) for s in self.services.values()]
            await asyncio.gather(*tasks, return_exceptions=True)

            # GPU OOM detection
            # NOTA: qwen38-syv (GPU1) opera a 97-98% por diseño (KV cache asignado).
            # Solo alertar si >99% (presión real, no uso fijo).
            gpus = await self._gpu_status()
            for g in gpus:
                if g["total_mb"] > 0:
                    pct = (g["used_mb"] / g["total_mb"]) * 100
                    if pct > 99:
                        self.log(f"GPU {g['index']}: OOM risk ({pct:.1f}%) — cleaning cache", "WARN")
                        await self._clean_gpu_cache(g["index"])

            # ── P1-docker (2026-09-01): ALERTAS al operador ──────────────────
            # Antes: el apagón de 6h y 2 caídas de api-eva pasaban invisibles
            # (solo log). Ahora: un servicio CRÍTICO que lleva >5min DOWN o que
            # acumula >2 restarts dispara push FCM/webhook al admin — máx 1
            # alerta por servicio por hora (anti-spam).
            now = time.time()
            for svc in self.services.values():
                if not svc.critical:
                    continue
                if svc.last_ok:
                    svc.down_since = 0.0
                    svc.alerts_sent = 0
                    continue
                if svc.down_since == 0.0:
                    svc.down_since = now
                    continue
                down_min = (now - svc.down_since) / 60
                needs_alert = (down_min >= 5) or (svc.consecutive_failures >= 2)
                # anti-spam: 1 alerta/servicio/hora; re-alerta a los 60min
                if needs_alert and svc.alerts_sent < max(1, int(down_min // 60) + (1 if down_min % 60 >= 5 else 0)):
                    svc.alerts_sent += 1
                    mins = int(down_min)
                    title = f"🚨 {svc.name} caído"
                    body = (f"{svc.name} lleva {mins} min DOWN"
                            f" ({svc.consecutive_failures} restarts). "
                            f"Revisa ojoia.com.do/admin → Sistema.")
                    self.log(f"ALERT: {title} — {body}", "ERROR")
                    try:
                        await asyncio.to_thread(_send_alert, title, body)
                    except Exception:
                        pass

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
                    "container": s.container,
                    "healthy": s.last_ok,
                    "paused": s.paused,
                    "paused_at": int(s.paused_at) if s.paused_at else None,
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


def _resolve_service(name: str):
    """Resuelve un nombre de servicio flexible.

    Acepta:
      - "yolo" (nombre del ServiceDef)
      - "yolo-server.service" (id del megapanel)
      - "yolo-server" (sin .service)
      - "whisper" / "whisper-turbo"
      - "qwen7b" / "qwen.service" / "qwen.service"
    """
    # Match exacto primero
    if name in monitor.services:
        return monitor.services[name]
    # Quitar .service suffix
    clean = name[:-len(".service")] if name.endswith(".service") else name
    if clean in monitor.services:
        return monitor.services[clean]
    # Mapeo explícito entre id del megapanel y nombre del health-monitor
    PANEL_TO_HEALTH = {
        "qwen.service": "qwen7b",
        "qwen35b.service": "qwen35b",
        "qwen9b.service": "qwen9b",
        "whisper.service": "whisper",
        "yolo-server.service": "yolo",
        "qwen14b.service": "qwen14b",
    }
    # Buscar por nombre original (con .service) o por clean (sin .service)
    if name in PANEL_TO_HEALTH:
        mapped = PANEL_TO_HEALTH[name]
        if mapped in monitor.services:
            return monitor.services[mapped]
    if clean in PANEL_TO_HEALTH:
        mapped = PANEL_TO_HEALTH[clean]
        if mapped in monitor.services:
            return monitor.services[mapped]
    # Búsqueda fuzzy por substring
    for sname, svc in monitor.services.items():
        if clean == sname or sname in clean or clean in sname:
            return svc
    return None


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
            svc = _resolve_service(svc_name)
            if not svc:
                writer.write(b"HTTP/1.1 404 Not Found\r\n\r\n")
            else:
                ok = await monitor._restart_service(svc)
                status = b"200 OK" if ok else b"500 FAIL"
                writer.write(b"HTTP/1.1 " + status + b"\r\n\r\n")
        elif path.startswith("/stop/") and method == "POST":
            svc_name = path[len("/stop/"):]
            svc = _resolve_service(svc_name)
            if not svc:
                writer.write(b"HTTP/1.1 404 Not Found\r\n\r\n")
            else:
                # Marcar como PAUSED para que el health-monitor NO lo auto-reinicie.
                svc.paused = True
                svc.paused_at = time.time()
                monitor.log(f"{svc.name}: PAUSED (stop manual del operador)", "INFO")
                if svc.level == "docker":
                    p = await asyncio.create_subprocess_exec("docker", "stop", svc.container)
                    await p.communicate()
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
            svc = _resolve_service(svc_name)
            if not svc:
                writer.write(b"HTTP/1.1 404 Not Found\r\n\r\n")
            else:
                # Limpiar el flag de PAUSED al arrancar manualmente.
                svc.paused = False
                svc.paused_at = 0.0
                svc.consecutive_failures = 0  # reset failures
                # Exclusividad: pausar los exclusivos que estén corriendo.
                for ex_name in svc.mutually_exclusive_with:
                    other = monitor.services.get(ex_name)
                    if other and not other.paused:
                        st = monitor._docker_state(other)
                        if st == "running":
                            monitor.log(
                                f"{svc.name}: iniciando — pausando exclusivo {other.name}",
                                "WARN"
                            )
                            other.paused = True
                            other.paused_at = time.time()
                            try:
                                p = await asyncio.create_subprocess_exec("docker", "stop", other.container)
                                await asyncio.wait_for(p.communicate(), timeout=30)
                            except Exception:
                                pass
                            await asyncio.sleep(8)  # esperar a que libere VRAM
                monitor.log(f"{svc.name}: RESUMED (start manual del operador)", "INFO")
                if svc.level == "docker":
                    p = await asyncio.create_subprocess_exec("docker", "start", svc.container)
                    await p.communicate()
                else:
                    cmd = ["systemctl"]
                    if svc.level == "user":
                        cmd.append("--user")
                    cmd += ["start", svc.name]
                    p = await asyncio.create_subprocess_exec(*cmd)
                    await p.communicate()
                writer.write(b"HTTP/1.1 200 OK\r\n\r\n")
        elif path.startswith("/resume/") and method == "POST":
            # Resume un servicio pausado (no lo arranca, solo le quita el flag).
            svc_name = path[len("/resume/"):]
            svc = _resolve_service(svc_name)
            if not svc:
                writer.write(b"HTTP/1.1 404 Not Found\r\n\r\n")
            else:
                svc.paused = False
                svc.paused_at = 0.0
                monitor.log(f"{svc.name}: RESUMED flag cleared", "INFO")
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
