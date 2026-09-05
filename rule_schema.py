"""
rule_schema.py — PIEZA 1 del sistema de reglas deterministas de OjoIA.
Schema + CRUD de reglas del dueño sobre camera.json `vigilance.rules[]`.

Regla (schema):
{
  "id": "rul_<8hex>",
  "name": str,
  "trigger": "zone_presence" | "proximity" | "count",
  "target": {"zone": str, "object": null},
  "condition": {"person": true, "min_count": 1, "dwell_s": 0},
  "schedule": "fuera_de" | "siempre",
  "severity": "critica" | "alta" | "media" | "baja",
  "cooldown_s": 300,
  "active": true,
  "created_at": epoch,
  "calibrated_at": null
}

Escritura atómica tmp+replace (patrón de camera_zones.py) y lectura
defensiva (devuelve [] ante cualquier malformación).
"""
import json
import os
import secrets
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

STORAGE_ROOT = Path("/home/sam/storage")

VALID_TRIGGERS = ("zone_presence", "proximity", "count")
VALID_SCHEDULES = ("fuera_de", "siempre")
VALID_SEVERITIES = ("critica", "alta", "media", "baja")

# Sin lock global: cada cámara es un archivo distinto y la UI edita por
# cámara; el coste de coordinar cross-proceso supera al riesgo (la última
# escritura gana sobre un solo campo de config).


def _cam_file(user_id: str, camera_id: str) -> Path:
    return STORAGE_ROOT / "users" / user_id / "cameras" / camera_id / "camera.json"


def _load_cam_data(user_id: str, camera_id: str) -> Optional[dict]:
    """Lee camera.json COMPLETO. None si ilegible/ausente."""
    if not user_id or not camera_id:
        return None
    try:
        f = _cam_file(user_id, camera_id)
        if not f.exists():
            return None
        with open(f, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _save_cam_data_atomic(user_id: str, camera_id: str, data: dict) -> bool:
    """Escritura atómica tmp+replace sobre camera.json (mismo directorio)."""
    try:
        f = _cam_file(user_id, camera_id)
        f.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(f.parent), prefix=".camera_", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=2)
            os.replace(tmp, f)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return True
    except Exception:
        return False


# ── Lectura defensiva ────────────────────────────────────────────────────

def get_rules(cam_cfg: Optional[dict]) -> List[dict]:
    """Reglas de una cámara desde un cam_cfg YA CARGADO (no relee disco).

    Defensivo: cualquier malformación → [] (nunca lanza). Solo devuelve
    reglas activas con id válido NO — devuelve todas las válidas; filtrar
    por `active` es responsabilidad del llamador (permite listar en UI).
    """
    if not isinstance(cam_cfg, dict):
        return []
    vig = cam_cfg.get("vigilance")
    if not isinstance(vig, dict):
        return []
    rules = vig.get("rules")
    if not isinstance(rules, list):
        return []
    out = []
    for r in rules:
        if isinstance(r, dict) and r.get("id") and r.get("trigger"):
            out.append(r)
    return out


def get_active_rules(cam_cfg: Optional[dict]) -> List[dict]:
    """Subconjunto activo (lo que evalúa dwell_engine en process_grid)."""
    return [r for r in get_rules(cam_cfg) if r.get("active", True)]


def get_rules_for_camera(user_id: str, camera_id: str) -> List[dict]:
    """Reglas activas leyendo camera.json CRUDO (bypass del normalizador).

    IMPORTANTE: eva.camera_builder.normalize_camera_vigilance_config
    reconstruye config['vigilance'] SIN la key 'rules' → cam_cfg normalizado
    nunca la lleva. Para evaluar reglas reales hay que leer el disco.
    """
    try:
        data = _load_cam_data(user_id, camera_id)
        if data is None:
            return []
        return [r for r in get_rules(data) if r.get("active", True)]
    except Exception:
        return []


# ── Validación ────────────────────────────────────────────────────────────

def validate_rule(rule: dict, zones: Optional[List[dict]] = None) -> Tuple[bool, str]:
    """Valida forma+valores del schema. Devuelve (ok, mensaje).

    `zones` opcional: lista de zonas de la cámara para validar que
    target.zone exista (por nombre). Si es None, se salta esa verificación
    (permitir calibrar una regla antes de dibujar la zona).
    """
    if not isinstance(rule, dict):
        return False, "la regla debe ser un objeto"
    name = str(rule.get("name", "")).strip()
    if not name:
        return False, "falta 'name'"
    if len(name) > 80:
        return False, "'name' demasiado largo (máx 80)"

    trig = rule.get("trigger")
    if trig not in VALID_TRIGGERS:
        return False, f"'trigger' inválido: debe ser {', '.join(VALID_TRIGGERS)}"

    target = rule.get("target")
    if not isinstance(target, dict):
        return False, "'target' debe ser un objeto {zone, object}"
    zone_name = target.get("zone")
    if not zone_name or not str(zone_name).strip():
        return False, "falta 'target.zone'"
    if zones is not None:
        zone_names = {str(z.get("name", "")).strip() for z in zones if isinstance(z, dict)}
        if str(zone_name).strip() not in zone_names:
            return False, f"la zona '{zone_name}' no existe en esta cámara"
    # target.object: solo relevante para trigger=proximity (stub futuro)
    if trig != "proximity" and target.get("object") not in (None, ""):
        return False, "'target.object' solo se usa en reglas de proximidad"

    cond = rule.get("condition")
    if not isinstance(cond, dict):
        return False, "'condition' debe ser un objeto {person, min_count, dwell_s}"
    try:
        if not bool(cond.get("person", True)):
            return False, "por ahora solo se soportan reglas sobre 'person'"
    except Exception:
        pass
    try:
        mc = int(cond.get("min_count", 1) or 1)
        if trig == "count" and mc < 1:
            return False, "'condition.min_count' debe ser >= 1 en reglas count"
        if mc < 0 or mc > 99:
            return False, "'condition.min_count' fuera de rango (0-99)"
    except (TypeError, ValueError):
        return False, "'condition.min_count' debe ser un entero"
    try:
        dw = float(cond.get("dwell_s", 0) or 0)
        if trig == "zone_presence" and dw < 0:
            return False, "'condition.dwell_s' no puede ser negativo"
        if dw > 3600:
            return False, "'condition.dwell_s' fuera de rango (máx 3600)"
    except (TypeError, ValueError):
        return False, "'condition.dwell_s' debe ser numérico"

    sched = rule.get("schedule")
    if sched not in VALID_SCHEDULES:
        return False, f"'schedule' inválido: debe ser {', '.join(VALID_SCHEDULES)}"

    sev = rule.get("severity")
    if sev not in VALID_SEVERITIES:
        return False, f"'severity' inválida: debe ser {', '.join(VALID_SEVERITIES)}"

    try:
        cd = int(rule.get("cooldown_s", 300) or 300)
        if cd < 10 or cd > 86400:
            return False, "'cooldown_s' fuera de rango (10-86400)"
    except (TypeError, ValueError):
        return False, "'cooldown_s' debe ser un entero"

    if "active" in rule and not isinstance(rule.get("active"), bool):
        return False, "'active' debe ser boolean"

    return True, "ok"


def _new_rule_id() -> str:
    return "rul_" + secrets.token_hex(4)


def normalize_rule(rule: dict) -> dict:
    """Rellena defaults de una regla (para add/update). No valida."""
    r = dict(rule) if isinstance(rule, dict) else {}
    r.setdefault("id", _new_rule_id())
    r.setdefault("name", "")
    r.setdefault("trigger", "zone_presence")
    r.setdefault("target", {"zone": "", "object": None})
    if not isinstance(r["target"], dict):
        r["target"] = {"zone": "", "object": None}
    r.setdefault("condition", {"person": True, "min_count": 1, "dwell_s": 0})
    if not isinstance(r["condition"], dict):
        r["condition"] = {"person": True, "min_count": 1, "dwell_s": 0}
    r["condition"].setdefault("person", True)
    r["condition"].setdefault("min_count", 1)
    r["condition"].setdefault("dwell_s", 0)
    r.setdefault("schedule", "siempre")
    r.setdefault("severity", "media")
    r.setdefault("cooldown_s", 300)
    r.setdefault("active", True)
    r.setdefault("created_at", time.time())
    r.setdefault("calibrated_at", None)
    return r


# ── CRUD atómico sobre camera.json ────────────────────────────────────────

def add_rule(user_id: str, camera_id: str, rule: dict, zones: Optional[List[dict]] = None) -> dict:
    """Agrega una regla validada. Devuelve la regla creada ({} si falla)."""
    r = normalize_rule(rule)
    ok, msg = validate_rule(r, zones)
    if not ok:
        return {"error": msg}
    data = _load_cam_data(user_id, camera_id)
    if data is None:
        return {"error": "camera.json no encontrado o ilegible"}
    vig = data.get("vigilance")
    if not isinstance(vig, dict):
        vig = {}
    rules = vig.get("rules")
    if not isinstance(rules, list):
        rules = []
    if any(x.get("id") == r["id"] for x in rules if isinstance(x, dict)):
        r["id"] = _new_rule_id()
    rules.append(r)
    vig["rules"] = rules
    data["vigilance"] = vig
    if not _save_cam_data_atomic(user_id, camera_id, data):
        return {"error": "no se pudo guardar camera.json"}
    return r


def update_rule(user_id: str, camera_id: str, rule: dict, zones: Optional[List[dict]] = None) -> dict:
    """Actualiza una regla por su 'id' (merge quirúrgico). Devuelve la regla."""
    if not isinstance(rule, dict) or not rule.get("id"):
        return {"error": "falta 'id' de la regla"}
    rid = rule["id"]
    data = _load_cam_data(user_id, camera_id)
    if data is None:
        return {"error": "camera.json no encontrado o ilegible"}
    vig = data.get("vigilance")
    if not isinstance(vig, dict) or not isinstance(vig.get("rules"), list):
        return {"error": "la cámara no tiene reglas"}
    rules = vig["rules"]
    idx = next((i for i, x in enumerate(rules)
                if isinstance(x, dict) and x.get("id") == rid), None)
    if idx is None:
        return {"error": f"regla {rid} no encontrada"}
    merged = dict(rules[idx])
    merged.update({k: v for k, v in rule.items() if k != "created_at"})
    merged["id"] = rid
    merged.setdefault("created_at", time.time())
    merged.setdefault("calibrated_at", None)
    ok, msg = validate_rule(merged, zones)
    if not ok:
        return {"error": msg}
    rules[idx] = merged
    vig["rules"] = rules
    if not _save_cam_data_atomic(user_id, camera_id, data):
        return {"error": "no se pudo guardar camera.json"}
    return merged


def delete_rule(user_id: str, camera_id: str, rule_id: str) -> bool:
    """Elimina una regla por id. True si el guardado fue exitoso."""
    if not rule_id:
        return False
    data = _load_cam_data(user_id, camera_id)
    if data is None:
        return False
    vig = data.get("vigilance")
    if not isinstance(vig, dict) or not isinstance(vig.get("rules"), list):
        return False
    remaining = [r for r in vig["rules"]
                 if not (isinstance(r, dict) and r.get("id") == rule_id)]
    if len(remaining) == len(vig["rules"]):
        return False
    vig["rules"] = remaining
    return _save_cam_data_atomic(user_id, camera_id, data)
