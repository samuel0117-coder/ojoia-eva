#!/usr/bin/env python3
"""frame_bus.py — C2: bus de frames intercambiable (Redis Streams / memoria).

Antes, FRAME_QUEUE era un asyncio.Queue in-process: si api_eva se reiniciaba,
los frames en vuelo se perdían y era imposible escalar workers fuera del
proceso API.

Ahora:
- Backend **redis** (default si REDIS_URL configurada): stream `ojoia:frames`
  con consumer group `workers`. At-least-once: el worker hace XACK solo después
  de procesar; un XAUTOCLAIM periódico rescata frames de consumers muertos.
  MAXLEN aproximado evita crecimiento infinito.
- Backend **memory**: comportamiento legacy (asyncio.Queue con drop policy).
  Se usa si Redis no está disponible o INGEST_QUEUE=memory.

API:
    bus = FrameBus()           # decide backend automáticamente
    await bus.start()          # conecta / crea group si redis
    ok = await bus.put(data)   # False si se descartó (cola llena)
    item = await bus.get(name) # (msg_id, dict) | None (timeout/poll)
    await bus.ack(msg_id)      # no-op en memory
    bus.qsize()                # entero aproximado
    bus.stats()                # dict métricas para /health
    await bus.close()

Formato del mensaje: dict con claves str; `img_bytes` es bytes (redis-py lo
maneja nativo como campo bytes del stream).
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import time

logger = logging.getLogger("frame_bus")

STREAM_KEY = "ojoia:frames"
GROUP = "workers"
MAXLEN = 2000          # trimming aproximado del stream
BLOCK_MS = 1000        # poll del XREADGROUP
CLAIM_IDLE_MS = 30000  # rescatar mensajes idle > 30s (worker muerto a mitad)


class FrameBus:
    def __init__(self):
        self.mode = (os.environ.get("INGEST_QUEUE") or "redis").lower()
        self._r = None
        self._mem: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self.drops = 0
        self._last_drop_log = 0.0

    async def start(self):
        if self.mode != "redis":
            logger.info("[C2] FrameBus en modo MEMORIA (INGEST_QUEUE!=redis)")
            return
        url = os.environ.get("REDIS_URL")
        if not url:
            logger.warning("[C2] INGEST_QUEUE=redis pero falta REDIS_URL → fallback memoria")
            self.mode = "memory"
            return
        try:
            import redis.asyncio as aioredis
            self._r = aioredis.from_url(url, socket_timeout=5, socket_connect_timeout=3)
            await self._r.ping()
            # crear consumer group (idempotente)
            try:
                await self._r.xgroup_create(STREAM_KEY, GROUP, id="0", mkstream=True)
                logger.info(f"[C2] FrameBus Redis: group '{GROUP}' creado en {STREAM_KEY}")
            except Exception as e:
                if "BUSYGROUP" in str(e):
                    logger.info(f"[C2] FrameBus Redis: group '{GROUP}' ya existe")
                else:
                    raise
        except Exception as e:
            logger.warning(f"[C2] Redis no disponible ({e}) → fallback memoria")
            self.mode = "memory"
            self._r = None

    async def put(self, data: dict) -> bool:
        """Encola un frame. Devuelve False si se descartó (backpressure)."""
        if self.mode == "redis" and self._r is not None:
            try:
                fields = {}
                for k, v in data.items():
                    if isinstance(v, (bytes, bytearray)):
                        fields[k] = bytes(v)
                    elif isinstance(v, (dict, list)):
                        fields[k] = json.dumps(v, ensure_ascii=False)
                    elif v is None:
                        fields[k] = ""
                    else:
                        fields[k] = str(v)
                await self._r.xadd(STREAM_KEY, fields, maxlen=MAXLEN, approximate=True)
                return True
            except Exception as e:
                self._count_drop(f"redis put error: {e}")
                return False
        # memoria: misma drop policy que antes (nunca bloquear al ESP32)
        try:
            self._mem.put_nowait(data)
            return True
        except asyncio.QueueFull:
            self._count_drop("queue llena")
            return False

    async def get(self, consumer: str, count: int = 1):
        """Obtiene hasta `count` mensajes como [(msg_id, data), ...] (F3).

        Devuelve lista (posiblemente vacía tras ~BLOCK_MS). Cada elemento es
        (msg_id, data). En modo memoria devuelve [(None, data)] máximo 1
        (la cola nativa no soporta peek de N)."""
        if self.mode == "redis" and self._r is not None:
            try:
                resp = await self._r.xreadgroup(
                    GROUP, consumer, {STREAM_KEY: ">"}, count=max(1, count),
                    block=BLOCK_MS)
                if not resp:
                    return []
                out = []
                for _stream, entries in resp:
                    for msg_id, fields in entries:
                        data = {}
                        for k, v in fields.items():
                            kk = k.decode() if isinstance(k, bytes) else k
                            if isinstance(v, bytes) and kk != "img_bytes":
                                try:
                                    v = v.decode()
                                except UnicodeDecodeError:
                                    pass
                            data[kk] = v
                        for jk in ("cam_cfg", "schedule", "vigilance", "yolo_detections", "yolo_classes"):
                            if jk in data and isinstance(data[jk], (bytes, str)):
                                raw = data[jk]
                                try:
                                    if isinstance(raw, bytes):
                                        raw = raw.decode()
                                    if raw.startswith(("{", "[")):
                                        data[jk] = json.loads(raw)
                                except Exception:
                                    pass
                        for ik in ("yolo_count",):
                            if ik in data:
                                try:
                                    data[ik] = int(data[ik])
                                except (TypeError, ValueError):
                                    data[ik] = 0
                        out.append((msg_id, data))
                return out
            except Exception as e:
                logger.warning(f"[C2] redis get error: {e}; reintentando en 2s")
                await asyncio.sleep(2)
                return []
        try:
            data = await asyncio.wait_for(self._mem.get(), timeout=BLOCK_MS / 1000)
            return [(None, data)]  # memory: sin msg_id (ack no-op)
        except asyncio.TimeoutError:
            return []

    async def ack(self, msg_id):
        if msg_id and self.mode == "redis" and self._r is not None:
            try:
                await self._r.xack(STREAM_KEY, GROUP, msg_id)
            except Exception as e:
                logger.warning(f"[C2] ack error: {e}")

    async def rescue_stale(self, consumer: str) -> list:
        """Reclama mensajes de consumers muertos (llamar periódicamente).
        F3 FIX: devuelve [(msg_id, data), ...] para que el caller LOS PROCESE
        (antes solo los reclamaba y quedaban pending bajo el nuevo consumer)."""
        if self.mode != "redis" or self._r is None:
            return []
        out = []
        try:
            res = await self._r.xautoclaim(STREAM_KEY, GROUP, consumer,
                                           min_idle_time=CLAIM_IDLE_MS, start_id="0-0", count=10)
            msgs = res[1] if isinstance(res, (list, tuple)) and len(res) > 1 else []
            for msg_id, fields in msgs:
                logger.warning(f"[C2] mensaje huérfano reclamado: {msg_id}")
                data = {}
                for k, v in fields.items():
                    kk = k.decode() if isinstance(k, bytes) else k
                    if isinstance(v, bytes) and kk != "img_bytes":
                        try:
                            v = v.decode()
                        except UnicodeDecodeError:
                            pass
                    data[kk] = v
                out.append((msg_id, data))
        except Exception as e:
            logger.debug(f"[C2] autoclaim: {e}")
        return out

    def qsize(self) -> int:
        if self.mode == "redis" and self._r is not None:
            try:
                # approximate: no await supported in sync context; devolvemos -1
                # si no hay loop; el /health usa la versión async abajo.
                return -1
            except Exception:
                return -1
        return self._mem.qsize()

    async def stats(self) -> dict:
        base = {"mode": self.mode, "drops": self.drops, "size": self._mem.qsize(),
                "maxsize": self._mem.maxsize}
        if self.mode == "redis" and self._r is not None:
            try:
                length = await self._r.xlen(STREAM_KEY)
                groups = await self._r.xinfo_groups(STREAM_KEY)
                pending = sum(int(g.get("pending", 0)) for g in groups)
                base.update({"size": length, "maxsize": MAXLEN, "pending": pending})
            except Exception:
                pass
        return base

    def _count_drop(self, reason: str):
        self.drops += 1
        now = time.time()
        if now - self._last_drop_log > 30:
            self._last_drop_log = now
            logger.warning(f"[C2] frame descartado ({reason}); total_drops={self.drops}")

    async def close(self):
        if self._r is not None:
            await self._r.aclose()
            self._r = None
