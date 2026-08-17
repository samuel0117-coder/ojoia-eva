# OjoIA — Plan de modernización para producción

Auditoría completa ejecutada el 2026-08-08 sobre el código en `/opt/ojoia/code/`
(rama `master`, `api_eva.py` v7.0, 5530 líneas). Versiones de runtime medidas:
GPU0 Qwen-VL 13.9G/24G, GPU1 Qwen-14B 23.6G/24G (96% VRAM — borde OOM),
disco 96% (20G libres), 68,916 eventos en una sola cámara sin retención,
`api_eva.log` 355 MB sin rotación, 29,359 líneas de error en runtime con
`NameError` vivos (`_save_vigilance_event`, `cam_id`, `_qwen`, `re`,
`business_type`) y 51 respuestas 500 reales en `/api/user/events`.

El colega revisó el informe y corrigió el orden de prioridades: la auditoría
original ponía seguridad después de estabilidad, lo cual está invertido —
auth deshabilitada y RCE sin auth en el megapanel son riesgo de negocio
**hoy**, no "antes de abrir a clientes". Se reordena abajo.

## Confirmado antes de este plan

- Filtro de clases YOLO **sí activo**: `yolo_server.py:241` (`cls != "person"`)
  y `orchestrator.py:679` (`NIGHT_YOLO_CLASSES = [0]`). El bug histórico del
  "mesa/botella dispara alertas" está cerrado en código. Por tanto P0.2
  (retención de alertas) es seguro: el grifo está cerrado, solo limpiar.

## Plan reordenado (este es el orden definitivo de ejecución)

### BLOQUE A — Seguridad (hacer HOY / esta semana, antes de alta de clientes)

A1. **P1.1 Activar auth de usuario.** `AUTH_ENFORCE=True` (api_eva.py:181) y
    cablear `_verify_user_token` como `Depends()` en `/api/*`. Hoy está
    definida pero jamás llamada.
A2. **P1.2 Cerrar el megapanel (RCE sin auth).** `megapanel.py` expone
    `POST /api/control/{service_id}/{action}` → `systemctl` por túnel público
    sin ninguna auth. Verificar CF Access JWT en nginx (19001) o añadir
    Bearer en megapanel.py.
A3. **P1.3 CORS.** `allow_origins=["*"]` + `allow_credentials=True` es
    inválido por spec y puede causar los errores CORS vistos en consola.
    Lista explícita de orígenes.
A4. **P1.9 Quitar inyección de comandos.** `/admin/stats` y
    `/admin/events/stats` hacen `sh -c "find '{events_dir}' ..."` con
    `events_dir` venido de `disks_config.json` → RCE. Sustituir por
    `pathlib`/`os.scandir`.
A5. **P1.4 Path traversal** en `/reportes/{user_id}/{filename}`:
    `.resolve().is_relative_to(base)`.
A6. **P1.5 `secrets.compare_digest`** para tokens + `chmod 600` en
    `admin_config.json` (hoy world-readable 664).
A7. **P1.6 Quitar endpoints de test públicos**: `/api/admin/centinela-test`,
    `/api/reports/test`, `/api/reports/inject-to-active-chat` (envían push
    real, sin auth).
A8. **P0.7 bind 127.0.0.1** en todos los FastAPI + verificar iptables. Hoy
    `0.0.0.0` queda expuesto en LAN `10.0.0.44` y saltea nginx/CF.
A9. **P0.8 Consolidar `firebase-key.json`** a una ruta, `chmod 600`,
    fail-loud si falta. Hoy 3 copias + 1 ruta inexistente.

### BLOQUE B — Estabilidad (en paralelo, misma semana)

B1. **P0.1 Rotar logs** con logrotate (`api_eva.log` 355MB sin tope).
B2. **P0.2 Retención de alertas** en `cleanup_frames.py`: hoy conserva
    `evt_*.json`+`.jpg` para siempre (68k en una cámara). `retention_days`
    configurable por plan. Filtro de clase confirmado activo → seguro.
B3. **P0.3 Mover I/O bloqueante fuera del loop**: `auth.verify_id_token`,
    FCM `requests.post`, PIL brightness, `subprocess du` → `asyncio.to_thread`
    o `httpx.AsyncClient`.
B4. **P0.4 Corregir NameError vivos**: `_save_vigilance_event`, `cam_id`,
    `_qwen`, `re`, `business_type` + el `KeyError:'msgs'` que da 51×500.
B5. **P0.5 Drop policy en `FRAME_QUEUE`**: `put_nowait` + `QueueFull` →
    contador de drops + métrica. No bloquear al ESP32.
B6. **P0.6 `Restart=always` + `StartLimitBurst=5`** en api-eva, yolo-server,
    qwen, whisper (hoy `on-failure` no reinicia salidas OOM-kill exit-0).

### BLOQUE C — Escalado de concurrencia (antes del 2do/3er cliente CONCURRENTE)

> El colega aclara: el objetivo aquí ya no es "40 cámaras" sino "que el
> cliente 2 no sufra por el cliente 1". Un cliente ocupado no debe
> ralentizar a los demás — esto es bloqueante de venta, no optimización.

C1. **P2.1 Múltiples `yolo_worker` tasks** (4–8) para que el `Semaphore(12)`
    del orchestrator sature de verdad. Hoy hay un solo consumidor.
C2. **P2.4 Cola por cámara, no global.** `FRAME_QUEUE` único → un cámara
    satura y retrasa a todas. Una `asyncio.Queue` por `user_id:camera_id`
    + round-robin de workers.
C3. **P2.2 PIL grid assembly en ThreadPoolExecutor** (libera GIL en C).
C4. **P2.3 `httpx.AsyncClient` compartido** en orchestrator/eva_v2 (hoy
    crea cliente por llamada).
C5. **P2.5 Enrutar VL por `service_bus:8200`** (semáforos por backend).
    Hoy el código lo bordea y golpea `:8004` directo.
C6. **P2.8 Capar concurrencia de MJPEG** (`while True` sin `is_disconnected()`
    ni semáforo → coroutines eternos).

### BLOQUE D — Robustez de datos (puede esperar sin bloquear)

D1. **P3.1 Decidir la cola**: desactivar `redis-ojoia.service` (no se usa) o
    migrar `service_bus` a Redis Streams con `noeviction` (hoy
    `allkeys-lru` evictiría cola durable — contradicción).
D2. **P3.2 `fsync` en escrituras críticas** (`_atomic_write_user_json`,
    `save_event_to_disk`, `save_camera_zones`).
D3. **P3.3 Lock en `camera.json`**: `update_camera_metrics`, `save_camera_zones`
    hacen RMW sin lock → pierden zonas/contadores.
D4. **P3.4 Evictir caches sin tope**: `_frame_cache`, `_USER_JSON_LOCKS`,
    `_vigilance_cooldowns`, `orchestrator.grids`, `_last_notification_ts`,
    `trackers`, `_sessions` (eva_v2). `cachetools.TTLCache`.
D5. **P3.5 `/health` no dependa del GPU**: separar `/health` (liveness) de
    `/health/deep` (modelo).
D6. **P1.7 Pydantic en endpoints críticos** (sustituir 49 `request: dict`).
    Empezar por `/auth/firebase/verify`, `/ingest/*`, `/api/chat/eva/message`.
D7. **P1.8 Rate-limit en `/admin/auth/login`** (hoy hereda 30r/s).

### BLOQUE E — Mantenibilidad (puede esperar)

E1. **P4.1 Partir `api_eva.py`** en `routes/{ingest,auth,admin,reports,chat}.py`
    + `lib/{storage,fcm,locks}.py`. Helpers compartidos con orchestrator.
E2. **P4.2 Borrar muertos**: `api_eva.py.bak`, `orchestrator.py.backup`,
    `ui_server.py`+unit, `yolo.service`, `inference.py`,
    vigilance_prompts top-level, `eva/eva_v2.py` (split-brain).
E3. **P4.3 Config por env**: `STORAGE_ROOT`, `VL_MODEL_URL`, `YOLO_URL`,
    `FIREBASE_KEY`, `PUBLIC_BASE_URL` (hoy cero `os.getenv` en api_eva).
E4. **P4.4 Tests mínimos** `TestClient` + tmp `STORAGE_ROOT`.
E5. **P4.5 CI baseline**: `ruff` + `mypy` pre-commit.
E6. **P4.6 Limpieza configs stale**: 5 `~/.cloudflared/config*.yml`, `.save`,
    `boot_system.sh` path roto.

### BLOQUE F — Operaciones (puede esperar)

F1. **P5.1 Revisar ESP32** upload (~13 GB/día/cámara a 1fps).
F2. **P5.2 `proxy_buffering off` en nginx para `/cameras/*/stream`**.
F3. **P5.3 `watchdog.sh` en cron** (hoy no está scheduleado pese a su cabecera).
F4. **P5.4 Quitar `@reboot … ai-arranque.service`** del cron (ya tiene
    `WantedBy`; el cron rebootea el túnel a los 30s).
F5. **P5.5 Quitar cron duplicado de reportes** (30 7 y 33 7 = double-push).

## Fuera de alcance de este plan (explícito)

- Flujo de Eva, promptsbase, narrativa rica, rediseño de detección:
  **no se tocan** hasta terminar Bloques A–C. Confirmar qué versión del
  flujo corre contra este `api_eva.py` v7.0 es trabajo posterior.

## Medición de éxito

- Cero `NameError` en `api_eva.log` tras 24h.
- `/api/user/events` sin respuestas 500.
- Auth: petición sin token a `/api/user/events` → 401 (no 200).
- Megapanel: `POST /api/control/...` sin auth → 401.
- Disco estable <80% con rotación + retención 24h.
- 2 cámaras activas no se ralentizan mutuamente (Bloque C).
