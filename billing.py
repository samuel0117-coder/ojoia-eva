#!/usr/bin/env python3
"""
billing.py — Sistema de autenticación, uso y facturación para OjoIA.

Funcionalidades:
  - API keys por cliente (generación, validación, revocación)
  - Conteo de tokens por request (prompt + completion)
  - Rate limiting por cliente (sliding window en Redis)
  - Cálculo de costos por modelo (precios configurables)
  - Agregación de uso por día/mes/cliente

Almacenamiento: Redis (db 0) con clave 'ojoia_billing:*'

Integración:
  - service_bus.py: middleware de auth + rate limit + tracking
  - megapanel.py: endpoints /api/usage, /admin/keys

Precios por modelo (por 1M tokens, USD):
  - qwen7b:  $0.50  (rápido, bajo costo)
  - qwen9b:  $2.00  (mejor calidad, más VRAM)
  - qwen35b: $10.00 (máxima calidad, contexto largo)
  - whisper: $0.10  (audio, por minuto)
  - yolo:    $0.05  (visión, por imagen)

Planes:
  - free: 1M tokens/mes
  - dev: 10M tokens/mes
  - pro: 100M tokens/mes
  - enterprise: ilimitado
"""
import json
import os
import secrets
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

try:
    import redis
    HAS_REDIS = True
except ImportError:
    HAS_REDIS = False

REDIS_URL = os.environ.get(
    "REDIS_URL",
    "redis://:hq1V4pQr1c99AWYYAIGBnCu7695jL75@127.0.0.1:6379/0",
)

# Precios por 1M tokens (USD)
MODEL_PRICES = {
    "qwen7b": {"input": 0.30, "output": 0.50, "unit": "tokens"},
    "qwen9b": {"input": 1.50, "output": 2.00, "unit": "tokens"},
    "qwen35b": {"input": 8.00, "output": 10.00, "unit": "tokens"},
    "whisper": {"input": 0.10, "output": 0.10, "unit": "minutes"},
    "yolo":    {"input": 0.05, "output": 0.05, "unit": "images"},
}

# Planes: tokens mensuales incluidos
PLANS = {
    "free":       {"tokens_quota": 1_000_000,    "rpm": 60,  "name": "Free"},
    "dev":        {"tokens_quota": 10_000_000,   "rpm": 300, "name": "Developer"},
    "pro":        {"tokens_quota": 100_000_000,  "rpm": 1000,"name": "Pro"},
    "enterprise": {"tokens_quota": 1_000_000_000, "rpm": 5000,"name": "Enterprise"},
}

REDIS_PREFIX = "ojoia_billing"


class BillingStore:
    """Cliente Redis para billing. Singleton."""

    _instance: Optional["BillingStore"] = None

    def __init__(self):
        if not HAS_REDIS:
            raise RuntimeError("redis module not installed")
        self.r = redis.Redis.from_url(REDIS_URL, decode_responses=True)

    @classmethod
    def instance(cls) -> "BillingStore":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    # ── API keys ─────────────────────────────────────────────────────────────

    def create_key(self, client_id: str, label: str = "",
                   plan: str = "free") -> dict:
        """Genera una API key para un cliente. Retorna {id, key, client_id, plan}."""
        if plan not in PLANS:
            plan = "free"
        kid = f"key_{secrets.token_hex(8)}"
        # Prefijo 'ojoia_live_' para identificar tokens externos
        raw = secrets.token_urlsafe(32)
        full_key = f"ojoia_live_{raw}"
        record = {
            "id": kid,
            "client_id": client_id,
            "label": label,
            "plan": plan,
            "created_at": int(time.time()),
            "revoked": False,
        }
        self.r.set(f"{REDIS_PREFIX}:apikey:{full_key}", json.dumps(record))
        self.r.sadd(f"{REDIS_PREFIX}:client:{client_id}:keys", full_key)
        return {"id": kid, "key": full_key, "client_id": client_id,
                "plan": plan, "label": label, "created_at": record["created_at"]}

    def validate_key(self, api_key: str) -> Optional[dict]:
        """Valida una API key. Retorna el registro o None."""
        if not api_key or not api_key.startswith("ojoia_live_"):
            return None
        raw = self.r.get(f"{REDIS_PREFIX}:apikey:{api_key}")
        if not raw:
            return None
        try:
            rec = json.loads(raw)
            if rec.get("revoked"):
                return None
            return rec
        except json.JSONDecodeError:
            return None

    def revoke_key(self, api_key: str) -> bool:
        """Revoca una API key."""
        rec = self.validate_key(api_key)
        if not rec:
            return False
        rec["revoked"] = True
        rec["revoked_at"] = int(time.time())
        self.r.set(f"{REDIS_PREFIX}:apikey:{api_key}", json.dumps(rec))
        return True

    def list_keys(self, client_id: Optional[str] = None) -> list:
        """Lista todas las API keys (o las de un cliente)."""
        out = []
        pattern = f"{REDIS_PREFIX}:apikey:*"
        for k in self.r.scan_iter(match=pattern):
            raw = self.r.get(k)
            if not raw:
                continue
            try:
                rec = json.loads(raw)
                # Ocultar el secret completo, solo primeros/últimos chars
                api_key = k.split(":", 2)[2]
                masked = api_key[:14] + "..." + api_key[-4:]
                rec["key_masked"] = masked
                rec["key"] = api_key  # full key, solo para uso admin (megapanel)
                rec.pop("id", None)
                if client_id is None or rec.get("client_id") == client_id:
                    out.append(rec)
            except json.JSONDecodeError:
                continue
        return sorted(out, key=lambda x: x.get("created_at", 0), reverse=True)

    # ── Token tracking ───────────────────────────────────────────────────────

    def track_usage(self, client_id: str, model: str,
                    prompt_tokens: int = 0, completion_tokens: int = 0,
                    request_id: str = "") -> dict:
        """Registra el uso de tokens de un request."""
        now = int(time.time())
        day = datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y-%m-%d")
        month = datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y-%m")

        # Acumuladores atómicos
        pipe = self.r.pipeline()
        # Uso total por cliente (tokens globales)
        pipe.hincrby(f"{REDIS_PREFIX}:usage:{client_id}:total", "tokens", prompt_tokens + completion_tokens)
        pipe.hincrby(f"{REDIS_PREFIX}:usage:{client_id}:total", "prompt_tokens", prompt_tokens)
        pipe.hincrby(f"{REDIS_PREFIX}:usage:{client_id}:total", "completion_tokens", completion_tokens)
        pipe.hincrby(f"{REDIS_PREFIX}:usage:{client_id}:total", "requests", 1)
        # Por modelo
        pipe.hincrby(f"{REDIS_PREFIX}:usage:{client_id}:model:{model}", "tokens", prompt_tokens + completion_tokens)
        pipe.hincrby(f"{REDIS_PREFIX}:usage:{client_id}:model:{model}", "requests", 1)
        # Por día
        pipe.hincrby(f"{REDIS_PREFIX}:usage:{client_id}:day:{day}", "tokens", prompt_tokens + completion_tokens)
        pipe.hincrby(f"{REDIS_PREFIX}:usage:{client_id}:day:{day}", "requests", 1)
        # Por mes (para quota)
        pipe.hincrby(f"{REDIS_PREFIX}:usage:{client_id}:month:{month}", "tokens", prompt_tokens + completion_tokens)
        pipe.expire(f"{REDIS_PREFIX}:usage:{client_id}:day:{day}", 60 * 60 * 24 * 35)  # 35 días
        pipe.expire(f"{REDIS_PREFIX}:usage:{client_id}:month:{month}", 60 * 60 * 24 * 35)
        pipe.execute()

        # Calcular costo
        price = MODEL_PRICES.get(model, {"input": 0.0, "output": 0.0})
        cost = (prompt_tokens / 1_000_000 * price["input"] +
                completion_tokens / 1_000_000 * price["output"])
        self.r.hincrbyfloat(f"{REDIS_PREFIX}:cost:{client_id}:total", "usd", cost)
        self.r.hincrbyfloat(f"{REDIS_PREFIX}:cost:{client_id}:month:{month}", "usd", cost)

        return {
            "client_id": client_id,
            "model": model,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "cost_usd": round(cost, 6),
            "ts": now,
        }

    # ── Rate limiting (sliding window) ───────────────────────────────────────

    def check_rate_limit(self, client_id: str, plan: str = "free",
                         window_s: int = 60) -> tuple[bool, int, int]:
        """Verifica el rate limit. Retorna (allowed, remaining, reset_s)."""
        rpm = PLANS.get(plan, PLANS["free"])["rpm"]
        key = f"{REDIS_PREFIX}:rate:{client_id}"
        now = int(time.time() * 1000)
        cutoff = now - window_s * 1000

        pipe = self.r.pipeline()
        # Eliminar entradas fuera de la ventana
        pipe.zremrangebyscore(key, 0, cutoff)
        # Contar entradas actuales
        pipe.zcard(key)
        # Agregar marca de tiempo actual
        pipe.zadd(key, {f"{now}-{secrets.token_hex(4)}": now})
        # Expirar la clave
        pipe.expire(key, window_s + 5)
        results = pipe.execute()
        count = results[1]  # después de zadd, incluye el nuevo

        if count > rpm:
            # Remover el que acabamos de agregar (fue rechazado)
            self.r.zremrangebyrank(key, -1, -1)
            return False, 0, window_s
        remaining = max(0, rpm - count)
        return True, remaining, window_s

    # ── Quota ────────────────────────────────────────────────────────────────

    def get_quota_status(self, client_id: str, plan: str = "free") -> dict:
        """Retorna el estado de cuota del cliente."""
        month = datetime.now(tz=timezone.utc).strftime("%Y-%m")
        tokens_used = int(self.r.hget(
            f"{REDIS_PREFIX}:usage:{client_id}:month:{month}", "tokens") or 0)
        quota = PLANS.get(plan, PLANS["free"])["tokens_quota"]
        pct = (tokens_used / quota * 100) if quota > 0 else 0
        return {
            "client_id": client_id,
            "plan": plan,
            "tokens_used": tokens_used,
            "tokens_quota": quota,
            "tokens_remaining": max(0, quota - tokens_used),
            "pct_used": round(pct, 2),
            "month": month,
        }

    def check_quota(self, client_id: str, plan: str = "free") -> bool:
        """Verifica si el cliente tiene quota disponible."""
        status = self.get_quota_status(client_id, plan)
        if status["tokens_remaining"] <= 0 and plan != "enterprise":
            return False
        return True

    # ── Usage reports ────────────────────────────────────────────────────────

    def get_client_usage(self, client_id: str,
                         period: str = "month") -> dict:
        """Retorna el uso detallado de un cliente."""
        now = int(time.time())
        if period == "day":
            day = datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y-%m-%d")
            tokens = int(self.r.hget(
                f"{REDIS_PREFIX}:usage:{client_id}:day:{day}", "tokens") or 0)
            requests = int(self.r.hget(
                f"{REDIS_PREFIX}:usage:{client_id}:day:{day}", "requests") or 0)
            cost = float(self.r.hget(
                f"{REDIS_PREFIX}:cost:{client_id}:month", "usd") or 0)
        else:  # month
            month = datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y-%m")
            tokens = int(self.r.hget(
                f"{REDIS_PREFIX}:usage:{client_id}:month:{month}", "tokens") or 0)
            requests = int(self.r.hget(
                f"{REDIS_PREFIX}:usage:{client_id}:month:{month}", "requests") or 0)
            cost = float(self.r.hget(
                f"{REDIS_PREFIX}:cost:{client_id}:month:{month}", "usd") or 0)

        # Desglose por modelo
        models = {}
        for key in self.r.scan_iter(match=f"{REDIS_PREFIX}:usage:{client_id}:model:*"):
            model = key.split(":")[-1]
            t = int(self.r.hget(key, "tokens") or 0)
            r = int(self.r.hget(key, "requests") or 0)
            models[model] = {"tokens": t, "requests": r}

        return {
            "client_id": client_id,
            "period": period,
            "tokens": tokens,
            "requests": requests,
            "cost_usd": round(cost, 4),
            "by_model": models,
        }

    def get_all_clients_usage(self, period: str = "month") -> list:
        """Retorna el uso de todos los clientes."""
        out = []
        # Patrón: ojoia_billing:usage:{client_id}:total
        for key in self.r.scan_iter(match=f"{REDIS_PREFIX}:usage:*:total"):
            parts = key.split(":")
            # parts = ['ojoia_billing', 'usage', client_id, 'total']
            if len(parts) < 4:
                continue
            client_id = parts[2]
            total = self.r.hgetall(key)
            tokens = int(total.get("tokens", 0))
            requests = int(total.get("requests", 0))
            if tokens == 0 and requests == 0:
                continue
            cost = float(self.r.hget(
                f"{REDIS_PREFIX}:cost:{client_id}:total", "usd") or 0)
            # Desglose por modelo
            by_model = {}
            for mk in self.r.scan_iter(match=f"{REDIS_PREFIX}:usage:{client_id}:model:*"):
                model = mk.split(":")[-1]
                mt = int(self.r.hget(mk, "tokens") or 0)
                mr = int(self.r.hget(mk, "requests") or 0)
                if mt or mr:
                    by_model[model] = {"tokens": mt, "requests": mr}
            out.append({
                "client_id": client_id,
                "tokens": tokens,
                "requests": requests,
                "cost_usd": round(cost, 4),
                "usage": {"by_model": by_model},
            })
        return sorted(out, key=lambda x: x["tokens"], reverse=True)


def extract_usage_from_response(model: str, response_body: bytes,
                                content_type: str = "") -> dict:
    """Extrae tokens de una respuesta JSON del backend."""
    if not content_type or "application/json" not in content_type:
        return {"prompt_tokens": 0, "completion_tokens": 0}
    try:
        data = json.loads(response_body)
        # OpenAI format: data["usage"] = {prompt_tokens, completion_tokens, total_tokens}
        if "usage" in data and isinstance(data["usage"], dict):
            return {
                "prompt_tokens": int(data["usage"].get("prompt_tokens", 0)),
                "completion_tokens": int(data["usage"].get("completion_tokens", 0)),
            }
        # llama.cpp format: data["tokens_evaluated"], data["tokens_predicted"]
        if "tokens_evaluated" in data or "tokens_predicted" in data:
            return {
                "prompt_tokens": int(data.get("tokens_evaluated", 0)),
                "completion_tokens": int(data.get("tokens_predicted", 0)),
            }
    except (json.JSONDecodeError, ValueError, TypeError):
        pass
    return {"prompt_tokens": 0, "completion_tokens": 0}


def extract_usage_from_request(model: str, request_body: bytes) -> dict:
    """Estima tokens de input antes de enviar (para pre-check de quota)."""
    if not request_body:
        return {"prompt_tokens": 0}
    try:
        data = json.loads(request_body)
        if "messages" in data and isinstance(data["messages"], list):
            # Estimación grosera: 1 token ≈ 4 chars
            total_chars = sum(len(str(m.get("content", ""))) for m in data["messages"])
            return {"prompt_tokens": total_chars // 4}
    except (json.JSONDecodeError, ValueError):
        pass
    return {"prompt_tokens": 0}
