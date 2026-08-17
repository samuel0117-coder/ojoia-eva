"""
identity.py — Capa 1 (P2)

Cache persistente de identidad visual de personas entre grids.

Cuando un grid se cierra, cada track_id deja una "huella" en
`users/<uid>/identity_cache.json` con TTL configurable (default 24h).
Cuando arranca un grid nuevo, comparamos cada nueva huella contra las
recientes: si la distancia espacial + el color dominante son consistentes,
reutilizamos el mismo `global_person_id`.

Esto resuelve: "el tracker dice 1 pero realmente son varias más" — porque
los IDs de YOLO se resetean entre grids, pero el ID global se mantiene
mientras la persona esté físicamente en el lugar.

Firma por track = {
  camera_id,
  centroid_xy_first_frame,  # promedio de bbox center de los primeros 2 frames
  bbox_aspect,              # alto / ancho
  dominant_rgb,             # color promedio de torso (centro del bbox)
  first_seen_ts,
  last_seen_ts,
}
"""
import json
import os
import time
import logging
from pathlib import Path
from typing import Dict, List, Any, Optional

logger = logging.getLogger(__name__)
STORAGE_ROOT = Path("/home/sam/storage")
TTL_SECONDS = 24 * 60 * 60  # 24h
SPATIAL_DIST_THRESHOLD_PX = 180  # px - qué tan cerca debe estar del anterior para ser "la misma"
COLOR_DIST_THRESHOLD = 38  # RGB euclidean - qué tan parecido el color


def _cache_path(user_id: str) -> Path:
    return STORAGE_ROOT / "users" / user_id / "identity_cache.json"


def _distance(a: Dict, b: Dict) -> float:
    """Distancia euclídea entre dos firmas."""
    d = 0.0
    ax, ay = a.get("cx", 0), a.get("cy", 0)
    bx, by = b.get("cx", 0), b.get("cy", 0)
    d += (ax - bx) ** 2 + (ay - by) ** 2
    ar, br = a.get("bbox_aspect", 1.0), b.get("bbox_aspect", 1.0)
    d += (ar - br) ** 2 * 100
    return d ** 0.5


def _color_dist(a_rgb, b_rgb) -> float:
    if not a_rgb or not b_rgb or len(a_rgb) != 3 or len(b_rgb) != 3:
        return 255.0
    return ((a_rgb[0]-b_rgb[0])**2 + (a_rgb[1]-b_rgb[1])**2 + (a_rgb[2]-b_rgb[2])**2) ** 0.5


def _read_cache(user_id: str) -> Dict:
    """Lee el cache, poda entradas con TTL vencido."""
    p = _cache_path(user_id)
    if not p.exists():
        return {"version": 1, "next_id": 1, "entries": []}
    try:
        data = json.loads(p.read_text())
    except Exception:
        return {"version": 1, "next_id": 1, "entries": []}
    now = time.time()
    alive = []
    for e in data.get("entries", []):
        if now - e.get("last_seen_ts", 0) <= TTL_SECONDS:
            alive.append(e)
    data["entries"] = alive
    return data


def _write_cache(user_id: str, cache: Dict) -> Dict:
    p = _cache_path(user_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(cache, indent=2, ensure_ascii=False))
    return cache


def _dominant_color_from_frames(track_frames: List[Dict]) -> Optional[List[int]]:
    """Color promedio del bbox-centro a lo largo de los frames."""
    if not track_frames:
        return None
    rs, gs, bs, n = 0, 0, 0, 0
    for tr in track_frames:
        rgb = tr.get("rgb_center")
        if not rgb or len(rgb) != 3:
            continue
        rs += rgb[0]; gs += rgb[1]; bs += rgb[2]; n += 1
    if n == 0:
        return None
    return [int(rs / n), int(gs / n), int(bs / n)]


def match_and_update(user_id: str, camera_id: str, new_tracks: List[Dict]) -> List[Dict]:
    """
    Punto de entrada principal. Llamar al FINAL de process_grid.

    new_tracks: lista de tracks nuevos, cada uno con campos:
      track_id, frames_indices, rgb_per_frame (list of [r,g,b] or None),
      centroid_xy (centro del bbox promedio global)

    Devuelve la MISMA lista pero enriquecida con:
      global_person_id (int),
      match_distance (float),
      matched_to (camera_id, ts del match)
    
    Si no hay match, asigna nuevo global_person_id y guarda en cache.
    """
    cache = _read_cache(user_id)
    now = time.time()
    existing = cache.get("entries", [])
    next_id = int(cache.get("next_id", 1))

    # Para matching: filtrar solo entries de la MISMA cámara y recientes
    candidates = [e for e in existing if e.get("camera_id") == camera_id and (now - e.get("last_seen_ts", 0)) <= TTL_SECONDS]
    
    out = []
    for track in new_tracks:
        cx = track.get("centroid_xy", {}).get("cx", 0)
        cy = track.get("centroid_xy", {}).get("cy", 0)
        ar = track.get("bbox_aspect", 1.0)
        rgb = track.get("dominant_rgb") or _dominant_color_from_frames(track.get("frames_rgb") or [])
        
        match = None
        best_d = 1e9
        for e in candidates:
            # spatial similarity in same camera
            d = _distance({"cx": cx, "cy": cy, "bbox_aspect": ar}, e.get("signature", {}))
            cd = _color_dist(rgb, e.get("signature", {}).get("dominant_rgb"))
            if d < SPATIAL_DIST_THRESHOLD_PX and cd < COLOR_DIST_THRESHOLD:
                # score = d + cd*0.5
                score = d + cd * 1.0
                if score < best_d:
                    best_d = score
                    match = e
        if match is not None:
            # hit
            track_out = dict(track)
            track_out["global_person_id"] = int(match["global_person_id"])
            track_out["match_distance"] = float(best_d)
            track_out["matched_to_camera"] = match["camera_id"]
            track_out["matched_to_ts"] = match.get("last_seen_ts")
            out.append(track_out)
            # refrescar last_seen y contadores
            match["last_seen_ts"] = now
            match["signature"]["cx"] = (match["signature"].get("cx", cx) + cx) / 2  # suavizar
            match["signature"]["cy"] = (match["signature"].get("cy", cy) + cy) / 2
            match["occurrences"] = match.get("occurrences", 1) + 1
        else:
            # miss → nuevo global_id
            new_id = next_id; next_id += 1
            sig = {"cx": cx, "cy": cy, "bbox_aspect": ar, "dominant_rgb": rgb}
            new_entry = {
                "global_person_id": new_id,
                "camera_id": camera_id,
                "global_created_ts": now,
                "last_seen_ts": now,
                "occurrences": 1,
                "signature": sig,
                "yolo_track_id_origin": track.get("track_id", -1)
            }
            existing.append(new_entry)
            track_out = dict(track)
            track_out["global_person_id"] = new_id
            track_out["match_distance"] = -1.0  # nueva, sin match
            track_out["matched_to_camera"] = None
            track_out["matched_to_ts"] = None
            out.append(track_out)
    
    # Persistir
    cache["entries"] = existing
    cache["next_id"] = next_id
    _write_cache(user_id, cache)
    
    # Resumen para devolver al orquestador
    return {
        "tracks": out,
        "global_unique_count": len({t["global_person_id"] for t in out}),
        "new_persons_count": sum(1 for t in out if t.get("match_distance") == -1.0),
        "matched_persons_count": sum(1 for t in out if t.get("match_distance", -1) >= 0),
    }


def global_unique_today(user_id: str, camera_id: str = None) -> Dict:
    """Devuelve {'unique_global_persons': N, 'entries_hoy': [...]}. Para contador en UI."""
    cache = _read_cache(user_id)
    now = time.time()
    start = now - 24 * 3600
    recent = [e for e in cache.get("entries", []) if e.get("last_seen_ts", 0) >= start]
    if camera_id:
        # combina entries globales que aparecieron en esta cámara
        recent = [e for e in recent if e.get("camera_id") == camera_id]
    return {
        "unique_global_persons": len({e["global_person_id"] for e in recent}),
        "entries_count": len(recent),
        "entries": recent[:30],
    }


def purge_old_entries(user_id: str, ttl_seconds: int = None) -> int:
    """Poda entradas vencidas. Llamar desde cron diario."""
    ttl = ttl_seconds or TTL_SECONDS
    cache = _read_cache(user_id)
    before = len(cache.get("entries", []))
    cache["entries"] = [e for e in cache.get("entries", []) if (time.time() - e.get("last_seen_ts", 0)) <= ttl]
    after = len(cache["entries"])
    if after != before:
        _write_cache(user_id, cache)
    return before - after
