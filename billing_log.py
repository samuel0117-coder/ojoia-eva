#!/usr/bin/env python3
"""billing_log.py — Log de requests con contenido completo en SQLite.

Guarda cada request al service bus:
  - metadata: cliente, modelo, tokens, costo, latency, timestamp, status
  - contenido: prompt completo + respuesta completa
  - rating: voto del cliente (up/down) sobre la calidad de la respuesta

Usado por service_bus.py (escritura) y megapanel.py (lectura/UI).

DB: /home/sam/ojoia-billing-db/billing.db (SQLite, NVMe)
Auto-purge: registros >30 dias (configurable via env BILLING_LOG_RETENTION_DAYS)
"""
import json
import os
import sqlite3
import threading
import time
from pathlib import Path

DB_PATH = os.environ.get(
    "BILLING_LOG_DB",
    "/home/sam/ojoia-billing-db/billing.db",
)
RETENTION_DAYS = int(os.environ.get("BILLING_LOG_RETENTION_DAYS", "30"))
PURGE_INTERVAL_S = int(os.environ.get("BILLING_LOG_PURGE_INTERVAL", "3600"))

# ── Mapeo canónico de modelos ─────────────────────────────────────────────
# Múltiples nombres representan el mismo modelo físico. Cuando llega un
# request, normalizamos a un nombre canónico para que las stats, los
# reportes y la web no tengan duplicados.
#
# Formato: lista de aliases que apuntan al nombre canónico (key).
# - "qwen36-35b-a3b" es el nombre canónico del container qwen-35b (35B)
# - "qwen35" es el alias genérico que los clientes usan
# - "qwen35b" es el nombre viejo del servicio systemd (legacy)
# - "/models/.../Qwen3.6-35B-...gguf" es el path del archivo .gguf que
#   a veces reporta llama.cpp cuando el cliente no envía el header
#   "model" correcto
_MODEL_ALIASES = {
    # 35B (qwen3.6-35b-a3b)
    "qwen36-35b-a3b": "qwen36-35b-a3b",
    "qwen35": "qwen36-35b-a3b",
    "qwen35b": "qwen36-35b-a3b",
    # 7B (qwen-vl-7b)
    "qwen7b": "qwen7b",
    "qwen-vl-7b": "qwen7b",
    "qwen.service": "qwen7b",
    # 9B (qwen-vl-9b)
    "qwen9b": "qwen9b",
    "qwen-vl-9b": "qwen9b",
    "qwen9b.service": "qwen9b",
    "ai-qwen-9b-1": "qwen9b",
}


def normalize_model_name(model: str) -> str:
    """Normaliza un nombre de modelo a su forma canónica.

    Casos:
      - "qwen35"               -> "qwen36-35b-a3b"
      - "qwen35b"              -> "qwen36-35b-a3b"
      - "/models/.../Qwen3.6..." -> "qwen36-35b-a3b"
      - "qwen7b"               -> "qwen7b"
      - "unknown_model"        -> "unknown_model" (sin cambios)
    """
    if not model:
        return model
    m = model.strip()
    # Match exacto
    if m in _MODEL_ALIASES:
        return _MODEL_ALIASES[m]
    # Match por path .gguf (el model de llama.cpp a veces devuelve el path)
    if ".gguf" in m and "qwen3" in m.lower() and "35b" in m.lower():
        return "qwen36-35b-a3b"
    if ".gguf" in m and "qwen3" in m.lower() and "9b" in m.lower():
        return "qwen9b"
    if ".gguf" in m and ("qwen" in m.lower()) and "7b" in m.lower():
        return "qwen7b"
    # Match por substring (case-insensitive)
    ml = m.lower()
    if "qwen3.6" in ml and "35b" in ml:
        return "qwen36-35b-a3b"
    if ml == "qwen35" or ml == "qwen-35b":
        return "qwen36-35b-a3b"
    if ml == "qwen35b" or ml == "qwen-35b":
        return "qwen36-35b-a3b"
    if "qwen-vl-9b" in ml or "qwen9b" in ml:
        return "qwen9b"
    if "qwen-vl-7b" in ml or "qwen7b" in ml:
        return "qwen7b"
    # Sin match: devolver tal cual
    return m


def normalize_legacy_records() -> int:
    """Migra registros existentes con nombres viejos a nombres canónicos.

    Idempotente: si un registro ya tiene el nombre canónico, no hace nada.
    Retorna el número de registros actualizados.
    """
    import re
    updated = 0
    with _lock:
        conn = _connect()
        try:
            cur = conn.execute("SELECT id, model FROM requests")
            rows = cur.fetchall()
            for rid, old_model in rows:
                new_model = normalize_model_name(old_model)
                if new_model != old_model:
                    conn.execute(
                        "UPDATE requests SET model = ? WHERE id = ?",
                        (new_model, rid),
                    )
                    updated += 1
            conn.commit()
            return updated
        finally:
            conn.close()


_lock = threading.Lock()
_last_purge = 0.0


def _connect() -> sqlite3.Connection:
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,
            client_id TEXT NOT NULL,
            model TEXT NOT NULL,
            backend TEXT NOT NULL,
            prompt_tokens INTEGER DEFAULT 0,
            completion_tokens INTEGER DEFAULT 0,
            cost_usd REAL DEFAULT 0,
            latency_ms INTEGER DEFAULT 0,
            status_code INTEGER DEFAULT 0,
            stream INTEGER DEFAULT 0,
            prompt TEXT,
            response TEXT,
            rating INTEGER DEFAULT 0,
            api_key_masked TEXT
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_requests_ts ON requests(ts DESC)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_requests_client ON requests(client_id, ts DESC)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_requests_model ON requests(model, ts DESC)
    """)
    return conn


def log_request(client_id: str, model: str, backend: str,
                prompt_tokens: int = 0, completion_tokens: int = 0,
                cost_usd: float = 0.0, latency_ms: int = 0,
                status_code: int = 200, stream: bool = False,
                prompt: str = "", response: str = "",
                api_key_masked: str = "") -> int:
    """Registra un request. Retorna el id del registro (para rating posterior).

    El nombre del modelo se normaliza automáticamente (qwen35/qwen35b/path → qwen36-35b-a3b).
    """
    canonical_model = normalize_model_name(model)
    with _lock:
        conn = _connect()
        try:
            cur = conn.execute(
                """INSERT INTO requests
                   (ts, client_id, model, backend, prompt_tokens, completion_tokens,
                    cost_usd, latency_ms, status_code, stream, prompt, response,
                    rating, api_key_masked)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (time.time(), client_id, canonical_model, backend,
                 prompt_tokens, completion_tokens, cost_usd, latency_ms,
                 status_code, 1 if stream else 0, prompt, response, 0,
                 api_key_masked),
            )
            conn.commit()
            return cur.lastrowid
        finally:
            conn.close()


def set_rating(request_id: int, rating: int) -> bool:
    """Actualiza el rating de un request (1=up, -1=down, 0=neutro)."""
    with _lock:
        conn = _connect()
        try:
            conn.execute(
                "UPDATE requests SET rating=? WHERE id=?",
                (rating, request_id),
            )
            conn.commit()
            return True
        finally:
            conn.close()


def get_requests(limit: int = 100, offset: int = 0,
                  client_id: str = "", model: str = "",
                  only_errors: bool = False,
                  min_cost: float = 0.0, since_ts: float = 0.0) -> list[dict]:
    """Lista requests con filtros. Retorna lista de dicts."""
    with _lock:
        conn = _connect()
        try:
            q = "SELECT id, ts, client_id, model, backend, prompt_tokens, " \
                "completion_tokens, cost_usd, latency_ms, status_code, " \
                "stream, rating, api_key_masked, " \
                "substr(prompt,1,200) as prompt_preview, " \
                "substr(response,1,200) as response_preview " \
                "FROM requests WHERE 1=1"
            params: list = []
            if client_id:
                q += " AND client_id=?"
                params.append(client_id)
            if model:
                q += " AND model=?"
                params.append(model)
            if only_errors:
                q += " AND status_code >= 400"
            if min_cost > 0:
                q += " AND cost_usd >= ?"
                params.append(min_cost)
            if since_ts > 0:
                q += " AND ts >= ?"
                params.append(since_ts)
            q += " ORDER BY ts DESC LIMIT ? OFFSET ?"
            params.extend([limit, offset])
            rows = conn.execute(q, params).fetchall()
            cols = [d[0] for d in conn.execute(q, params).description]
            return [dict(zip(cols, r)) for r in rows]
        finally:
            conn.close()


def get_request_detail(request_id: int) -> dict | None:
    """Retorna un request completo (con prompt + response completos)."""
    with _lock:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT * FROM requests WHERE id=?", (request_id,)
            ).fetchone()
            if not row:
                return None
            cols = [d[0] for d in conn.execute(
                "SELECT * FROM requests WHERE id=?", (request_id,)
            ).description]
            return dict(zip(cols, row))
        finally:
            conn.close()


def get_stats(hours: int = 24) -> dict:
    """Estadisticas agregadas para el dashboard."""
    with _lock:
        conn = _connect()
        try:
            since = time.time() - hours * 3600
            # Totales
            row = conn.execute(
                """SELECT COUNT(*), COALESCE(SUM(prompt_tokens+completion_tokens),0),
                          COALESCE(SUM(cost_usd),0),
                          COALESCE(AVG(latency_ms),0),
                          SUM(CASE WHEN status_code>=400 THEN 1 ELSE 0 END)
                   FROM requests WHERE ts>=?""",
                (since,),
            ).fetchone()
            total_reqs, total_tokens, total_cost, avg_lat, errors = row

            # Por modelo
            by_model = {}
            for row_m in conn.execute(
                """SELECT model, SUM(prompt_tokens+completion_tokens),
                          SUM(cost_usd), COUNT(*)
                   FROM requests WHERE ts>=? GROUP BY model ORDER BY 2 DESC""",
                (since,),
            ).fetchall():
                by_model[row_m[0]] = {"tokens": row_m[1] or 0,
                                      "cost": round(row_m[2] or 0, 4),
                                      "requests": row_m[3]}

            # Por cliente
            by_client = {}
            for row_c in conn.execute(
                """SELECT client_id, SUM(prompt_tokens+completion_tokens),
                          SUM(cost_usd), COUNT(*)
                   FROM requests WHERE ts>=? GROUP BY client_id ORDER BY 2 DESC""",
                (since,),
            ).fetchall():
                by_client[row_c[0]] = {"tokens": row_c[1] or 0,
                                       "cost": round(row_c[2] or 0, 4),
                                       "requests": row_c[3]}

            # Serie horaria (tokens por hora)
            hourly: list[dict] = []
            for row_h in conn.execute(
                """SELECT CAST(ts/3600 AS INTEGER)*3600,
                          SUM(prompt_tokens+completion_tokens),
                          SUM(cost_usd), COUNT(*)
                   FROM requests WHERE ts>=?
                   GROUP BY 1 ORDER BY 1""",
                (since,),
            ).fetchall():
                hourly.append({"ts": row_h[0], "tokens": row_h[1] or 0,
                               "cost": round(row_h[2] or 0, 4),
                               "requests": row_h[3]})

            # Ratings
            ratings = conn.execute(
                """SELECT SUM(CASE WHEN rating>0 THEN 1 ELSE 0 END),
                          SUM(CASE WHEN rating<0 THEN 1 ELSE 0 END)
                   FROM requests WHERE ts>=?""",
                (since,),
            ).fetchone()
            up_votes = ratings[0] or 0
            down_votes = ratings[1] or 0

            return {
                "hours": hours,
                "total_requests": total_reqs or 0,
                "total_tokens": total_tokens or 0,
                "total_cost": round(total_cost or 0, 4),
                "avg_latency_ms": int(avg_lat or 0),
                "errors": errors or 0,
                "by_model": by_model,
                "by_client": by_client,
                "hourly": hourly,
                "up_votes": up_votes,
                "down_votes": down_votes,
            }
        finally:
            conn.close()


def get_storage_info() -> dict:
    """Info de almacenamiento de la DB."""
    with _lock:
        p = Path(DB_PATH)
        db_size = p.stat().st_size if p.exists() else 0
        wal_size = 0
        wal = p.parent / (p.name + "-wal")
        if wal.exists():
            wal_size = wal.stat().st_size

        # total requests
        conn = _connect()
        try:
            total = conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0]
        finally:
            conn.close()

        # disk free
        disk = os.statvfs(p.parent)
        free_bytes = disk.f_bavail * disk.f_frsize

        return {
            "db_path": DB_PATH,
            "db_size_mb": round((db_size + wal_size) / 1024 / 1024, 2),
            "total_records": total,
            "disk_free_mb": round(free_bytes / 1024 / 1024, 2),
            "retention_days": RETENTION_DAYS,
        }


def purge_old() -> int:
    """Elimina registros > RETENTION_DAYS. Retorna cuantos borro."""
    global _last_purge
    now = time.time()
    if now - _last_purge < PURGE_INTERVAL_S:
        return 0
    _last_purge = now
    cutoff = now - RETENTION_DAYS * 86400
    with _lock:
        conn = _connect()
        try:
            cur = conn.execute("DELETE FROM requests WHERE ts < ?", (cutoff,))
            conn.commit()
            return cur.rowcount
        finally:
            conn.close()


if __name__ == "__main__":
    # test rapido
    rid = log_request("test", "qwen9b", "qwen9b",
                      prompt_tokens=10, completion_tokens=20,
                      prompt="hola", response="mundo", cost_usd=0.001)
    print(f"logged request id={rid}")
    print("stats:", json.dumps(get_stats(24), indent=2))
    print("storage:", json.dumps(get_storage_info(), indent=2))
    set_rating(rid, 1)
    print("detail:", json.dumps(get_request_detail(rid), indent=2)[:500])
