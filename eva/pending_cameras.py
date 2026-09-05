"""
eva/pending_cameras.py — Cola de cámaras nuevas detectadas (announces sin reclamar).

Bug #1+#2 (2026-09-04): antes, /devices/announce IGNORABA las cámaras
desconocidas y el wizard de Eva inventaba camera_id sintéticos ("cam_<ts>")
que nunca recibirían frames (cámaras fantasma). Ahora el announce de una
cámara no registrada se guarda aquí (en memoria + json en .runtime) y el
wizard puede (a) mostrar telemetría al usuario ("Veo una cámara nueva
encendiéndose...") y (b) reclamar el camera_id REAL en vez de inventarlo.

NO escribe user.json ni camera.json: la cámara no está reclamada todavía.
"""

import json
import logging
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# Cola en memoria: {camera_id: {rssi, ip_lan, firmware, last_seen}}
_PENDING_CAMERAS: dict = {}
_LOCK = threading.Lock()

# TTL de una entrada en la cola (15 min: margen amplio para un wizard)
_TTL_S = 15 * 60
# Máximo de entradas (evita crecimiento si un atacante spamea announces)
_MAX_ENTRIES = 10
# Un announce se considera "reciente" para telemetría del wizard (<5 min)
_RECENT_S = 5 * 60

# Persistencia: sobrevive reinicios del servicio (opcional pero barata)
_STATE_FILE = Path("/home/sam/storage/.runtime/pending_cameras.json")


def _load_state():
    """Carga la cola desde disco (al importar) descartando entradas vencidas."""
    try:
        if _STATE_FILE.exists():
            data = json.loads(_STATE_FILE.read_text())
            now = time.time()
            for cid, info in (data or {}).items():
                if isinstance(info, dict) and now - float(info.get("last_seen", 0)) < _TTL_S:
                    _PENDING_CAMERAS[cid] = info
    except Exception as e:
        logger.warning(f"[pending-cams] load state falló: {e}")


def _persist_state():
    """Vuelca la cola a disco (best-effort, no rompe el flujo si falla)."""
    try:
        _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _STATE_FILE.write_text(json.dumps(_PENDING_CAMERAS, indent=2))
    except Exception as e:
        logger.warning(f"[pending-cams] persist state falló: {e}")


def register_announce(camera_id: str, rssi=None, ip_lan: str = "", firmware: str = "") -> dict:
    """Registra el announce de una cámara desconocida. Devuelve la entrada."""
    now = time.time()
    with _LOCK:
        # Purga entradas vencidas antes de insertar
        for cid in [c for c, i in _PENDING_CAMERAS.items() if now - i.get("last_seen", 0) > _TTL_S]:
            _PENDING_CAMERAS.pop(cid, None)
        entry = _PENDING_CAMERAS.get(camera_id) or {}
        entry.update({
            "rssi": rssi, "ip_lan": ip_lan or entry.get("ip_lan", ""),
            "firmware": firmware or entry.get("firmware", ""),
            "first_seen": entry.get("first_seen", now), "last_seen": now,
        })
        _PENDING_CAMERAS[camera_id] = entry
        # Cap de entradas: conserva las más recientes
        if len(_PENDING_CAMERAS) > _MAX_ENTRIES:
            for cid in sorted(_PENDING_CAMERAS, key=lambda c: _PENDING_CAMERAS[c]["last_seen"])[:-_MAX_ENTRIES]:
                _PENDING_CAMERAS.pop(cid, None)
        _persist_state()
        return dict(entry)


def list_recent(max_age_s: float = _RECENT_S) -> dict:
    """Cámaras con announce reciente (default <5 min), sin reclamar."""
    now = time.time()
    with _LOCK:
        return {cid: dict(info) for cid, info in _PENDING_CAMERAS.items()
                if now - info.get("last_seen", 0) <= max_age_s}


def claim(camera_id: str) -> dict:
    """Saca una cámara de la cola (se registró / vinculó). Devuelve la entrada."""
    with _LOCK:
        entry = _PENDING_CAMERAS.pop(camera_id, None)
        if entry is not None:
            _persist_state()
        return dict(entry) if entry else {}


_load_state()
