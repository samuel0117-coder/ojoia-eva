# Informe Arquitectónico Completo — OjoIA

**Fecha:** 2026-08-21
**Alcance:** Backend completo (`/opt/ojoia/code/*.py` ~27,500 LOC), frontend completo (`/opt/ojoia/code/frontend/` ~4,919 LOC activas + 928 KB obsoletos), servicios secundarios (16 archivos ~7,150 LOC).
**Método:** Lectura exhaustiva por agente especializado por archivo + verificación manual de hallazgos críticos.

---

## 🚨 RESUMEN EJECUTIVO — Bugs Activos en Producción

El equipo encontró **4 bugs que están corriendo HOY** en producción:

| # | Bug | Severidad | Ubicación | Impacto |
|---|---|---|---|---|
| 1 | `_saveCooldown` y `deleteCamera` definidos **fuera** del objeto `App` | 🔴 CRÍTICO | `app-v12.js:1848, 3198` | El botón "💾 Guardar cooldown" y "🗑️ Eliminar cámara" lanzan TypeError al hacer click |
| 2 | Redis password hardcoded en `billing.py:48` | 🔴 CRÍTICO | `billing.py:48` | Secreto `hq1V4pQr1c99AWYYAIGBnCu7695jL75` en repo |
| 3 | `enforce_user_auth` middleware bypass con header vacío | 🔴 CRÍTICO | `api_eva.py:1202` | Si handler olvida `_verify_user_token()`, endpoint queda público |
| 4 | `robots.txt` es HTML obsoleto de v5 | 🟠 ALTO | `frontend/robots.txt` | Crawlers ven HTML roto; humanos ven dashboard deprecado |

---

## 📊 MÉTRICAS GLOBALES

| Categoría | Total |
|---|---|
| Archivos Python | 27 (10 principales + 17 soporte) |
| LOC Python totales | ~27,500 |
| Endpoints API (en `api_eva.py`) | ~140 |
| Funciones >100 LOC | ~25 |
| Funciones >200 LOC | ~10 |
| Código muerto detectado | ~600 LOC (~2.2% del total) |
| Archivos frontend obsoletos | 13 (~928 KB en repo) |
| Archivos `.bak` / `.backup` | 5 (~305 KB) |
| Race conditions críticas | 3 (`_sessions`, `_latest_frame`, escritura de user.json sin lock) |

---

## 1. BACKEND — `api_eva.py` (6445 líneas)

### 1.1 🔴 Bugs activos

#### [P0] Race condition en escritura de user.json sin lock
**Líneas:** 1996-2003, 2021-2030, 3042-3056, 3114-3128, 5587-5594, 5598-5612, 5653-5658, 5687-5698, 5710-5714, 5725-5729, 5740-5746, 5800-5809, 5831-5836, 6223-6230, 6242-6251 (~18 sitios)

```python
with open(user_file) as f:
    user_data = json.load(f)
# ... mutación ...
with open(user_file, "w") as f:
    json.dump(user_data, f, indent=2)  # ← sin lock, sin atomic write
```

**Problema:** Si entre el `read` y el `write` otro request (e.g. `_update_camera_last_frame` cada segundo) escribe, los cambios se pierden. Un crash entre `write_text` y `close` corrompe el archivo.

**Fix:** Aplicar `_get_user_lock(user_id)` + `_atomic_write_user_json()` en TODOS los writes.

#### [P0] `enforce_user_auth` middleware bypass
**Línea 1202-1206:**
```python
if not request.headers.get("authorization"):
    resp = JSONResponse({"detail": "Authorization requerido"}, status_code=401)
    return resp
return await call_next(request)  # ← si tiene header pero no user_id, NO valida
```

Si la petición tiene `Authorization` (aunque sea inválido) pero **no** `user_id` en path, el middleware delega al handler sin validar. La validación depende de que el handler llame manualmente a `_verify_user_token`. Si el handler olvida, endpoint queda público.

**Fix:** El middleware debería leer el body async para extraer user_id; o marcar endpoints públicos con un marker explícito (no allowlist por path).

#### [P0] Path traversal en endpoints de eventos
**Líneas:** 3232-3234, 3322, 3379 — `/api/event-thumb/{event_id}`, `/api/events/{event_id}/frame/{index}`, `/api/events/{event_id}` no llaman a `_validate_safe_path`. Aunque `event_id` viene del backend (`evt_<ts>_<cam>`), si un atacante puede crear eventos (vía ingest malicioso) puede inyectar paths arbitrarios.

**Fix:** Llamar `_validate_safe_path(event_id, "event_id")` en TODOS los endpoints que interpolan `event_id` en filesystem.

### 1.2 Duplicación severa

| Patrón | Sitios | Líneas afectadas |
|---|---|---|
| "abrir user.json + actualizar campo + escribir" | 25 | ~600 |
| "iterar todos los user.json de todos los discos" | 5 | ~150 |
| "cargar events dirs + iterar por mtime" | 5 | ~120 |
| "online si last_announce/last_frame < 120s" | 4 | ~40 |
| "FCM con google.oauth2.service_account" | 3 | ~90 |

**Fix prioritario:** `def update_user_field(user_id, mutator_fn)` que encapsule read-modify-write bajo lock.

### 1.3 Código muerto

| Función | Línea | Acción |
|---|---|---|
| `_get_plan_features` | 481 | ❌ Borrar |
| `_enforce_plan_on_create_camera` | 530 | ❌ Borrar |
| `_enforce_plan_on_ingest` | 547 | ❌ Borrar |
| `_parse_json_body` | 388 | ❌ Borrar |
| `verify_user` (Depends) | 321 | ❌ Borrar |
| `_auth_user_from_body` | 381 | ❌ Borrar |
| `_sum_people_in_events` | 2557 | ❌ Borrar |
| `_resolve_user_events_dir` | 2539 | ❌ Borrar |
| `eva_sessions` (global) | 426 | ❌ Borrar |
| `register_fcm_token` / `unregister_fcm_token` (legacy) | 1978, 2014 | ⚠️ Verificar uso, probablemente borrable |

### 1.4 God functions (>150 líneas)

| Función | LOC | Responsabilidades mezcladas |
|---|---:|---|
| `chat_eva_message` | 190 | Validación token + carga user.json + sesión + LLM + persistencia + threading daemon |
| `send_report_v2` | 165 | Parseo + generate_report + persistencia triple + FCM inline |
| `_process_ingest` | 170 | 8 pasos numerados: frame + YOLO + alerta + grid + last_frame + response |

### 1.5 5 endpoints "enviar reporte" — solo 1 está activo
- `send_report_v2` (cron interno) ✅
- `send_report_to_channel` (manual via panel) ✅
- `send_report_manual` (4645), `send_daily_report_production` (5045), `inject_report_to_active_chat` (4955) — **probablemente dead**

### 1.6 Endpoints admin stub (no-op)
**~12 endpoints** retornan `{"success": True}` sin lógica:
- `admin_confirm_event`, `admin_dismiss_event` (5919, 5925)
- `admin_cloudflared_save`, `admin_sync_firestore` (6021, 6027)
- `admin_clear_queue` (6267)
- Firestore queue trio (6350-6362)
- `admin_queue_status` (6368)
- `admin_update_server_status` (6385)

**Riesgo:** Operador cree que confirmó un evento y no fue así. Marcar con `@deprecated` o implementar.

### 1.7 Magic strings hardcoded

| Constante | Sitios | Acción |
|---|---|---|
| `/home/sam/ai_system/firebase-key.json` | 3 | Usar `FIREBASE_KEY_PATH` |
| `STORAGE_ROOT / "users" / user_id / "user.json"` | 6 | Usar `find_user_json(user_id)` |
| `120` (online threshold) | 4 | `ONLINE_THRESHOLD_SEC` |
| `https://api.ojoia.com.do` | 6 | `PUBLIC_BASE_URL` env var |
| `"chatierbox"` | 30+ | Constante única |
| `IP_USER_MAP = {"10.0.0.161": "moXcjYsfYogCFfvHq0TmadF8ytt2"}` | 1 | Mover a `disks_config.json` (es fallback de seguridad) |

---

## 2. BACKEND — `eva_v2.py` (3846 líneas)

### 2.1 🔴 Bugs activos

#### [P0] `_sessions` global sin lock — race conditions reales
`_sessions: Dict[str, Dict] = {}` se muta desde múltiples corrutinas. Cada handler hace `_sessions[sid] = session` o `session["msgs"].append(...)`. Async es cooperativo pero entre `await` cede el control; cualquier `await _call_qwen(...)` permite race.

**Escenario real:** Dos requests concurrentes al mismo `session_id` (e.g., retry HTTP + cron) pierden `append` de mensajes.

**Fix:**
```python
_session_locks: Dict[str, asyncio.Lock] = {}
async def _with_session(sid, fn):
    lock = _session_locks.setdefault(sid, asyncio.Lock())
    async with lock:
        sess = _sessions.get(sid) or await _load_from_disk(sid)
        result = await fn(sess)
        _sessions[sid] = sess
        _save_session_to_disk(sess)
        return result
```

#### [P0] God function `_handle_os_mode_v2` — 458 líneas
**Líneas 2712-3171.** Concentra 6 responsabilidades:
1. Routing hardcoded de intents (~155 líneas de `if any(k in msg_lower for k in ...)`)
2. Construcción de system prompt (~50 líneas)
3. Filtrado de historial + presupuesto de tokens (~50 líneas)
4. Detección de tool_call nativa vs Hermes fallback (~18 líneas)
5. Retry con `tool_choice="required"` (~43 líneas)
6. Ejecución del tool + segundo LLM call (~63 líneas)

**Fix:** Dividir en 6 funciones puras.

#### [P0] System prompt construido en 3 lugares distintos
- `_build_system_prompt` (521-569): prompt LLM de testigo puro
- `_build_fallback_prompt` (571-585): fallback hardcoded
- `sys_p` inline en `_handle_os_mode_v2` (2914-2962): system prompt del OS mode

**Fix:** Centralizar en `eva/prompts.py` con builders puros.

### 2.2 Código muerto (180 líneas)

| Función | Línea |
|---|---|
| `_sanitize_os_tool_params` | 3528 |
| `_normalize_os_tool_params` | 3559 |
| `_build_os_chat_messages` | 3175 |
| `_route_os_message` | 3193 |
| `_load_current_vigilance_normal` | 1031 |
| `_list_from_config` | 1041 |
| `_handle_daily_summary` | 1342 |
| `_frame_problem` | 645 |
| `_TOOLS_JSON_SCHEMA` | 43-58 |

### 2.3 Constructores de sesión duplicados (3 sitios)
- `_handle_setup` rama new_camera (1190-1206)
- `handle_eva_v2` rama no-session (1216-1231)
- `_make_os_session` (1247-1263)

**Fix:** `_new_session(user_id, session_id, phase)` con kwargs.

### 2.4 Funciones largas sin refactor

| Función | LOC | Acción |
|---|---:|---|
| `_handle_os_mode_v2` | 458 | P0 — dividir en 6 |
| `_detect_intent_and_route` | 353 | P1 — tabla de intents |
| `_build_vigilance_update_from_message` | 200 | P1 — 4 sub-funciones |
| `_handle_setup` | 102 | P2 — dispatch dict |
| `_handle_context` | 105 | P2 — extraer context_step handlers |

### 2.5 Anti-patterns
- **~40** `except Exception: pass` o logger.warn sin acción
- **30+** imports inline (deberían estar al top)
- `glob` usado sin importar (línea 154, NameError potencial)
- `_normalize_text(_normalize_text(x))` aplicado dos veces (líneas 910, 919)

---

## 3. FRONTEND — `app-v12.js` (3619 líneas)

### 3.1 🔴 Bugs activos

#### [P0] `_saveCooldown` y `deleteCamera` definidos FUERA del objeto App
```js
// Línea 1846: },    ← cierre del objeto App
1848: async _saveCooldown(camId, btn) { ... }   ← función top-level, this será undefined
// Línea 3198: async deleteCamera(camId, camName) { ... }   ← mismo problema
```

`App` abre en línea 55 y cierra en línea 3688. Estos métodos están en 1848 y 3198 → **son funciones globales huérfanas**.

**Impacto en producción:**
- Botón "💾 Guardar cooldown" → `App._saveCooldown is not a function`
- Botón "🗑️ Eliminar cámara" → `App.deleteCamera is not a function`

**Fix (urgente):** Mover ambos dentro del literal `App` antes de `};` línea 3688.

#### [P0] XSS residual via `innerHTML` con interpolación
`_escAttr` se aplica a 50+ IDs, pero otras interpolaciones user-controlled no se escapan:
- `app-v12.js:494` `${title}`, `${msg}` en `_toast` — strings arbitrarios
- `app-v12.js:2236` `attentionHits.map(h => \`• ${h}\`)` — **`h` no se escapa**, viene de la API
- `app-v12.js:2077-2089` `vision.scene` sin escape
- `app-v12.js:2098-2105` `${enrichedDesc}` — descripción parseada sin escape

**Fix:** Aplicar `escapeHtml` (existe en `eva-chat-v7.js:993`) a TODA interpolación de string user-controlled.

### 3.2 Duplicación entre archivos

| Helper | `app-v12.js` | `eva-chat-v7.js` | Acción |
|---|---|---|---|
| `_escAttr` | L59-62 (con `s==null`) | L23-26 (con `s||''`) | Extraer a `lib/dom-utils.js` |
| `escapeHtml` | ❌ No tiene | L993 | Mover a `lib/dom-utils.js` |
| `cleanEventDescription` | L2035 (con prefijo `_`) | L661 (sin prefijo) | Renombrar + unificar |

**Riesgo:** Ya divergieron (`catch(e) { console.warn }` vs `catch(e) {}`). Si arreglas uno, otro queda desincronizado.

### 3.3 Código muerto — 928 KB recuperables

| Archivo | Tamaño | Acción |
|---|---:|---|
| `app-v5.js` | 72 KB | ❌ Borrar |
| `app-v5-formatted.js` | 45 KB | ❌ Borrar (artefacto prettier) |
| `app-v6.js` | 74 KB | ❌ Borrar |
| `app-v7.js` | 87 KB | ❌ Borrar |
| `app-v8.js` | 99 KB | ❌ Borrar |
| `app-v10.js` | 113 KB | ❌ Borrar |
| `app-v11.js` | 172 KB | ❌ Borrar |
| `app-v12.js.backup_1787151067` | 182 KB | ❌ Borrar |
| `eva-chat.js` | 15 KB | ❌ Borrar |
| `eva-chat-v2.js` | 15 KB | ❌ Borrar |
| `eva-chat-v3.js` | 15 KB | ❌ Borrar |
| `eva-chat-v4.js` | 15 KB | ❌ Borrar |
| `eva-chat-v5.js` | 45 KB | ❌ Borrar |
| **Total** | **~928 KB** | |

### 3.4 Funciones >100 líneas

| Función | LOC | Acción |
|---|---:|---|
| `_moveTimeline` | 372 | P1 — dividir en 4 |
| `_loadCamVigilance` | 249 | P1 — separar config + render + handlers |
| `_eventRowHtml` | 223 | P1 — extraer parsers de visión |
| `_updateConfigButtonStates` | 186 | P1 — tabla declarativa |
| `_moveEventFrame` | 186 | P1 — separar navegación + render |
| `_showSubscription` | 175 | P1 — 8 estados condicionales |
| `_renderEvaChat` | 148 | P2 |
| `_isOsIntentText` (eva-chat) | 216 | P1 — lista gigante de 60+ keywords hardcoded |

### 3.5 Inconsistencias de naming
- `camera_id` (backend) vs `camId` (frontend locals) vs `cameraId` (ruta L443)
- Prefijo `_` inconsistente (algunos privados lo tienen, otros no)
- `App._saveCooldown` declarada fuera de App (mezcla scopes)

### 3.6 Anti-patterns
- **~100** `onclick="App.xxx(...)"` inline → CSP `script-src` lo rompería
- **71** `innerHTML` directos (de los cuales 15+ con interpolación)
- `_homeLastYoloFetchByCam` (L752) crea mapa sin cleanup cuando se elimina cámara → memory leak leve
- Listeners duplicados en `_startAuth` (L154-157) sin protección anti-duplicación

---

## 4. SERVICIOS SECUNDARIOS — 16 archivos (~7,150 LOC)

### 4.1 🔴 Bugs activos

#### [P0] Redis password hardcoded
**`billing.py:48`:** `redis://:hq1V4pQr1c99AWYYAIGBnCu7695jL75@127.0.0.1:6379/0`

Es fallback de `os.environ.get("REDIS_URL")`. Si el env var no está, **se filtra el secreto en cada deploy**.

**Fix:** Lanzar error si env var no está; nunca fallback a string.

#### [P0] `vigilance_prompts.py` (top-level) es 100% código muerto
`api_eva.py:3776` importa `from eva.vigilance_prompts import format_vision_prompt` (que es **OTRO archivo** en `eva/vigilance_prompts.py`). El archivo top-level puede borrarse sin consecuencias.

**Acción:** ❌ Borrar.

#### [P0] `face_pipeline.py` importado pero no usado
`orchestrator.py:16` importa `identify_from_frame` y `extract_face_from_frame`, pero **ninguna se llama**. InsightFace buffalo_l carga 600MB de modelo RAM para nada.

**Acción:** Eliminar el import.

### 4.2 Servicios duplicados

#### [P1] `gpu1_image_server.py` (root) duplica `ai_system/gpu1_image_server.py`
Mismo puerto 8015, mismas rutas, mismo md5. **Fusionar.**

### 4.3 Funciones duplicadas entre servicios

| Helper | Sitios |
|---|---|
| Lectura de `camera.json` | api_eva, orchestrator, camera_zones |
| Read/write user.json sin lock | reportes/*.py |
| Extractores de usage OpenAI | service_bus (×2) + billing (×1) |
| Lista maestra de servicios | health_monitor.py:53-100 + megapanel.py:160 |

### 4.4 Constantes hardcoded

| Constante | Archivos |
|---|---|
| `STORAGE_ROOT = "/home/sam/storage"` | orchestrator, camera_zones, face_pipeline + 9 más |
| `users/{user_id}/cameras/{camera_id}/camera.json` | api_eva (×2), camera_zones (×3), orchestrator (×3), eva_v2 |
| `users/{user_id}/user.json` | api_eva (×10+), reportes |
| `/home/sam/projects` | project_server |
| `/home/sam/ojoia-billing-db/billing.db` | billing_log |
| `/home/sam/ai_system/venv/bin/python` | yolo_dispatcher |
| `/home/sam/esp32cam_project/...` | ui_server |

**Acción:** Crear `ojoia_paths.py` con paths centralizados via env vars.

### 4.5 Funciones grandes

| Función | LOC | Archivo |
|---|---:|---|
| `ui()` (HTML inline) | 576 | megapanel.py:569 |
| `_enqueue_and_proxy` | 209 | service_bus.py:193 |
| `_enrich_qwen_json_from_metadata` | 155 | orchestrator.py:1199 |
| `send_fcm_notification` | 143 | orchestrator.py:344 |
| `_detect_attention_hits` | 143 | orchestrator.py:1372 |

### 4.6 service_bus.py — operacional
Los backends `qwen9b:8018` y `qwen35b:8019` referenciados **no se inician en boot** ni tienen systemd unit. Antes de invertir tiempo consolidando, verificar con operador si está en desuso.

---

## 5. PLAN DE ACCIÓN PRIORIZADO

### 🔴 Sprint "Hotfixes" (1-2 días) — Bug fixes críticos

| # | Acción | Esfuerzo |
|---|---|---|
| H1 | Mover `_saveCooldown` y `deleteCamera` dentro del objeto `App` en `app-v12.js` | 30 min |
| H2 | Borrar 13 archivos frontend obsoletos (~928 KB) | 5 min |
| H3 | Borrar `robots.txt` (HTML obsoleto) o renombrar a `index_v5_backup.html` | 2 min |
| H4 | Mover Redis password de `billing.py:48` a env var obligatorio | 15 min |
| H5 | Eliminar import de `face_pipeline` en `orchestrator.py:16` | 5 min |
| H6 | Borrar `vigilance_prompts.py` (top-level, muerto) | 2 min |
| H7 | Borrar bloque duplicado `sdxl_generate`/`sdxl_models` en `ui_server.py:209-283` | 5 min |
| H8 | Borrar 5 archivos `.bak`/`.backup` (~305 KB) | 2 min |

### 🟠 Sprint "Seguridad" (2-3 días) — P0 race conditions + XSS

| # | Acción | Esfuerzo |
|---|---|---|
| S1 | Aplicar `_get_user_lock` + `_atomic_write_user_json` en 18 sitios de `api_eva.py` | 2h |
| S2 | Implementar `_with_session` lock para `_sessions` en `eva_v2.py` | 2h |
| S3 | Validar path en endpoints `/api/events/{event_id}*` y `/api/event-thumb/{event_id}` | 15 min |
| S4 | Aplicar `escapeHtml` a 15+ sitios de interpolación en `app-v12.js` | 2h |
| S5 | Fix middleware `enforce_user_auth` para validar body con user_id | 2h |

### 🟡 Sprint "Refactor" (1-2 semanas) — Arquitectura

| # | Acción | Esfuerzo |
|---|---|---|
| R1 | Dividir `api_eva.py` en módulos FastAPI routers (`auth.py`, `user_storage.py`, `ingest.py`, `admin.py`, `eva_chat.py`, `reports.py`) | 2 días |
| R2 | Dividir `_handle_os_mode_v2` (458 líneas) en 6 funciones puras | 4h |
| R3 | Extraer `lib/dom-utils.js` con `_escAttr`, `escapeHtml`, `cleanEventDescription` | 4h |
| R4 | Crear `ojoia_paths.py` y migrar 9 archivos | 4h |
| R5 | Eliminar ~600 LOC de código muerto (sección 1.3 + 2.2) | 2h |
| R6 | Fusionar 3 constructores de sesión en `eva_v2.py` → `_new_session()` | 2h |
| R7 | Eliminar 12 endpoints admin stub o marcarlos `@deprecated` | 1h |
| R8 | Reemplazar `@app.on_event("startup")` por `lifespan` (3 sitios) | 1h |

### 🟢 Sprint "Limpieza final" (opcional, 1 día)

| # | Acción | Esfuerzo |
|---|---|---|
| C1 | Auditar y eliminar 3 de 5 endpoints "enviar reporte" | 2h |
| C2 | Mover magic strings a constantes (`FIREBASE_KEY_PATH`, `ONLINE_THRESHOLD_SEC`, etc.) | 2h |
| C3 | Mover `IP_USER_MAP` de hardcoded a `disks_config.json` | 30 min |
| C4 | Adoptar event delegation para eliminar ~100 `onclick` inline | 1 día |
| C5 | Adoptar bundler (Vite/Rollup) + módulos ES | 2 días |

---

## 6. CONCLUSIÓN

**El producto OjoIA funciona en producción** y los P0 del sprint anterior eliminaron los crashes más visibles. Pero la auditoría revela que:

- **4 bugs activos** (2 frontend críticos, 1 secret leak, 1 middleware bypass) están corriendo HOY
- **~600 LOC de código muerto** (2.2% del total) confunde el repo
- **~928 KB de archivos obsoletos** en frontend (0 impacto en bundle, pero dificulta navegación)
- **~305 KB de `.bak`** listos para borrar
- **1 god function de 458 líneas** + 9 funciones >200 líneas requieren división
- **Refactor mayor pendiente** para llevar el monolito a módulos

**Quick wins (Sprint Hotfixes, 1 día):**
1. Bugs críticos corregidos (botones rotos, secret leak)
2. **-1.2 MB de archivos muertos borrados**
3. Frontend y backend validados sin bugs activos

**El sprint "Hotfixes" + "Seguridad" es la prioridad inmediata.** El resto puede iterar.
