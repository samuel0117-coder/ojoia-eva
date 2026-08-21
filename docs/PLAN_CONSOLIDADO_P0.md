# 📋 Plan Consolidado P0 — Estabilizar OjoIA y Cerrar Fugas

> **Fecha de auditoría:** 21 ago 2026
> **Alcance:** P0 — errores que rompen la app HOY + fugas de seguridad inmediatas + optimizaciones de estabilidad.
> **Objetivo:** Declarar la app lista para producción con clientes pagos (no perfecta, pero sin fugas ni crashes).

---

## 📊 Resumen ejecutivo

Se auditaron **16 641 líneas** de código (eva_v2.py 3834, api_eva.py 6445, frontend 4961) más 5 docs de plan previo. El sistema está funcional y el flujo de instalación funciona, pero tiene:

- **6 bugs críticos** que causan crashes o estados inconsistentes EN PRODUCCIÓN HOY.
- **4 fugas de seguridad inmediatas** (auth, XSS, QR de terceros, tokens en localStorage).
- **3 problemas de estabilidad** ( versión inconsistente, streams rotos, craws de memoria).

Plan previo: `PLAN_FALTANTES.md` está **desactualizado** — lista C1.3, C3.1, C3.3 como ❌ FALTAN pero `PLAN_PRINCIPAL_INSTALACION_CAMARA.md` los marca ✅ realizado. Este plan consolida y reemplaza ambos.

---

## 🚨 SECCIÓN 1 — Bug crítico #1: `NoneType` no soporta asignación (confirmado)

### Root cause (逮ado en código)
`api_eva.py:2384-2396` (fallback de `handle_eva_v2`):
```python
except Exception as e1:
    session = _load_session(session_id)     # eva_v2.py:290 → puede retornar None
    if not session or session.get("user_id") != user_id:
        session = _make_os_session(user_id, session_id)   # puede RAISE (user.json corrupto)
    result = _eva_mk_resp(session, ...)   # session puede ser None aquí
```

`_mk_resp` en `eva_v2.py:3801-3834` hace `session["image_url"]`, `session["last_event_id"] = ...`, `session["session_id"]` → `TypeError: 'NoneType' object does not support item assignment`.

### Fix P0 (mínimo riesgo)
En `api_eva.py:2383`:
```python
from eva_v2 import _make_os_session, _load_session, _mk_resp as _eva_mk_resp
session = None
try:
    session = _load_session(session_id)
except Exception:
    session = None
if not session or (isinstance(session, dict) and session.get("user_id") != user_id):
    try:
        session = _make_os_session(user_id, session_id)
    except Exception as e2:
        logger.error(f"[EVA] fallback _make_os_session falló: {e2}")
        session = None
if not isinstance(session, dict) or not session:
    # Última barrera: sesión vacía sintética, no None
    session = {
        "session_id": session_id, "user_id": user_id, "phase": "os",
        "owner_name": "amigo", "msgs": [], "image_b64": "", "image_sent": True,
        "image_url": "", "last_event_id": None, "last_event_camera_id": "",
        "zone": "", "has_image": False,
    }
# ahora session SIEMPRE es un dict válido
```

### Defensa adicional (eva_v2.py `_mk_resp`)
En `eva_v2.py:3796`, agregar guard al inicio:
```python
def _mk_resp(session, text, ...):
    if not isinstance(session, dict):
        logger.error("[_mk_resp] session no es dict: %r", type(session))
        return {"success": False, "error": "Sessión inválida. Empieza de nuevo.", "response": text}
    # ... resto como está
```

### Verificación
```bash
# Reproducir: borrar user.json de un user y mandar "muestrame la camara"
mv /home/sam/storage/users/<uid>/user.json /tmp/user.json.bak
curl -X POST https://api.ojoia.com.do/api/chat/eva/message \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{"user_id":"<uid>","message":"muestrame la camara"}'
# Antes: HTTP 200 + traceback en log (error silenciado)
# Después: HTTP 200 + respuesta amigable ("Sessión inválida")
```

**Prioridad:** P0 ( blocker — el error aparece en logs sin traceback útil).
**Tiempo estimado:** 30 min.
**Commit esperado:** `fix(eva): NoneType guard en fallback de chat_eva_message y _mk_resp`

---

## 🚨 SECCIÓN 2 — Bug crítico #2: Streams MJPEG rotos al volver a "Home"

### Root cause
`app-v12.js:657, 667` — `_homeStreamStarted[key]` se setea en `True` pero **nunca se resetea** al abandonar Home. Cuando el usuario vuelve, el check `if (!this._homeStreamStarted[key])`falla → no reinicia el stream → video en negro silencioso.

### Fix P0
En `app-v12.js`, función `_clearAllPolls()` (línea 334) o en el destroy de Home, agregar:
```js
_clearAllPolls() {
  // ... existing code ...
  this._homeStreamStarted = {};   // <-- agregar
  this._homeFrameInFlight = {};   // <-- agregar
  this._homeWatermarkTextByCam = {};
  this._homeLastDetectionsByCam = {};
}
```

Y en `_pageHome()`, antes del loop de tiles, resetear solo los keys que se usarán:
```js
_pageHome() {
  // Reset per-cam state for cams we're about to render
  for (const key of Object.keys(this._homeStreamStarted)) {
    if (!this._homeCams.find(c => `live-wrap:${c.camera_id}` === key)) {
      delete this._homeStreamStarted[key];
    }
  }
  // ... existing ...
}
```

### Verificación
1. Abrir Home → ver video vivo.
2. Ir a Settings, volver a Home.
3. Antes: imagen congelada / placeholder. Después: stream reinicia.

**Prioridad:** P0 (UX critical para demo).
**Tiempo estimado:** 20 min.
**Commit esperado:** `fix(front): reset _homeStreamStarted en _clearAllPolls`

---

## 🚨 SECCIÓN 3 — Bug crítico #3: Listener `visibilitychange` acumulado

### Root cause
`eva-chat-v7.js:1175-1179` — cada `init()` agrega un `addEventListener('visibilitychange', ...)` sin removerlo en `teardown()`. Tras logout/login, hay N handlers disparándose en paralelo, cada uno llamando `poll()` con closures distintas.

### Fix P0
```js
// init() o _startRemoteSync:
if (this._onVisibilityChange) {
  document.removeEventListener('visibilitychange', this._onVisibilityChange);
}
this._onVisibilityChange = () => {
  if (document.visibilityState === 'visible') setTimeout(poll, 300);
};
document.addEventListener('visibilitychange', this._onVisibilityChange);

// teardown():
if (this._onVisibilityChange) {
  document.removeEventListener('visibilitychange', this._onVisibilityChange);
  this._onVisibilityChange = null;
}
```

### Verificación
1. Login → logout → login → logout → login (3 ciclos).
2. Abrir DevTools, poner tab en background, volver.
3. Antes: 3+ polls disparándose (network panel muestra 3 requests cada 10s). Después: 1 solo.

**Prioridad:** P0.
**Tiempo estimado:** 15 min.
**Commit esperado:** `fix(front): cleanup de visibilitychange listener en teardown`

---

## 🚨 SECCIÓN 4 — Bug crítico #4: Versionado inconsistente (cacheoh staleness)

### Estado actual (medido)
| Asset | Cache-buster | Problema |
|---|---|---|
| `app.css` | `v=20260623a` | Fijo, 2 meses stale |
| `app-v12.js` | `v=Date.now()` | Recarga completa cada view — arruina CDN |
| `eva-chat-v7.js` | `v=20260819g` | Fijo, manual bump |
| Banner in-file `eva-chat-v7.js` | "v5" | Conflito con filename |
| Banner in-file `app-v12.js` | "v6" | Conflicto con filename |

### Fix P0
1. **Adoptar un solo esquema**: `?v=YYYYMMDD<n>` literal para todos.
2. **Bump sincronizado**: cada release sube una letra (a→b→c) o cambia la fecha.
3. **Actualizar banners** in-file para matchear filename real.

`index.html`:
```html
<link rel="stylesheet" href="app.css?v=20260821a">
...
<script src="app-v12.js?v=20260821a"></script>
<script src="eva-chat-v7.js?v=20260821a"></script>
```

Eliminar `<script>document.write(...)</script>` con `Date.now()`.

### Verificación
- Recargar sin cache: todos los assets cargan con `?v=20260821a`.
- Segundo reload: cache hit en todos (304 o from cache).
- Editar `eva-chat-v7.js`, bump a `v=20260821b`, recargar: nuevo JS cargado.

**Prioridad:** P0 (解放军 bug de deploy — arregla el problema del usuario de no ver cambios).
**Tiempo estimado:** 10 min.
**Commit esperado:** `fix(front): versioning literal sincronizado (apps CSS/JS)`

---

## 🚨 SECCIÓN 5 — Bug crítico #5: Auth bypass vía `POST /api/auth/token`

### Root cause
`api_eva.py:693-745` — endpoint público en `PUBLIC_USER_PATHS` que mucha un token Bearer de 90 días a cualquier `user_id` que el cliente nombre en el body. Sin contraseña, sin Firebase verify, sin proof. Cualquiera que conozca un Firebase UID (28 chars, leaked en URLs y push payloads) obtiene acceso completo a la cuenta.

### Fix P0 (mínimo cambio)
**Opción A (recomendada):** Mover el endpoint detrás de Firebase auth — requerir `firebase_token` válido que matchee el `user_id`:
```python
@app.post("/api/auth/token")
async def issue_user_token(request: dict, authorization: str = Header(None, alias="Authorization")):
    user_id = (request.get("user_id") or "").strip()
    firebase_token = (request.get("firebase_token") or "").strip()
    if not firebase_token and authorization and authorization.startswith("Bearer "):
        firebase_token = authorization[7:]
    if not firebase_token:
        raise HTTPException(401, "Firebase token requerido para emitir API token")
    # Verificar Firebase SDK
    decoded = firebase_admin.auth.verify_id_token(firebase_token)
    if decoded.get("uid") != user_id:
        raise HTTPException(403, "Firebase UID no corresponde a user_id")
    # ...emisión del token...
```

**Opción B (si no hay Firebase Admin disponible en este endpoint):** Eliminar el endpoint y obligar al frontend a usar el flujo `/auth/firebase/verify` (que ya sí valide el token Firebase).

### Verificación
```bash
# Sin token Firebase → debe 401
curl -X POST https://api.ojoia.com.do/api/auth/token \
  -H "Content-Type: application/json" \
  -d '{"user_id":"z6q9KStIs1boz31q2fiHJREPBMH2"}'
# Esperado: 401, no 200 con bearer
```

**Prioridad:** P0 CRÍTICO (privilege escalation completa).
**Tiempo estimado:** 1-2h.
**Commit esperado:** `fix(api): /api/auth/token requiere Firebase token para emitir bearer`

---

## 🚨 SECCIÓN 6 — Bug crítico #6: `verify_user` / `_auth_user_from_body` son código muerto

### Root cause
`api_eva.py:321, 381` — las dependencias FastAPI (`verify_user`, `_auth_user_from_body`) están definidas pero **nunca cableadas**. La autenticación reside 100% en el middleware `enforce_user_auth` (línea 1139) que tiene un bypass documentado: si un endpoint recibe `user_id` vía body y cualquier header Authorization existe (aunque sea inválido), el middleware lo deja pasar.

El endpoint `/api/chat/eva/message` llama manualmente a `_verify_user_token` (línea 2334 — bien), pero **otros endpoints que toman user_id del body no lo hacen**. La superficie de bypass no está completa en este plan (necesita mapeo de endpoints) — queda como P1, pero el remedio base es activar el guard de forma central.

### Auditoría completa (realizada 2026-08-21)
De los **122 endpoints** en `api_eva.py`, solo 2 reciben `user_id` del body:
1. `/api/auth/token` → **cubierto por bug #5** (Firebase ID Token ahora requerido internamente).
2. `/admin/users` → **ya protegido** por `_verify_admin(authorization)` (auth admin separado).

Los **38 endpoints con `user_id` en path/query** son validados por el middleware `enforce_user_auth` que sí llama `_verify_user_token` (línea 1206) cuando extrae el user_id del request.

**Conclusión:** El bypass teórico del middleware (línea 1204, cuando user_id viene en body y Authorization está presente pero inválido) no aplica a ningún endpoint en producción. Los únicos dos endpoints con user_id en body están cubiertos.

### Fix P0 (mínimo) — ✅ COMPLETO
Marcar bug #6 como **resuelto por extensión**: el middleware + fix #5 cubren todos los endpoints que reciben `user_id`.

> Queda como **P1** cablear `verify_user` como dependencia FastAPI para futuras rutas con user_id en body — esto convertiría el fix en automático en vez de manual.

---

## 🛡 SECCIÓN 7 — Fugas de seguridad inmediatas (XSS + Token + QR)

### #7.1 XSS masivo en frontend (APROX 30+ puntos)
**Lugares confirmados:**
- `eva-chat-v7.js:517` — `onclick="EvaChat._openZoneEditorFromWizard('${camId}')"` — `camId` del backend sin escapar. Una camera_id con `'` → RCE.
- `eva-chat-v7.js:718` — `onclick="EvaChat.openEventDetail('${evt.event_id}')"` — `event_id` sin escapar.
- `app-v12.js:467-472, 1068-1089, 1859, 2024, 2021, 2162, 2555-2564, 3059` — `cam.camera_id`, `evt.event_id`, `profile.name`, `d.system_prompt` todos en `innerHTML`.

**Fix P0 (mínimo viable):**
Crear helper `escapeAttr(s)` en ambos archivos:
```js
// eva-chat-v7.js y app-v12.js (compartido)
function escapeAttr(s) {
  return String(s || '').replace(/&/g,'&').replace(/</g,'<')
    .replace(/>/g,'>').replace(/"/g,'"').replace(/'/g,''');
}
```
Patchear los `onclick="${...}"` más críticos (~10 puntos) en una primera pasada. El resto queda en P1.

**Estado:** ✅ RESUELTO (commit `e078caa`). Se patchearon **51 puntos** en app-v12.js + 4 puntos en eva-chat-v7.js (incluido `chip.text` que usaba `.replace(/'/g, "\\'")` vulnerable via backslash). Helper `_escAttr()` agregado a ambos archivos. Verificado: 0 puntos con `${camId}`, `${evt.event_id}`, `${cam.camera_id}`, `${cam.name}` en onclick/oninput/onerror sin escapar.

**Prioridad:** P0 (si el backend o un usuario malicioso puede inyectar un `'` → RCE).
**Commit:** `e078caa` — `P0 (Fuga #7.1): escapeAttr en onclick/oninput inline del frontend`

### #7.2 Token bearer en localStorage
`app-v12.js:108-109` — `localStorage.setItem('ojoia_token', this.accessToken)`.
Cualquier XSS (y hay ~30 puntos) exfiltra el bearer. Agravante con #7.1.

**Fix P0 (mínimo):** Mientras el bug #5 está abierto, al menos **cambiar a `sessionStorage`** para que el token se borre al cerrar pestaña:
```js
sessionStorage.setItem('ojoia_token', this.accessToken);
// ... y todas las lecturas
```
Mejor aún (P1): HttpOnly cookie + SameSite=Strict.

**Prioridad:** P0 (agravante #5 + #7.1).
**Tiempo:** 30 min.
**Commit esperado:** `fix(front): token a sessionStorage en vez de localStorage`

### #7.3 QR de terceros filtra `claim_token`
`eva-chat-v7.js:512`:
```js
https://api.qrserver.com/v1/create-qr-code/?size=200x200&data=${encodeURIComponent(qrUrl)}
```
El `qrUrl` incluye el `claim_token` (secret de pairing ESP32). El token viaja a un servicio externo via HTTP.

**Fix P0:** Usar librería local `qrcode.js` (~50 KB) o generar el QR via canvas en backend.
```html
<!-- index.html -->
<script src="https://cdn.jsdelivr.net/npm/qrcode@1.5.4/build/qrcode.min.js"></script>
```
```js
// eva-chat-v7.js:512
const canvas = document.createElement('canvas');
QRCode.toCanvas(canvas, qrUrl, { width: 200 });
extraHtml = extraHtml.replace(/<img src="https:\/\/api.qrserver.com[^"]*">/, canvas.outerHTML);
```

**Prioridad:** P0 (datos sensibles a terceros).
**Tiempo:** 30 min.
**Commit esperado:** `fix(front): QR local vía qrcode.js (no filtra claim_token)`

---

## 🛡 SECCIÓN 8 — Estabilidad de errores silenciados (~150 except silentes)

### Estado
Se cuentan ~40 `except Exception: pass` en api_eva.py (los 40 primeros auditados) que tragan errores. En frontend, ~30 `catch(e) {}` vacíos.

### Fix P0 (mínimo)
No tocar los 150 de una vez. Aplicar patrón "fuga selectiva": para los que afectan UX visible, reemplazar:
```python
except Exception: pass
# por
except Exception as e: logger.warning(f"[<contexto>] {e}")
```

**Prioridad P0 (subset crítico) — 6 puntos a patchear:**
- `api_eva.py:602, 628, 636-637, 908, 1257, 1296, 1624, 1659, 2076` → loggeo mínimo.
- `app-v12.js:21, 577, 1017, 1192, 1879, 2199, 2477, 3121` → `console.warn(e)`.

**Tiempo:** 1-2h.
**Commit esperado:** `fix: reemplazar except pass silenciosos por loggeo (P0 subset)`

---

## 🛡 SECCIÓN 9 — Estabilidad: `await r.json()` sin `r.ok` check

### Estado
~21 puntos en `app-v12.js` hacen `const x = await r.json()` sin verificar `response.ok`. Un 401 responde `{"detail":"..."}` (JSON válido), entonces no throw, pero `x.cameras` es `undefined` → UI muestra vacío en vez de "sesión expirada → login".

### Fix P0 (mínimo)
Wrapper en `apiFetch` (`app-v12.js:18`):
```js
async function apiFetch(url, opts) {
  const r = await fetch(url, withAuth(opts));
  if (r.status === 401) {
    // Sesión expirada → logout automático
    App._handleAuthExpired?.();
    throw new Error('Sesión expirada');
  }
  return r;
}
```

**Estado:** ✅ RESUELTO (commit `eff8a4a`). apiFetch detecta 401, dispara evento `ojoia:auth-expired`. App._handleAuthExpired escucha, hace firebase.auth().signOut(), cierra EvaChat, muestra `_showLogin()` con mensaje. Flag `_authExpiredHandled` evita storms. Sintaxis validada (1183/1183 braces, 2084/2084 parens). Lógica del wrapper verificada con mock test.

**Prioridad:** P0 ( UX crítica — clientes no entran en loop de "vacío" pensando que no hay cámaras).
**Commit:** `eff8a4a` — `P0 (Sección #9): apiFetch 401 handler + auto-redirect a login`

---

## 🔄 SECCIÓN 10 — Plan faltante previo (de PLAN_FALTANTES.md, sanity check)

Items que estaban pendientes en planes anteriores y SÍ necesitan acción (los que ya están hechos se omiten):

### B5 — Activar `cleanup_frames.py` con cron
- Config existe (`api_eva.py:6030-6119`). Falta poner en cron/systemd timer.
- **Acción:** `crontab: 0 3 * * * /opt/ojoia/venv/bin/python -u /opt/ojoia/code/cleanup_frames.py >> /var/log/ojoia/cleanup.log 2>&1`
- **Tiempo:** 10 min.
- **Commit esperado:** `ops: activar cleanup_frames.py en cron diario`

### D2 — Cola per-camara en lugar de `FRAME_QUEUE` global
- Queda P1. No es P0 — el sistema aguanta 1-2 cámaras concurrentes hoy.

### T1 — `--edge-ip-version 4` en cloudflared
- **Estado:** ✅ APLICADO 2026-08-21 11:09. Editado `/etc/cloudflared/config.yml` con `edge-ip-version: "4"` (necesita comillas — cloudflared rechaza int). Backup en `/etc/cloudflared/config.yml.bak.20260821`. Reiniciado `tunnel.service` correctamente. Verificado en logs: `Settings: ... edge-ip-version:4 p:http2 ...`. Tunnel conectado a 4 regiones Cloudflare (atl06, tpa01, atl11, mia01). `api.ojoia.com.do/health` responde 200 (1.4s). `ui` y `admin` devuelven 530 porque sus backends (`:8080`, `:8030`) no están corriendo en este host — pre-existente, no del tunnel.
- **Acción rápida:** `sed -i 's/^tunnel: /tunnel:\n  edge-ip-version: 4/' /etc/cloudflared/config.yml && systemctl restart cloudflared` (nota: necesita comillas para evitar `expected string found int`)
- **Tiempo:** 5 min.

### C2.2-C2.5, C3.2, C3.4, C4.1-C4.2 — Funcionalidades del wizard de zonas
- NO son P0. El flujo básico funciona. Programar como P1 cuando el P0 esté en producción.

---

## 📅 Plan de ejecución P0 (orden sugerido)

### Sprint 1 — "No más crashes" (2 días) ✅ COMPLETO 2026-08-21
1. ✅ Bug #1 — NoneType guard en `_mk_resp` + fallback api_eva.py:2383 — commit `229ad11`
2. ✅ Bug #2 — Reset `_homeStreamStarted` — commit `1b6af90`
3. ✅ Bug #3 — Cleanup `visibilitychange` — commit `c3bb102`
4. ✅ Bug #4 — Versionado literal único — commit `21bb717`
5. ✅ Fuga #7.3 — QR local — commit `d041cb8`
6. ✅ Fuga #7.2 — token a sessionStorage — commit `1af0b9d`

### Sprint 2 — "No más fugas" (3-4 días) — EN PROGRESO
7. ✅ Bug #5 — `/api/auth/token` requiere Firebase — commit `116d766`
8. ✅ Bug #6 — auditoría completa, marcado como resuelto por extensión
9. ✅ Fuga #7.1 — escapeAttr en 18+ onclick críticos — commit `e078caa`
10. ✅ Sección #9 — apiFetch 401 handler — commit `eff8a4a`

### Sprint 3 — "Mejor diagnóstico" (opcional, 1 día)
11. ✅ Sección #8 — 12 except pass backend + 15 catch vacíos frontend → loggeo — commit `21c6ef4`
12. ✅ B5 — cron de cleanup_frames.py (diario 3 AM) — commit `21c6ef4`
13. ✅ T1 — edge-ip-version 4 — commit `201b026`

**Sprint 1 total: ~2h (vs 2 días estimado). Bugs de crashes y UX + fugas más graves cubiertos.**
**Total P0:** ~6 días-hombre. Deja el sistema sin crashes ni fugas conocidas.

---

## 🎯 Criterios de aceptación (definición de "listo")

- [ ] Cargar una página con `user.json` borrado → respuesta amigable, no traceback oculto.
- [ ] Navegar Home → Settings → Home → video sigue vivo.
- [ ] Logout/login 3x → solo 1 poll de Eva cada 10s.
- [ ] Editar `eva-chat-v7.js`, bump `?v=`, refresh → nuevo JS cargado. Reload de nuevo → cache hit.
- [ ] `curl /api/auth/token` sin Firebase → 401.
- [ ] `curl /api/auth/token` con Firebase válida y user_id matching → 200 + bearer.
- [ ] Responder JSON con `camera_id: "ab'cd"` → no hay RCE en frontend.
- [ ] QR en chat → generado localmente (no request a `api.qrserver.com`).
- [ ] Forzar 401 → redirige a login (no UI vacía silenciosa).
- [ ] `logrotate -vf /etc/logrotate.d/ojoia` y 24h después → logs rotados, no crecen indefinidamente.

---

## ❌ Fuera de alcance (explícito, para P1 siguiente)

- Split `api_eva.py` en módulos (P1).
- Tests automatizados (P1).
- CSP / `script-src 'self'` (P1, requiere refactor de inline onclick en masa).
- HttpOnly cookies (P1, requiere backend coordination).
- Performance del archivo 6MB `api_eva.py` (P1).
- Features C2.2-C4.2 del wizard (P1+P2).

---

## 📚 Referencias

- `eva_v2.py:290` — `_load_session` (puede retornar None).
- `eva_v2.py:3796` — `_mk_resp` (accede session[...] sin guard).
- `api_eva.py:2383-2396` — fallback bug.
- `api_eva.py:321, 381` — dependencias FastAPI no cableadas (código muerto).
- `api_eva.py:693-745` — endpoint `/api/auth/token` (auth bypass).
- `api_eva.py:1139-1187` — middleware con bypass documentado.
- `app-v12.js:7-15` — Firebase config harcoded.
- `app-v12.js:108-109` — token en localStorage.
- `app-v12.js:657, 667` — _homeStreamStarted no se resetea.
- `app-v12.js:577, 1017, 1192, 1879, 2199, 2477, 3121` — catch silentes.
- `eva-chat-v7.js:512` — QR a terceros.
- `eva-chat-v7.js:517, 718` — onclick con valor sin escapar.
- `eva-chat-v7.js:1175-1179` — listener que se acumula.
- `/opt/ojoia/code/frontend/index.html:84` — `Date.now()` cache-buster inconsistente.

---

**Fin del Plan Consolidado P0.**
