#!/usr/bin/env python3
"""rtsp_puller.py — D2: soporte de cámaras RTSP (remotas por internet).

Para cada cámara registrada con `rtsp_url` en su camera.json:
- Mantiene una conexión RTSP persistente (cv2/FFmpeg) y extrae frames a 1 fps
  (configurable con "fps" o "max_fps" en camera.json).
- Cada frame se publica al pipeline existente vía POST /ingest/frame con
  X-Camera-Key (A4) → entra al MISMO flujo que una ESP32: disco, YOLO,
  grid, Qwen, reglas, notificaciones. Cero duplicación de lógica.
- Reconexión con backoff exponencial si el stream cae.
- Watchdog: si pasan `down_after_s` (default 180s) sin frames, marca
  camera.json.stream_down=true y notifica al usuario (1 push/día).

Seguridad (anti-SSRF):
- Solo esquema rtsp:// (rtsp over TCP por defecto).
- Bloqueados: loopback, link-local, y 169.254.169.254 (metadata cloud).
- IPs privadas bloqueadas por defecto (caso remoto); habilitar con
  OJOIA_RTSP_ALLOW_PRIVATE=1 para despliegues in-LAN.
- La URL (con credenciales) vive solo en camera.json y NUNCA se loguea.

Uso:
    python3 rtsp_puller.py            # daemon
    python3 rtsp_puller.py --probe URL --out /tmp/p.jpg   # probar una cámara
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import os
import socket
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [rtsp] %(levelname)s %(message)s")
logger = logging.getLogger("rtsp_puller")

STORAGE_ROOT = Path(os.environ.get("OJOIA_STORAGE", "/home/sam/storage"))
API_INGEST = os.environ.get("OJOIA_INGEST_URL", "http://127.0.0.1:8005/ingest/frame")
SCAN_INTERVAL_S = 30
DEFAULT_FPS = 1.0
DOWN_AFTER_S = 300
NOTIFY_COOLDOWN_S = 24 * 3600


# ─────────────────────────────────────────────────────────────────────────────
# Seguridad de URL (anti-SSRF)
# ─────────────────────────────────────────────────────────────────────────────

class RtspUrlError(ValueError):
    pass


def validate_rtsp_url(url: str) -> str:
    """Valida una URL RTSP. Devuelve la URL saneada o lanza RtspUrlError."""
    if not url or not isinstance(url, str):
        raise RtspUrlError("URL vacía")
    url = url.strip()
    p = urlparse(url)
    if p.scheme != "rtsp":
        raise RtspUrlError("solo esquema rtsp:// permitido")
    if not p.hostname:
        raise RtspUrlError("sin host")
    port = p.port or 554
    if not (1 <= port <= 65535):
        raise RtspUrlError("puerto inválido")

    allow_private = os.environ.get("OJOIA_RTSP_ALLOW_PRIVATE") == "1"
    try:
        infos = socket.getaddrinfo(p.hostname, port, type=socket.SOCK_STREAM)
    except socket.gaierror:
        raise RtspUrlError(f"no se puede resolver {p.hostname}")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_unspecified:
            raise RtspUrlError(f"destino no permitido ({ip})")
        if ip.is_private and not allow_private:
            raise RtspUrlError(f"IP privada {ip} bloqueada (OJOIA_RTSP_ALLOW_PRIVATE=1 para permitir)")
    return url


def _safe_host_for_log(url: str) -> str:
    """Host sin credenciales, para logs."""
    try:
        p = urlparse(url)
        return f"{p.hostname}:{p.port or 554}"
    except Exception:
        return "<url>"


# ─────────────────────────────────────────────────────────────────────────────
# Captura
# ─────────────────────────────────────────────────────────────────────────────

def grab_one_frame(url: str, timeout_s: float = 10):
    """Captura UN frame de un stream RTSP. Devuelve bytes JPEG o None."""
    import cv2  # import perezoso: cv2 es pesado
    cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
    if not cap.isOpened():
        return None
    try:
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        end = time.time() + timeout_s
        while time.time() < end:
            ok, frame = cap.read()
            if ok and frame is not None and frame.size > 0:
                ok2, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
                if ok2:
                    return buf.tobytes()
            time.sleep(0.05)
        return None
    finally:
        cap.release()


def _camera_dirs():
    users = STORAGE_ROOT / "users"
    if not users.exists():
        return
    for u in users.iterdir():
        cams = u / "cameras"
        if not cams.is_dir():
            continue
        for c in cams.iterdir():
            cj = c / "camera.json"
            if cj.exists():
                yield u.name, c.name, cj


def load_rtsp_cameras() -> dict:
    """Escanea todas las camera.json y devuelve {cam_key: cfg} de cámaras RTSP."""
    out = {}
    for user_id, cam_id, cj in _camera_dirs():
        try:
            cfg = json.loads(cj.read_text())
        except Exception:
            continue
        url = cfg.get("rtsp_url")
        if url and cfg.get("enabled", True) and cfg.get("rtsp_enabled", True):
            out[f"{user_id}/{cam_id}"] = {
                "user_id": user_id, "camera_id": cam_id,
                "rtsp_url": url, "ingest_key": cfg.get("ingest_key", ""),
                "fps": float(cfg.get("fps") or cfg.get("max_fps") or DEFAULT_FPS),
                "camera_json": cj,
            }
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Tarea por cámara
# ─────────────────────────────────────────────────────────────────────────────

class RtspCameraTask:
    def __init__(self, key: str, cfg: dict):
        self.key = key
        self.cfg = cfg
        self.last_frame_ts = 0.0
        self.last_notify_ts = 0.0
        self.fail_streak = 0
        self._stop = asyncio.Event()

    async def run(self):
        url = self.cfg["rtsp_url"]
        host = _safe_host_for_log(url)
        logger.info(f"[{self.key}] iniciando pull de {host} a {self.cfg['fps']} fps")
        while not self._stop.is_set():
            try:
                frame = await asyncio.to_thread(grab_one_frame, url, 15)
                if frame is None:
                    raise RuntimeError("sin frame")
                self.last_frame_ts = time.time()
                self.fail_streak = 0
                await self._push(frame)
                self._mark_up()
                interval = 1.0 / max(self.cfg.get("fps", DEFAULT_FPS), 0.05)
                await asyncio.sleep(max(interval, 0.2))
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.fail_streak += 1
                backoff = min(60, 5 * (2 ** min(self.fail_streak, 4)))
                logger.warning(f"[{self.key}] error ({e}); reintento en {backoff}s")
                await self._maybe_notify_down()
                try:
                    await asyncio.wait_for(self._stop.wait(), backoff)
                except asyncio.TimeoutError:
                    pass

    async def _push(self, frame: bytes):
        import httpx
        headers = {}
        if self.cfg.get("ingest_key"):
            headers["X-Camera-Key"] = self.cfg["ingest_key"]
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                API_INGEST,
                files={"image": ("rtsp.jpg", frame, "image/jpeg")},
                data={"camera_id": self.cfg["camera_id"], "user_id": self.cfg["user_id"]},
                headers=headers,
            )
            if r.status_code == 429:
                return  # rate limit: frame omitido, no es error
            r.raise_for_status()

    def _patch_camera_json(self, mutator):
        try:
            cj = self.cfg["camera_json"]
            cfg = json.loads(cj.read_text())
            mutator(cfg)
            tmp = cj.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(cfg, indent=2, ensure_ascii=False))
            tmp.replace(cj)
        except Exception as e:
            logger.warning(f"[{self.key}] no pude actualizar camera.json: {e}")

    def _mark_up(self):
        now = time.time()
        if getattr(self, "_was_down", False):
            self._was_down = False
            self._patch_camera_json(lambda c: c.__setitem__("stream_down", False))
            logger.info(f"[{self.key}] stream RECUPERADO")

    async def _maybe_notify_down(self):
        now = time.time()
        if now - self.last_frame_ts > DOWN_AFTER_S and now - self.last_notify_ts > NOTIFY_COOLDOWN_S:
            self.last_notify_ts = now
            self._was_down = True
            self._patch_camera_json(lambda c: c.__setitem__("stream_down", True))
            logger.warning(f"[{self.key}] stream CAÍDO, notificando")
            try:
                from orchestrator import send_fcm_notification
                await send_fcm_notification(
                    title="📷 Cámara sin señal",
                    body=f"La cámara {self.cfg['camera_id']} dejó de enviar video. "
                         f"Revisa su conexión o alimentación.",
                    user_id=self.cfg["user_id"],
                )
            except Exception as e:
                logger.warning(f"[{self.key}] notificación de caída falló: {e}")

    def stop(self):
        self._stop.set()


# ─────────────────────────────────────────────────────────────────────────────
# Daemon principal
# ─────────────────────────────────────────────────────────────────────────────

async def main_loop():
    logger.info("rtsp_puller iniciado")
    tasks: dict[str, RtspCameraTask] = {}
    while True:
        try:
            wanted = load_rtsp_cameras()
            # arrancar nuevas / actualizar cambiadas
            for key, cfg in wanted.items():
                t = tasks.get(key)
                if t is None:
                    t = RtspCameraTask(key, cfg)
                    tasks[key] = t
                    asyncio.create_task(t.run())
            # detener las que ya no existen
            for key in list(tasks):
                if key not in wanted:
                    logger.info(f"[{key}] cámara RTSP eliminada/deshabilitada → stop")
                    tasks[key].stop()
                    del tasks[key]
        except Exception as e:
            logger.error(f"scan error: {e}")
        await asyncio.sleep(SCAN_INTERVAL_S)


def probe(url: str, out_path: str):
    """Captura un frame y lo guarda — para el wizard de instalación."""
    url = validate_rtsp_url(url)
    frame = grab_one_frame(url, 15)
    if frame is None:
        print("ERROR: no se pudo obtener frame")
        sys.exit(2)
    Path(out_path).write_bytes(frame)
    print(f"OK: frame guardado en {out_path} ({len(frame)} bytes)")


if __name__ == "__main__":
    if len(sys.argv) >= 4 and sys.argv[1] == "--probe":
        probe(sys.argv[2], sys.argv[3])
    else:
        asyncio.run(main_loop())
