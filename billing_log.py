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
    # 27B (Qwen3.8-27B)
    "qwen38": "qwen38",
    "qwen3.8-27b": "qwen38",
    "qwen3.8-27b-a3b+": "qwen38",
    # 8B (Qwen3-VL-8B — llama.cpp puerto 8019)
    "qwen3vl8b": "qwen3vl8b",
    "qwen3-vl-8b": "qwen3vl8b",
    "qwen3vl": "qwen3vl8b",
    "qwen38-9b": "qwen3vl8b",
    "qwen3.8-9b-distill": "qwen3vl8b",
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

            # Por modelo — extendido: tok/s de generación, latencia p95,
            # respuestas vacías (completion=0 con status 200), tasa de error
            by_model = {}
            for row_m in conn.execute(
                """SELECT model,
                          SUM(prompt_tokens+completion_tokens),
                          SUM(cost_usd),
                          COUNT(*),
                          SUM(CASE WHEN status_code>=400 THEN 1 ELSE 0 END),
                          SUM(CASE WHEN status_code<400
                                    AND COALESCE(completion_tokens,0)=0
                                    AND COALESCE(prompt_tokens,0)>0
                                   THEN 1 ELSE 0 END),
                          SUM(CASE WHEN latency_ms>0 AND completion_tokens>0
                                   THEN completion_tokens*1000.0/latency_ms ELSE 0 END),
                          SUM(CASE WHEN latency_ms>0 AND completion_tokens>0 THEN 1 ELSE 0 END)
                   FROM requests WHERE ts>=? GROUP BY model ORDER BY 2 DESC""",
                (since,),
            ).fetchall():
                n_gen = row_m[7] or 0
                by_model[row_m[0]] = {
                    "tokens": row_m[1] or 0,
                    "cost": round(row_m[2] or 0, 4),
                    "requests": row_m[3],
                    "errors": row_m[4] or 0,
                    "empty_responses": row_m[5] or 0,
                    # tok/s medio ponderado (solo requests que generaron)
                    "gen_tok_s": round(row_m[6] / n_gen, 1) if n_gen else 0,
                }
            # latencia p95 por modelo (percentil aproximado por orden)
            for m in by_model:
                p95 = conn.execute(
                    """SELECT latency_ms FROM requests
                       WHERE ts>=? AND model=? AND latency_ms>0
                       ORDER BY latency_ms DESC LIMIT 1 OFFSET ?""",
                    (since, m, max(0, int(by_model[m]["requests"] * 0.05))),
                ).fetchone()
                by_model[m]["p95_latency_ms"] = int(p95[0]) if p95 else 0

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


def get_alerts() -> list[dict]:
    """Detecta abusos y anomalías en la última hora:
    - client_id con >20 req/min sostenido (ventana 1h, >1200 reqs)
    - client_id con >$5/hora de consumo
    - modelo con tasa de respuestas vacías >20% (>10 reqs)
    Retorna lista de alertas activas (vacía = todo normal).
    """
    with _lock:
        conn = _connect()
        try:
            since = time.time() - 3600
            alerts: list[dict] = []

            # 1) Rate: >1200 reqs/hora por cliente (~20/min sostenido)
            for r in conn.execute(
                """SELECT client_id, COUNT(*), SUM(cost_usd)
                   FROM requests WHERE ts>=? GROUP BY client_id HAVING COUNT(*) > 1200""",
                (since,),
            ).fetchall():
                alerts.append({
                    "type": "rate_abuse",
                    "severity": "high",
                    "client_id": r[0],
                    "requests_1h": r[1],
                    "detail": f"{r[0]}: {r[1]} reqs/hora (>20/min) — posible loop o abuso",
                })

            # 2) Costo: >$5/hora por cliente
            for r in conn.execute(
                """SELECT client_id, COUNT(*), SUM(cost_usd)
                   FROM requests WHERE ts>=? GROUP BY client_id HAVING SUM(cost_usd) > 5.0""",
                (since,),
            ).fetchall():
                alerts.append({
                    "type": "cost_abuse",
                    "severity": "high",
                    "client_id": r[0],
                    "cost_1h": round(r[2] or 0, 2),
                    "detail": f"{r[0]}: ${r[2]:.2f}/hora — consumo anormal",
                })

            # 3) Modelo con muchas respuestas vacías (>20% y >10 reqs)
            for r in conn.execute(
                """SELECT model, COUNT(*),
                          SUM(CASE WHEN status_code<400
                                   AND COALESCE(completion_tokens,0)=0
                                   AND COALESCE(prompt_tokens,0)>0
                              THEN 1 ELSE 0 END)
                   FROM requests WHERE ts>=? GROUP BY model
                   HAVING COUNT(*) > 10 AND SUM(CASE WHEN status_code<400
                                   AND COALESCE(completion_tokens,0)=0
                                   AND COALESCE(prompt_tokens,0)>0
                              THEN 1 ELSE 0 END) > COUNT(*)*0.2""",
                (since,),
            ).fetchall():
                pct = int(100 * r[2] / r[1])
                alerts.append({
                    "type": "model_empty_responses",
                    "severity": "medium",
                    "model": r[0],
                    "empty": r[2], "total": r[1],
                    "detail": f"{r[0]}: {r[2]}/{r[1]} respuestas vacías ({pct}%)",
                })

            return alerts
        finally:
            conn.close()


def get_capacity_report() -> dict:
    """Capacidad teórica y real de tokens/día del sistema, por modelo.
    Basado en throughput medido en producción (billing.db) y ventanas 24h."""
    with _lock:
        conn = _connect()
        try:
            out = {"models": {}}
            for r in conn.execute(
                """SELECT model,
                          AVG(CASE WHEN latency_ms>0 AND completion_tokens>10
                                   THEN completion_tokens*1000.0/latency_ms END),
                          MAX(completion_tokens),
                          COUNT(*)
                   FROM requests GROUP BY model""",
            ).fetchall():
                model, tok_s, max_out, n = r
                if not model or not tok_s:
                    continue
                # Capacidad teórica: generación 24h sostenida al tok/s medido
                # (cota superior; la práctica incluye colas y thinking)
                theo_24h = int(tok_s * 86400)
                out["models"][model] = {
                    "measured_gen_tok_s": round(tok_s, 1),
                    "max_output_seen": max_out or 0,
                    "requests_seen": n,
                    "theoretical_tokens_per_day": theo_24h,
                    "theoretical_Mtok_per_day": round(theo_24h / 1e6, 2),
                }
            return out
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
