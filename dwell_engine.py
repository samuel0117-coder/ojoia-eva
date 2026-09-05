"""
dwell_engine.py — PIEZA 2 del sistema de reglas deterministas de OjoIA.

Motor de "dwell" (permanencia) en RAM por cámara: trackea cuánto tiempo
pasa cada persona (track_id) dentro de cada zona, con una máquina de
estados anti-flicker (OUTSIDE→ENTERING→INSIDE→EXITING) para no rebotar
por detecciones perdidas de 1 frame.

Consumo:
- api_eva worker: process_frame(...) por cada frame YOLO (hook l.~6844).
- orchestrator.process_grid: get_active_dwells() para el prompt y
  eval_rule() para disparar reglas deterministas (PIEZA 3).

Persistencia: .runtime/dwell_states.json cada 30s + load al importar;
poda de tracks sin last_seen > 30 min.
"""
import json
import os
import threading
import time
from typing import Any, Dict, List, Optional

STORAGE_ROOT = "/home/sam/storage"
_RUNTIME_DIR = os.path.join(STORAGE_ROOT, ".runtime")
_STATE_FILE = os.path.join(_RUNTIME_DIR, "dwell_states.json")

# Hiperparámetros de la máquina de estados
MIN_PERSON_CONF = 0.40      # conf mínima para alimentar el motor
FRAMES_TO_CONFIRM = 3       # frames consecutivos en zona → INSIDE
EXIT_GRACE_S = 5.0          # re appearing dentro de grace → INSIDE sin reset
PERSIST_EVERY_S = 30.0      # flush a disco
PRUNE_AFTER_S = 1800.0      # 30 min sin ver el track → limpiar

ST_OUTSIDE, ST_ENTERING, ST_INSIDE, ST_EXITING = "OUTSIDE", "ENTERING", "INSIDE", "EXITING"

# {cam_id: {(track_id, zone_name): {state, frames_in, entered_at, last_seen}}}
_states: Dict[str, Dict[tuple, dict]] = {}
_lock = threading.Lock()
_last_persist = 0.0
_persist_lock = threading.Lock()


# ── Geometría ─────────────────────────────────────────────────────────────

def _anchor_point(bbox: list) -> Optional[tuple]:
    """Bottom-center del bbox (px): punto de anclaje de la persona al suelo."""
    try:
        x1, y1, x2, y2 = [float(v) for v in bbox]
        if x2 <= x1 or y2 <= y1:
            return None
        return ((x1 + x2) / 2.0, y2)
    except (TypeError, ValueError, IndexError):
        return None


def _zone_hit(px: float, py: float, img_w: int, img_h: int, zones: list) -> Optional[str]:
    """Nombre de la primera zona cuyo rect {x,y,w,h} (rel 0-1) contiene el punto."""
    if not zones or not img_w or not img_h:
        return None
    rx, ry = px / img_w, py / img_h
    for z in zones:
        if not isinstance(z, dict):
            continue
        c = z.get("coords") or {}
        try:
            zx = float(c.get("x", 0.0))
            zy = float(c.get("y", 0.0))
            zw = float(c.get("w", 0.0))
            zh = float(c.get("h", 0.0))
        except (TypeError, ValueError):
            continue
        if zx <= rx < zx + zw and zy <= ry < zy + zh:
            return str(z.get("name") or z.get("type") or "zona")
    return None


# ── Máquina de estados por frame ───────────────────────────────────────────

def process_frame(cam_id: str, detections: list, zones: list,
                  img_w: int, img_h: int, now: float) -> None:
    """Actualiza el estado dwell de una cámara con las detecciones de UN frame.

    Solo personas conf>=0.4 CON track_id (sin identidad entre frames no hay
    permanencia medible); ancla bottom-center del bbox; una detección cae en
    a lo sumo 1 zona (la primera cuyo rect contiene el punto). Nunca lanza.
    """
    if not cam_id or not zones or not img_w or not img_h:
        return
    try:
        with _lock:
            cam = _states.setdefault(str(cam_id), {})
            seen_keys = set()
            for det in (detections or []):
                if not isinstance(det, dict):
                    continue
                if str(det.get("class", "")).lower() != "person":
                    continue
                try:
                    if float(det.get("confidence", 0) or 0) < MIN_PERSON_CONF:
                        continue
                except (TypeError, ValueError):
                    continue
                track = det.get("track_id")
                if track is None:
                    continue  # sin track_id no hay dwell entre frames
                anchor = _anchor_point(det.get("bbox"))
                if not anchor:
                    continue
                zname = _zone_hit(anchor[0], anchor[1], img_w, img_h, zones)
                if not zname:
                    continue
                k = (str(track), zname)
                seen_keys.add(k)
                st = cam.get(k)
                if not st:
                    # entered_at se fija en el PRIMER frame dentro de la zona
                    # (permanencia real); INSIDE solo lo confirma al editor.
                    st = {"state": ST_OUTSIDE, "frames_in": 0,
                          "entered_at": None, "last_seen": now}
                    cam[k] = st
                st["last_seen"] = now
                s = st["state"]
                if s in (ST_OUTSIDE, ST_EXITING):
                    # (re)ingreso: en EXITING dentro de grace → restaurar INSIDE
                    if s == ST_EXITING and st.get("entered_at") is not None:
                        st["state"] = ST_INSIDE
                        st["frames_in"] = 0
                        st.pop("exit_at", None)
                    else:
                        st["state"] = ST_ENTERING
                        st["frames_in"] = 1
                        st["entered_at"] = now
                elif s == ST_ENTERING:
                    st["frames_in"] += 1
                    if st["frames_in"] >= FRAMES_TO_CONFIRM:
                        st["state"] = ST_INSIDE
                        if st.get("entered_at") is None:
                            st["entered_at"] = now
                # ST_INSIDE: acumula via entered_at (dwell = now-entered_at)
            # Tracks que estaban en zona y NO aparecen en este frame:
            for k, st in cam.items():
                if k in seen_keys:
                    continue
                if st["state"] == ST_INSIDE:
                    # salió de vista/zona → grace antes de reset
                    if st.get("exit_at") is None:
                        st["exit_at"] = now
                    st["state"] = ST_EXITING
                elif st["state"] == ST_ENTERING:
                    # racha de confirmación rota → volver a empezar
                    st["state"] = ST_OUTSIDE
                    st["frames_in"] = 0
                    st["entered_at"] = None
            # Grace vencida → OUTSIDE y limpiar
            for k, st in cam.items():
                if st["state"] == ST_EXITING and st.get("exit_at") is not None:
                    if (now - st["exit_at"]) > EXIT_GRACE_S:
                        st["state"] = ST_OUTSIDE
                        st["frames_in"] = 0
                        st["entered_at"] = None
                        st.pop("exit_at", None)
    except Exception:
        pass
    _maybe_persist(now)


# ── Consulta ──────────────────────────────────────────────────────────────

def get_active_dwells(cam_id: str, now: Optional[float] = None) -> List[dict]:
    """Dwells INSIDE de una cámara: [{track_id, zone, dwell_s, state}].

    `now` opcional para tests/evaluaciones con reloj controlado.
    """
    out: List[dict] = []
    try:
        _now = time.time() if now is None else now
        with _lock:
            cam = _states.get(str(cam_id)) or {}
            for (tid, zname), st in cam.items():
                if st["state"] != ST_INSIDE or st.get("entered_at") is None:
                    continue
                out.append({
                    "track_id": tid,
                    "zone": zname,
                    "dwell_s": max(0.0, _now - st["entered_at"]),
                    "state": st["state"],
                })
    except Exception:
        return []
    return out


# ── Evaluación de reglas ──────────────────────────────────────────────────

def eval_rule(rule: dict, cam_id: str, now: float, after_hours: bool) -> Optional[dict]:
    """Evalúa UNA regla contra el estado dwell de la cámara.

    Retorna None si no dispara, o:
      {"frase": str, "dwell_s": float, "severity": str, "rule_id": str}
    """
    try:
        if not isinstance(rule, dict) or not rule.get("active", True):
            return None
        sched = rule.get("schedule", "siempre")
        if sched == "fuera_de" and not after_hours:
            return None
        trig = rule.get("trigger")
        target_zone = str((rule.get("target") or {}).get("zone") or "").strip()
        cond = rule.get("condition") or {}
        sev = rule.get("severity", "media")
        rid = rule.get("id") or "rul_?"
        name = str(rule.get("name") or target_zone or "regla")

        if trig == "zone_presence":
            need_dwell = float(cond.get("dwell_s", 0) or 0)
            with _lock:
                cam = _states.get(str(cam_id)) or {}
                for (tid, zname), st in cam.items():
                    if zname != target_zone:
                        continue
                    if st["state"] != ST_INSIDE or st.get("entered_at") is None:
                        continue
                    d = max(0.0, now - st["entered_at"])
                    if d >= need_dwell:
                        return {
                            "frase": f"persona en zona '{target_zone}'" +
                                     (f" (permanencia {d:.0f}s)" if need_dwell > 0 else ""),
                            "dwell_s": d,
                            "severity": sev,
                            "rule_id": rid,
                            "rule_name": name,
                            "track_id": tid,
                        }
            return None

        if trig == "count":
            min_count = int(cond.get("min_count", 1) or 1)
            with _lock:
                cam = _states.get(str(cam_id)) or {}
                inside = [((tid, zname), st) for (tid, zname), st in cam.items()
                          if st["state"] == ST_INSIDE and st.get("entered_at") is not None]
                if target_zone:
                    inside = [x for x in inside if x[0][1] == target_zone]
                if len(inside) >= max(1, min_count):
                    d = max(0.0, now - min(st["entered_at"] for _, st in inside))
                    zone_txt = f" en zona '{target_zone}'" if target_zone else ""
                    return {
                        "frase": f"{len(inside)} persona(s){zone_txt} simultánea(s)",
                        "dwell_s": d,
                        "severity": sev,
                        "rule_id": rid,
                        "rule_name": name,
                        "track_id": None,
                    }
            return None

        if trig == "proximity":
            # TODO (pieza futura): proximidad persona↔objeto fijo del mapa de
            # objetos (distancia euclidiana <0.15 en coords normalizadas).
            return None

        return None
    except Exception:
        return None


# ── Persistencia ───────────────────────────────────────────────────────────

def _serialize() -> dict:
    data = {}
    for cam_id, cam in _states.items():
        data[cam_id] = {}
        for (tid, zname), st in cam.items():
            data[cam_id][f"{tid}||{zname}"] = {
                "state": st.get("state"),
                "frames_in": st.get("frames_in", 0),
                "entered_at": st.get("entered_at"),
                "last_seen": st.get("last_seen"),
                "exit_at": st.get("exit_at"),
            }
    return data


def _deserialize(data: dict) -> None:
    for cam_id, cam in (data or {}).items():
        if not isinstance(cam, dict):
            continue
        loaded = {}
        for k, st in cam.items():
            if not isinstance(st, dict) or "||" not in str(k):
                continue
            tid, zname = str(k).split("||", 1)
            loaded[(tid, zname)] = {
                "state": st.get("state") or ST_OUTSIDE,
                "frames_in": int(st.get("frames_in") or 0),
                "entered_at": st.get("entered_at"),
                "last_seen": st.get("last_seen") or time.time(),
                "exit_at": st.get("exit_at"),
            }
        if loaded:
            _states[str(cam_id)] = loaded


def _prune(now: float) -> int:
    """Elimina tracks con last_seen > 30 min. Devuelve cantidad podada."""
    pruned = 0
    for cam_id in list(_states.keys()):
        cam = _states.get(cam_id) or {}
        stale = [k for k, st in cam.items()
                 if st.get("last_seen") is None
                 or (now - st["last_seen"]) > PRUNE_AFTER_S]
        for k in stale:
            cam.pop(k, None)
            pruned += 1
        if not cam:
            _states.pop(cam_id, None)
    return pruned


def _flush_locked(now: float) -> None:
    global _last_persist
    os.makedirs(_RUNTIME_DIR, exist_ok=True)
    tmp = _STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(_serialize(), f, ensure_ascii=False)
    os.replace(tmp, _STATE_FILE)
    _last_persist = now


def _maybe_persist(now: float, force: bool = False) -> None:
    """Persiste cada 30s (llamado desde process_frame). Never-block: en fallo, silencio."""
    global _last_persist
    try:
        if not force and (now - _last_persist) < PERSIST_EVERY_S:
            return
        if not _persist_lock.acquire(blocking=False):
            return  # otro thread ya está persistiendo
        try:
            with _lock:
                _prune(now)
                _flush_locked(now)
        finally:
            _persist_lock.release()
    except Exception:
        pass


def load_states() -> int:
    """Carga estados persistidos al importar. Devuelve nº de tracks cargados."""
    global _last_persist
    try:
        if not os.path.exists(_STATE_FILE):
            return 0
        with open(_STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        with _lock:
            _states.clear()
            _deserialize(data)
            n = sum(len(c) for c in _states.values())
            _last_persist = time.time()
        return n
    except Exception:
        return 0


def persist_now() -> bool:
    """Flush síncrono inmediato (para shutdown hooks / tests)."""
    try:
        _maybe_persist(time.time(), force=True)
        return True
    except Exception:
        return False


# Cargar al importar: un reinicio del worker no pierde dwells en curso
# (entered_at se conserva; dwell sigue acumulando desde el instante real
# de entrada, no desde el reinicio).
load_states()
