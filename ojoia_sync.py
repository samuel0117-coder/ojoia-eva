#!/usr/bin/env python3
"""
ojoia_sync.py — Sincronización multi-nodo del megapanel vía Firebase Firestore.

Arquitectura (sin Cloud Functions, firebase-admin en cada nodo):
  - Cada nodo hace PUSH de su estado a /nodes/{node_id}/status cada N segundos.
  - Cada nodo hace POLLING de /control/{node_id}/{cmd_id} y ejecuta los comandos
    encolados por el panel web; escribe el resultado en /control/{node_id}/{cmd_id}.
  - El panel web (SPA en Firebase Hosting) lee /nodes/* y escribe /control/*.

Requisitos:
  - firebase_admin instalado en el venv.
  - Service account key: /opt/ojoia/config/firebase-key.json
    (o FIREBASE_CREDENTIAL env; fuente preferida: mismos keys existentes)
  - OJOIA_ENV / NODE_ID para identificar el nodo.
"""

import asyncio
import json
import os
import time
from datetime import datetime
from pathlib import Path

try:
    import firebase_admin
    from firebase_admin import credentials, firestore
    _FIREBASE_OK = True
except Exception:
    _FIREBASE_OK = False

# Ruta del service account: buscar en varias ubicaciones
_CRED_CANDIDATES = [
    "/opt/ojoia/config/firebase-key.json",
    "/home/sam/ai_system/firebase-key.json",
    "/home/sam/ojoia-eva/ai_system/firebase-key.json",
]


def _pick_credential() -> Path:
    env = os.environ.get("FIREBASE_CREDENTIAL", "").strip()
    if env and Path(env).exists():
        return Path(env)
    for c in _CRED_CANDIDATES:
        if Path(c).exists():
            return Path(c)
    return None


class OjoiaSync:
    """Push de estado + polling de comandos contra Firestore."""

    def __init__(self, node_id: str, fb_project: str | None = None,
                 interval: float = 5.0, cred: Path | None = None):
        self.node_id = node_id
        self.interval = interval
        self.db = None
        self._heartbeat = 0.0
        if not _FIREBASE_OK:
            print("[ojoia_sync] firebase_admin no disponible; sincronización desactivada")
            return
        cred_path = cred or _pick_credential()
        if not cred_path:
            print("[ojoia_sync] No se encontró firebase-key.json; sincronización desactivada")
            return
        try:
            cred = credentials.Certificate(str(cred_path))
            opts = {"projectId": fb_project or "ojoia-67216"}
            if not firebase_admin._apps:
                firebase_admin.initialize_app(cred, opts)
            else:
                firebase_admin.get_app()
            self.db = firestore.client()
            print(f"[ojoia_sync] Conectado a Firestore (proyecto {opts['projectId']}), nodo={node_id}")
        except Exception as e:
            print(f"[ojoia_sync] Error inicializando Firestore: {e}")
            self.db = None

    @property
    def enabled(self) -> bool:
        return self.db is not None

    def push_status(self, status: dict) -> None:
        """Escribe el snapshot de estado del nodo en Firestore."""
        if not self.enabled:
            return
        try:
            doc = self.db.collection("nodes").document(self.node_id)
            doc.set({
                "node_id": self.node_id,
                "online": True,
                "hostname": status.get("hostname", ""),
                "updated_at": firestore.SERVER_TIMESTAMP,
                "status": status,
            })
        except Exception as e:
            print(f"[ojoia_sync] ERROR push status: {e}")

    def push_billing(self, billing: dict) -> None:
        """Sincroniza datos de billing (stats, clients, config) a Firestore.

        Estructura en Firestore:
          /billing/global/stats/{window}  -> stats agregados
          /billing/global/clients        -> lista de clientes con uso
          /billing/global/config         -> precios y planes
          /billing/global/reqlog/{id}    -> requests recientes (max 200)
        """
        if not self.enabled:
            return
        try:
            base = self.db.collection("billing").document("global")
            # Stats por ventana
            if "stats" in billing:
                for window_h, sdata in billing["stats"].items():
                    base.collection("stats").document(str(window_h)).set({
                        "window_h": window_h,
                        "data": sdata,
                        "updated_at": firestore.SERVER_TIMESTAMP,
                    })
            # Clientes
            if "clients" in billing:
                base.collection("clients_meta").document("current").set({
                    "clients": billing["clients"],
                    "count": len(billing["clients"]),
                    "updated_at": firestore.SERVER_TIMESTAMP,
                })
            # Config (precios + planes)
            if "config" in billing:
                base.collection("config").document("current").set({
                    "config": billing["config"],
                    "updated_at": firestore.SERVER_TIMESTAMP,
                })
            # ReqLog (últimos N)
            if "reqlog" in billing:
                for req in billing["reqlog"][:200]:
                    base.collection("reqlog").document(str(req.get("id", ""))).set({
                        **req,
                        "synced_at": firestore.SERVER_TIMESTAMP,
                    })
        except Exception as e:
            print(f"[ojoia_sync] ERROR push billing: {e}")

    def billing_provider(self) -> dict:
        """Retorna un provider de billing listo para pasar a run_loop como billing_provider.

        Uso:
          sync = OjoiaSync(node_id)
          sync.push_billing(sync.billing_provider())  # dentro del loop
        """
        try:
            import sys as _sys
            _sys.path.insert(0, "/opt/ojoia/code")
            from billing import BillingStore
            from billing_log import get_stats, get_requests, get_storage_info
            billing = BillingStore.instance()
            stats_24h = get_stats(24)
            stats_7d = get_stats(168)
            clients = billing.get_all_clients_usage("month")
            config = billing.get_config()
            reqlog = get_requests(limit=200, offset=0)
            return {
                "stats": {"24": stats_24h, "168": stats_7d},
                "clients": clients,
                "config": config,
                "reqlog": reqlog,
            }
        except Exception as e:
            print(f"[ojoia_sync] billing_provider error: {e}")
            return {}

    def set_offline(self) -> None:
        """Marca el nodo como offline (para shutdown limpio)."""
        if not self.enabled:
            return
        try:
            self.db.collection("nodes").document(self.node_id).update({
                "online": False,
                "offline_at": firestore.SERVER_TIMESTAMP,
            })
        except Exception:
            pass

    async def poll_control(self, executor) -> None:
        """Consulta comandos pendientes para este nodo y los ejecuta.

        executor: async callable(svc_id, action) -> dict  (o coroutine)
        """
        if not self.enabled:
            return
        try:
            # Leer comandos pendientes (sin reconocer aún)
            pending = list(
                _sync_query(self.db, "control", self.node_id)
            )
            for doc in pending:
                data = doc.to_dict() or {}
                svc_id = data.get("service_id", "")
                action = data.get("action", "")
                if not svc_id or not action:
                    await _sync_delete(self.db, "control", self.node_id, doc.id)
                    continue
                # Ejecutar
                result = {}
                try:
                    if asyncio.iscoroutinefunction(executor):
                        result = await executor(svc_id, action)
                    else:
                        result = executor(svc_id, action)
                except Exception as e:
                    result = {"error": str(e)}
                # Escribir resultado
                await _sync_set(self.db, "control", self.node_id, doc.id, {
                    "status": "done",
                    "result": result,
                    "executed_at": firestore.SERVER_TIMESTAMP,
                })
        except Exception as e:
            print(f"[ojoia_sync] ERROR poll control: {e}")

    async def run_loop(self, status_provider, executor, shutdown=None,
                       billing_provider=None, billing_interval: float = 60.0):
        """Bucle principal: push status + poll control cada `interval` s.

        Si billing_provider está definido, sincroniza billing cada
        `billing_interval` segundos (default 60s — billing no necesita
        frecuencia alta).
        """
        if not self.enabled:
            print("[ojoia_sync] Bucle no iniciado (deshabilitado)")
            return
        last_billing = 0.0
        while True:
            try:
                self.push_status(status_provider())
            except Exception as e:
                print(f"[ojoia_sync] push error: {e}")
            try:
                await self.poll_control(executor)
            except Exception as e:
                print(f"[ojoia_sync] poll error: {e}")
            # Billing sync (menos frecuente)
            now = time.time()
            if billing_provider and (now - last_billing) >= billing_interval:
                try:
                    billing_data = billing_provider()
                    if billing_data:
                        self.push_billing(billing_data)
                        last_billing = now
                except Exception as e:
                    print(f"[ojoia_sync] billing sync error: {e}")
            # Semáforo de parada
            if shutdown and shutdown.is_set():
                break
            await asyncio.sleep(self.interval)


# ── Helpers síncronos reutilizables (por estabilidad con el event loop) ──

def _sync_query(db, col1, doc1):
    try:
        return db.collection(col1).document(doc1).collection("cmds").stream()
    except Exception:
        return []


def _sync_delete(db, col1, doc1, doc2):
    return _run_future(db.collection(col1).document(doc1).collection("cmds").document(doc2).delete())


def _sync_set(db, col1, doc1, doc2, data):
    return _run_future(db.collection(col1).document(doc1).collection("cmds").document(doc2).set(data))


def _run_future(fut):
    """Ejecuta una operación Firestore (que devuelve un future) sin bloquear el loop."""
    import concurrent.futures
    loop = asyncio.get_running_loop()
    return loop.run_in_executor(None, lambda: fut.result())


# ── Función de arranque sencilla ──

def start_status_task(node_id: str, status_provider, shutdown=None) -> "OjoiaSync":
    """Arranca un hilo/loop de push de estado en background (conveniente para no async apps)."""
    sync = OjoiaSync(node_id)
    if not sync.enabled:
        return sync

    import threading

    def _worker():
        asyncio.run(_background_loop(sync, status_provider, shutdown))

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    return sync


async def _background_loop(sync, status_provider, shutdown):
    while True:
        try:
            sync.push_status(status_provider())
        except Exception:
            pass
        if shutdown and shutdown.is_set():
            break
        await asyncio.sleep(sync.interval)


if __name__ == "__main__":
    print("ojoia_sync importable. Node:", os.environ.get("NODE_ID", "?"))