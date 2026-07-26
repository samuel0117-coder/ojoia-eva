# OjoIA — Plan de Mejoras para Producción
**App de producción analizada:** `app-v12.js` (4.379 líneas / 236 KB), `eva-chat-v5.js` (1.174 líneas / 68 KB), `index.html` (87 líneas), `app.css` (1.107 líneas / 48 KB), `sw.js` (54 líneas), `deploy.py`
**Fecha:** 2026-07-26 · **Estado:** propuesta para ejecutar por fases

---

## 1. Resumen ejecutivo

La app es una PWA de 4 tabs (Eva, Cámara, Eventos, Ajustes) sobre Firebase Hosting + API propia (`api.ojoia.com.do`). Funciona en producción con 1 cliente real, pero tiene **deuda técnica concentrada en 5 áreas**:

| Área | Riesgo | Impacto |
|---|---|---|
| Autenticación de API | **P0 — Crítico** | Cualquiera con un `user_id` (visible en URLs/logs) puede leer eventos, cámaras y chat de ese usuario. Ningún request lleva token. |
| Triple implementación de chat | P1 — Alto | `EvaChat` (v5), `_minimalEva*` y `_evaMsgs` (wizard) son 3 chats distintos con estados separados que se desincronizan. |
| Funciones rotas silenciosas | P1 — Alto | `_showToast` no existe (los toasts de auto-avance y deep-link nunca se ven); `_scheduleAutoAdvance` muestra "NaN s". |
| XSS por `innerHTML` | P1 — Alto | Descripciones de eventos, nombres de cámara y respuestas de Eva se interpolan sin escapar en varios módulos. |
| Polling desordenado | P2 — Medio | Hasta 6 `setInterval` simultáneos (2s, 5s, 10s, 10s, 15s, 30s) sin coordinación ni backoff. |

**Esfuerzo total estimado:** ~12–16 días de trabajo en 5 fases. Las fases 0 y 1 (seguridad + bugs rotos) son las únicas bloqueantes para llamar a esto "producción".

---

## 2. Bugs confirmados (arreglar primero — sin discusión de diseño)

| # | Bug | Evidencia | Fix |
|---|---|---|---|
| B1 | `App._showToast` **no está definido**. Se llama en 7 lugares (deep-link `#events`, auto-advance, navegación entre eventos). Solo existe `_toast`. Resultado: los toasts con botón "Ver" **nunca aparecen**. | `app-v12.js:519` llama `App._showToast(...)` sin `?.`; única def: `_toast` en línea 619. | Crear `_showToast(msg, ms, {action})` que envuelva `_toast` con botón de acción, o renombrar todas las llamadas a `_toast`. |
| B2 | `_scheduleAutoAdvance` muestra `NaN s` en el toast: usa `this._eventAutoAdvanceTimeout` que **nunca se inicializa** (es `undefined`). | `app-v12.js:3254` | Inicializar `_eventAutoAdvanceTimeout = 2000` como propiedad del objeto App. |
| B3 | CSS inline de `index.html` sin cerrar llave: `[id^="event-modal-"]::-webkit-scrollbar-thumb:hover{background:rgba(128,128,128,0.4)</style>` — falta `}`. El resto del bloque inline queda a merced del parser. | `index.html:17` | Cerrar la llave y mover el bloque a `app.css`. |
| B4 | Tag `__daily_summary__` visible como burbuja del usuario en el chat + duplicación de mensajes al hacer merge (firma local usa texto visible, backend guarda el tag crudo). | `api_eva.py:1603` guarda `message` crudo; `eva-chat-v5.js:231` no mapea tags. | Frontend: mapear `__*__`→texto visible en render y en la firma del merge. Backend: guardar el texto visible. |
| B5 | `_loadCamVigilance` es un no-op (`return;`) pero se llama desde `_pageHome` y `_switchHomeCamera` — código muerto que confunde. | `app-v12.js:1341` | Eliminar función y sus llamadas. |
| B6 | `doLogin`: tras el error "usuario no registrado", el botón queda deshabilitado y el texto cambia a registro por timeout de 1s — el usuario puede quedarse sin poder reintentar. | `app-v12.js:272-275` | Rehabilitar el botón en el `setTimeout`. |
| B7 | Iconos `/img/icon-*.png` **no existen en el repo ni en git**; se sirven desde Firebase solo porque versiones viejas los incluyeron. Si se limpia, las notificaciones y el ícono de la PWA quedan rotos. | `ls img/` no existe; `curl https://ojoia.com.do/img/icon-192.png` → 200. | Regenerar los 7 iconos desde el SVG del logo y commitearlos en `img/`. |

---

## 3. Plan por módulo

### M0 — Infraestructura: PWA, Service Worker, Deploy

**Estado actual:** `sw.js` solo maneja push (no cachea nada → la app no abre offline). `deploy.py` sube todo el directorio a Firebase Hosting vía API REST con la key en `/home/sam/Downloads/firebase-key.json`.

| # | Mejora | Detalle | Esfuerzo | Prioridad |
|---|---|---|---|---|
| M0.1 | **App-shell cache en `sw.js`** | Precachear `index.html`, `app-v12.js`, `eva-chat-v5.js`, `app.css`, iconos y manifest con estrategia *network-first, cache-fallback*. Bump del `CACHE_NAME` por versión (ya se hace manualmente). Resultado: la app abre sin red. | M | P1 |
| M0.2 | **Mover `firebase-key.json` fuera de Downloads** | `deploy.py:6` lee `/home/sam/Downloads/firebase-key.json` (frágil: cualquier limpieza de Downloads rompe el deploy). Mover a `/opt/ojoia/secrets/` con permisos `600` y actualizar el path. | S | P1 |
| M0.3 | **Eliminar rewrite muerto `/api/**`** | `deploy.py:30` reescribe `/api/**`→`api.ojoia.com.do` pero el frontend nunca lo usa (resuelve `this.API` por health-check). Quita el rewrite o, mejor, **úsalo**: que el frontend llame a rutas relativas `/api/*` y desaparezca el problema de mixed-content y el health-check. Decisión recomendada: mantener health-check (más robusto al tunnel caído) y borrar el rewrite. | S | P2 |
| M0.4 | **Rollback documentado** | Firebase conserva versiones; documentar el comando de rollback (`firebase hosting:releases:list` / rollback por consola) en el propio `deploy.py` como comentario + opción `--rollback <version>`. | S | P2 |
| M0.5 | **Cache-buster automático** | Hoy el `?v=20260725a` se edita a mano (3 lugares en `index.html`). Que `deploy.py` lo genere (timestamp) y lo parchee en `index.html` antes de subir. Elimina olvidos. | S | P1 |

### M1 — Shell y Auth (`index.html`, `init`, `_startAuth`, `doLogin`)

**Estado actual:** Firebase Auth (email/password) → `/auth/firebase/verify` → `user_id` guardado en `localStorage`. Después de ese verify, **ningún request lleva credencial**.

| # | Mejora | Detalle | Esfuerzo | Prioridad |
|---|---|---|---|---|
| M1.1 | **🔴 Token en todos los requests** | Añadir `Authorization: Bearer <idToken>` en `apiFetch` (usando `firebase.auth().currentUser.getIdToken()` con caché de 50 min). Backend: middleware que verifica el token y **deriva el `user_id` del token**, ignorando el `?user_id=` del query. Sin esto, la app es pública para quien adivine un `user_id` (28 chars, visible en URLs del chat, logs de nginx/CF, analytics). Es el agujero #1. | L | **P0** |
| M1.2 | **Rotación/refresh del token** | `getIdToken(true)` si el request devuelve 401, con un solo retry. Hoy un token expirado rompe la sesión silenciosamente. | S | P1 |
| M1.3 | **Eliminar `ojoia_uid` como fuente de verdad** | `localStorage.getItem('ojoia_uid')` se usa como fallback cuando el verify falla (`_startAuth:158`). Un usuario puede editarlo y suplantar. Con M1.1, el fallback debe ser "pedir login", no "confiar en localStorage". | S | P1 |
| M1.4 | **Rate limiting en backend** | `/auth/firebase/verify`, `/api/chat/eva/message` y `/ingest` sin límite. Añadir bucket por IP/usuario (p.ej. 60 req/min en chat, 10/min en verify) para frenar abuso y costos de Qwen. | M | P1 |
| M1.5 | **Headers de seguridad** | Añadir en `deploy.py` config headers: `Content-Security-Policy` (script-src 'self' gstatic + unsafe-inline temporal), `X-Content-Type-Options: nosniff`, `Referrer-Policy: strict-origin-when-cross-origin`. Quitar `unsafe-inline` del CSP cuando se migren los `onclick` inline (ver M2.4). | S | P2 |
| M1.6 | **Fix login UX** | B6 (botón deshabilitado) + validación de email antes de llamar a Firebase (ahorro de roundtrips) + `autocomplete="username"`/`current-password` correctos en registro. | S | P2 |
| M1.7 | **Mover el CSS inline de `index.html` a `app.css`** | El bloque `<style>` de la línea 17 (además de tener B3) se descarga en cada navegación sin caché. | S | P2 |

### M2 — Tab Eva (`eva-chat-v5.js` + `_minimalEva*` + wizard `_evaMsgs`)

**Estado actual:** 3 implementaciones de chat coexisten:
1. `EvaChat` (v5, el principal) con sync remoto, storage cross-tab, carruseles y heatmaps.
2. `_minimalEva*` en `app-v12.js` (un chat paralelo "mínimo" con su propio `_minimalEvaMessages` y su propio render).
3. `_evaMsgs` / `_renderEvaChat` (wizard de instalación de cámara, otro render independiente).

El wizard y el minimal duplican lógica, compiten por `#eva-chat-container` y son la fuente de los "chats fantasma" que el propio código comenta.

| # | Mejora | Detalle | Esfuerzo | Prioridad |
|---|---|---|---|---|
| M2.1 | **Consolidar a UN chat** | Eliminar `_minimalEva*` y `_renderEvaChat`/`_evaMsgs`. El wizard de instalación debe ser una **fase de `EvaChat`** (el backend ya devuelve `next_phase: WIZARD_QR` etc., y `sendMessage` ya pinta QR y botón de zonas — líneas 442-451 de `eva-chat-v5.js`). `newCamera()` debe llamar `EvaChat.init` + enviar `__new_camera__` como intent. Resultado: −250 líneas, cero estados duplicados. | L | P1 |
| M2.2 | **B4: mapeo de tags `__*__`** | Helper `_userVisibleText()`: `__daily_summary__`→"Resumen del día", `__yesterday_summary__`→"Resumen de ayer", `__adjust_protection__`→"Ajustar protección", `__new_camera__`→"Instalar cámara nueva". Aplicarlo (a) en el render de burbujas de usuario, (b) en la firma del merge (líneas 909/956) normalizando `content` antes de comparar, (c) en el backend al guardar (`api_eva.py:1603`). | S | **P0** |
| M2.3 | **Render incremental** | `render()` recompone **todo** el HTML del chat en cada llamada (incluyendo imágenes del carrusel y heatmaps → recarga de red y re-dibujo de canvas en cada sync de 10s). Cambiar a: append de mensajes nuevos + re-render del último bloque solo si cambió. Clave: asignar `id="msg-<ts>-<i>"` por burbuja y solo insertar los nuevos. Es la mayor ganancia de performance del chat. | L | P1 |
| M2.4 | **Eliminar `onclick` inline con datos interpolados** | Carrusel (`eva-chat-v5.js:634`): `onclick="EvaChat.openEventDetail('${evt.event_id}')"`. Si un `event_id`/`desc` trajera comillas, se rompe el HTML y es vector de XSS. Cambiar a `addEventListener` tras crear el nodo, o `data-event-id` + delegación en el contenedor. | M | P1 |
| M2.5 | **Polling adaptativo** | `_startRemoteSync` hace `GET /history?limit=1` cada 10s siempre (lee `user.json` completo del disco en el backend). Mejoras: (a) backend — endpoint `/api/chat/eva/history-ts` que devuelva solo el `last_message_at` (leer 1 campo, no el archivo entero); (b) frontend — backoff: 10s con actividad, 30s tras 2 min sin cambios, pausa total cuando `document.hidden` (ya pausa, mantener). | M | P1 |
| M2.6 | **Backend: 1 lectura de `user.json` por GET** | `get_eva_chat_history` abre y parsea `user.json` dos veces (línea 1241 para mensajes, 1304 para el `ts`). Leer una sola vez y reutilizar `ud`. | S | P1 |
| M2.7 | **Backend: escritura con lock** | `chat_eva_message` guarda `user.json` en un `threading.Thread` sin lock (`api_eva.py:1622-1629`). Dos mensajes simultáneos se pisan el archivo (pérdida de mensajes, JSON corrupto). Añadir un `threading.Lock` global por `user_id` (dict de locks) alrededor de read-modify-write. Mismo problema en `save_eva_chat_message`. | M | **P0** |
| M2.8 | **AbortController en `sendMessage`** | Si el usuario envía 2 mensajes seguidos, las respuestas pueden llegar desordenadas. Cancelar el fetch anterior al enviar uno nuevo. | S | P2 |
| M2.9 | **Reducir `scrollToBottom`** | Dispara 5-6 timers encadenados (hasta 1.65s de reflows). Con render incremental (M2.3), 2 llamadas bastan. | S | P2 |
| M2.10 | **Quitar redundancia greeting vs "Resumen del día"** | El greeting (`_buildDailyBrief`) y el chip llaman a stats similares. El chip debería reutilizar el último brief si tiene <15 min (guardado en `this._lastBrief`), sin nueva llamada al backend. | S | P2 |

### M3 — Tab Home/Cámara (`_pageHome`, streaming, YOLO, timeline)

**Estado actual:** Grid de 1/2/4/8/16 cámaras con MJPEG por `<img>` (5 fps) + polling de metadata YOLO cada 2s por cámara + watchdog de reconexión (buena implementación de robustez). Timeline de 45 min con hasta 1000 frames.

| # | Mejora | Detalle | Esfuerzo | Prioridad |
|---|---|---|---|---|
| M3.1 | **Metadata YOLO: 1 request por ciclo, no por cámara** | `_fetchHomeFrames` → `_refreshYoloOnly` → `_fetchYoloMetadata` hace 1 `GET /frames/latest` **por cámara** cada 2s (16 cámaras = 480 req/min). Añadir endpoint batch `/frames/latest-batch?camera_ids=a,b,c` (1 request por ciclo). | M | P1 |
| M3.2 | **MJPEG solo para tiles visibles** | Con 16 tiles se abren 16 conexiones MJPEG simultáneas (cada una ~5 fps × ~20 KB = 1.6 MB/s). Opciones: (a) tiles pequeños usan JPEG estático a 1 fps y solo el tile enfocado/grande usa MJPEG; (b) IntersectionObserver para pausar streams fuera de pantalla. Ahorra ancho de banda del servidor y del móvil. | M | P1 |
| M3.3 | **Canvas YOLO sin resize por frame** | `_drawYoloBoxes` reasigna `canvas.width/height` en cada frame (resetea el contexto y fuerza realloc). Solo redimensionar si cambió el tamaño del `<img>` (guardar `cw/ch` previos). Reduce GC churn en móviles. | S | P2 |
| M3.4 | **WebSocket/SSE para metadata** | A mediano plazo, el polling de 2s debería ser un canal push (el backend ya procesa YOLO en tiempo real). Fase posterior; no bloquea. | L | P2 |
| M3.5 | **Timeline: paginar y liberar** | `_openCameraTimeline` carga metadata de hasta 1000 frames y las imágenes se piden con `?_=Date.now()` (sin caché). Mejoras: ventana de ±20 frames cacheados, liberar `img.src` de frames lejanos, y `loading="lazy"`. | M | P2 |
| M3.6 | **Eliminar `alert()` nativo** | `_saveRecentClip` (línea 1697) y `deleteCamera` usan `alert()`/`confirm()` nativos — bloquean el hilo y rompen el look&feel. Sustituir por el toast/modal propio (tras B1, `_showToast` con action sirve para confirmaciones). | S | P1 |
| M3.7 | **`_fetchViewerGrid` sin recargar imagen** | Cada 5s recompone el card con el grid `base64` (~180 KB) aunque no haya cambiado. Backend devuelve `frames` (count): solo actualizar el `<img>` si `frames` cambió. | S | P2 |
| M3.8 | **Estado de cámara offline honesto** | `_refreshCamStatus` existe pero el badge "En vivo/Offline" del tile solo se fija en el render inicial. Refrescar también el `data-home-status` por cámara en cada ciclo. | S | P2 |

### M4 — Tab Eventos (`_pageEvents`, lista, `_openEvent`, autoplay)

**Estado actual:** Lista con filtros (24h/alertas/todos/cámara), poll cada 10s, modal con autoplay de frames + auto-advance + navegación prev/next. Trabajo reciente: viewer simple para centinela, `<video>` si hay `video_file`, banner "1 frame" para vigilance.

| # | Mejora | Detalle | Esfuerzo | Prioridad |
|---|---|---|---|---|
| M4.1 | **B1+B2 (toasts y NaN)** | Cubiertos en §2. Sin `_showToast` no hay aviso de "nuevo evento" ni "próximo evento en Xs". | S | **P0** |
| M4.2 | **Escapar todo el HTML de la lista** | `_eventRowHtml` interpola `camName`, `enrichedDesc`, `evtTime` sin escapar (`_pageEvents:2769-2771`). Una descripción de Qwen con `<` rompe el layout o inyecta HTML. Pasar por `escapeHtml` (ya existe en EvaChat; llevar uno a App). | S | P1 |
| M4.3 | **Paginación real** | `_loadMoreEvents` reemplaza toda la lista (limit 50→100) y vuelve a renderizar todo. Cambiar a cursor (`before_ts=`) con append, y `IntersectionObserver` para infinite scroll en vez de botón. | M | P2 |
| M4.4 | **Filtro por cámara en backend** | El filtro por cámara se hace en el cliente tras pedir 50 eventos (línea 2631): si los 50 más recientes son de otra cámara, la lista sale vacía aunque haya eventos de la elegida. Pasar `camera_id` al endpoint. | M | P1 |
| M4.5 | **Virtualizar thumbnails** | El loop de línea 2655 carga todas las thumbs tras el render. Con listas largas, usar `loading="lazy"` en el `<img>` directo del row (ya hay `thumbHtml` con `<img>`: solo añadir el atributo) y quitar el segundo loop de asignación. | S | P2 |
| M4.6 | **Eventos centinela: no mezclar con análisis de Eva** | El brief del chat y `/api/user/events` mezclan `vigilance_alert` (disparos YOLO de 1 frame) con `evt_*` (análisis Qwen de 16 frames). En la lista, añadir filtro/badge "🛡️ Centinela" y por defecto el filtro "Alertas" debe priorizar `violation`/`attention` sobre `vigilance_alert`. En el brief del chat, excluir vigilance del "último análisis". | M | P1 |
| M4.7 | **Prefetch del siguiente evento** | En auto-advance, durante la pausa de 1.5-2s, hacer `fetch` del detalle del siguiente evento (HEAD o GET con abort) para que el cambio sea instantáneo. | S | P2 |
| M4.8 | **`_pauseEventAutoplay` simplificado** | La expresión de `total` (línea ~3280) es ilegible y frágil con varios eventos en el Map. Sustituir por `this._eventFrameTotal?.[eventId] || 0`. | S | P2 |

### M5 — Tab Ajustes (`_pageSettings`, config de cámara, zonas, vigilancia, cuenta)

**Estado actual:** Perfil/plan, config ESP32 (brillo, contraste, rotación, calidad, fps, LED, cooldown), editor de zonas en canvas, editor de reglas de protección, tamaño de grid, suscripción/comprobantes, soporte, PWA install.

| # | Mejora | Detalle | Esfuerzo | Prioridad |
|---|---|---|---|---|
| M5.1 | **Editor de zonas: touch real** | `_openZoneEditor` simula mouse events desde touch (líneas 1951-1973) — pierde precisión y multi-touch. Reescribir con Pointer Events (`pointerdown/move/up`) que unifican mouse/touch/stylus. | M | P2 |
| M5.2 | **Validación de horarios** | `_saveVigilanceSettings` acepta cualquier texto en `vig-open`/`vig-close`. Validar `HH:MM` antes de guardar (backend también) para no romper `_is_vigilante_mode`. | S | P1 |
| M5.3 | **Cooldown: unidades coherentes** | El slider dice "min" (5-60) pero `_save_vigilance_event` lo trata como **segundos** (`cooldown_sec = int(cam_cfg.get("cooldown_min", 60))`). Confirmar unidad y renombrar campo o convertir. Hoy el usuario cree poner 5 min y el backend espera 5s→60s. Riesgo de spam de notificaciones. | S | **P0** (verificar) |
| M5.4 | **Config ESP32: cola visible** | `_sendCamCmd` ya maneja "cámara offline — comando en cola" (toast warning). Añadir una sección en la página de config que liste los comandos pendientes para que el usuario vea qué falta por aplicar. | M | P2 |
| M5.5 | **Comprobante de pago: validar tipo/tamaño** | El upload de comprobante (línea ~3807) valida solo "hay archivo". Limitar a imagen ≤5 MB y avisar antes de subir. | S | P2 |
| M5.6 | **Settings sin flash de contenido viejo** | `_pageSettings` pinta el HTML con datos del perfil aunque las llamadas fallen (perfil `{}` → plan "Fundador" ficticio). Mostrar skeleton hasta tener datos o error explícito. | S | P2 |

### M6 — Viewer modal y push (`openViewer`, `sw.js`, `_initPush`)

**Estado actual:** Trabajo reciente: SW abre deep-link completo, listener `message` en la app, links de backend a `#cameras?event=`. El viewer es un modal MJPEG + grid.

| # | Mejora | Detalle | Esfuerzo | Prioridad |
|---|---|---|---|---|
| M6.1 | **Push: dedupe por `tag`** | Todos los FCM usan `tag: "violation"` (orchestrator) o `ojoia-alert` (sw.js default). Alinear: usar `tag = event_id` para que 3 alertas del mismo evento no apilen 3 notificaciones (Android las reemplaza por tag). | S | P1 |
| M6.2 | **Push: deep-link unificado** | Hoy hay 3 formatos según tipo (`#cameras?event=`, `#eva?alert=`, `#events?event=`). Unificar a uno solo (`#event/<id>`) y que `_handleEventDeepLink` decida la vista por `event_type` (vigilance→cámara+modal; violation→events+modal; daily_summary→chat). Menos ramas, menos bugs. | M | P2 |
| M6.3 | **Token FCM: limpieza de tokens muertos** | `_send_vigilance_fcm` y `send_fcm_notification` nunca eliminan tokens inválidos (FCM devuelve `UNREGISTERED`). Marcar y borrar tokens muertos en `user.json` para no inflar la lista ni los logs. | M | P2 |
| M6.4 | **Viewer: liberar stream al cerrar** | `closeViewer` resetea flags pero el `<img>` MJPEG sigue en el DOM oculto (el navegador mantiene la conexión). Hacer `imgEl.src = ''` al cerrar para cortar la conexión. | S | P1 |
| M6.5 | **Permiso de notificaciones con contexto** | `_initPush` pide permiso al entrar (frío). Pedirlo tras el primer evento/alerta relevante con un mensaje previo ("¿Quieres que te avisemos?") — sube la tasa de aceptación. | S | P2 |

---

## 4. Seguridad — programa transversal

| # | Tema | Estado actual | Objetivo | Prioridad |
|---|---|---|---|---|
| S1 | **AuthN de API** | Sin tokens; `user_id` por query param (ver M1.1). | Bearer token verificado en middleware, `user_id` derivado del token. | **P0** |
| S2 | **XSS** | `innerHTML` con datos de Qwen/YOLO/usuario sin escapar en ~10 puntos (lista de eventos, carrusel, chat, home). | Política: todo dato externo pasa por `escapeHtml()` antes de interpolar; onclick inline → addEventListener. Después, endurecer CSP (M1.5). | P1 |
| S3 | **Firebase API key expuesta** | `firebaseConfig` en el cliente (normal en web). No es secreto, pero **obliga** a: reglas de Auth correctas, App Check opcional, y no confiar nunca en el `user_id` del cliente (vuelve a S1). | Documentar + App Check si hay abuso. | P2 |
| S4 | **Escritura de `user.json`** | Sin lock, thread daemon (M2.7). Riesgo de corrupción = pérdida de cuenta/config. | Lock por usuario + escritura atómica (tmp + rename). | **P0** |
| S5 | **Endpoints admin** | `/admin/` con session token propio (`admin_config.json`). Revisar que no sea enumerable y que el token rote; no compartir el mecanismo con usuarios finales. | Auditoría + rate limit. | P1 |
| S6 | **CORS** | Frames se sirven con `Access-Control-Allow-Origin: *` (`api_eva.py:2093`). Con S1, restringir a `https://ojoia.com.do`. | CORS estricto tras M1.1. | P1 |
| S7 | **Datos en localStorage** | `ojoia_uid`, historial de chat, session ids sin cifrar. Riesgo bajo (dispositivo propio) pero el chat puede contener descripciones sensibles. | Con S1, dejar solo lo no sensible; botón "Limpiar conversación" ya existe (bien). | P2 |
| S8 | **Secretos en el repo** | `firebase-key.json` se usa desde Downloads (deploy) y `/home/sam/ai_system/` (backend). Verificar `.gitignore` y que ninguna key esté commiteada (`git ls-files | grep key` — hoy solo `favicon.ico`, bien). | Mantener + documentar ubicación canónica `/opt/ojoia/secrets/`. | P1 |

---

## 5. Performance — programa transversal

| # | Tema | Estado | Objetivo | Prioridad |
|---|---|---|---|---|
| P1 | **Carga inicial** | 236 KB JS + 68 KB chat + 48 KB CSS sin minificar, sin code-split; Firebase JS ×3 desde CDN bloqueando. | Minificar en `deploy.py` (terser/esbuild), `defer` en scripts de Firebase, y lazy-load de `eva-chat-v5.js` solo al abrir la tab Eva. | P1 |
| P2 | **Red por polling** | Ver M2.5, M3.1, M3.2: con 16 cámaras, ~800 req/min entre frames, metadata, stats, sync. | Batch + SSE/WS + backoff. Objetivo: <100 req/min en idle. | P1 |
| P3 | **Imágenes** | `frame_b64` (22 KB) se envía en el detalle del evento aunque haya `frames[]` (que se cargan aparte). Omitir `frame_b64` cuando `frames.length>0`. Grid del viewer sin delta (M3.7). | P2 |
| P4 | **GC en canvas** | M3.3 (resize por frame) + recreación de heatmaps por render (M2.3). | Reusar contextos. | P2 |
| P5 | **`app.css`** | 1.107 líneas sin minificar, sin purgar (hay clases de versiones viejas del chat). | Minificar + purgar clases huérfanas tras M2.1. | P2 |

---

## 6. Fases de ejecución

### Fase 0 — Seguridad crítica (2-3 días) — **bloqueante**
1. S4/M2.7: lock + escritura atómica de `user.json`.
2. M5.3: verificar y corregir unidad de `cooldown_min`.
3. S1/M1.1+M1.2: Bearer token en `apiFetch` + middleware de verificación en backend + `user_id` derivado del token. Rollout compatible: el backend acepta ambos (token o query) durante 1 semana, luego exige token.
4. B7: regenerar y commitear iconos `img/`.

**Criterio de salida:** ningún dato accesible sin token válido; `user.json` sobrevive a 2 escrituras simultáneas.

### Fase 1 — Bugs visibles (1-2 días)
B1 (`_showToast`), B2 (NaN auto-advance), B3 (CSS), B4 (tags chat), B5 (no-op), B6 (login), M4.2 (escape lista eventos), M2.2 completo, M6.4 (cerrar stream), M6.1 (tag push).

**Criterio de salida:** toasts funcionan, chat sin tags ni duplicados, cero `alert()` nativos en flujos principales.

### Fase 2 — Consolidación del chat (2-3 días)
M2.1 (un solo chat), M2.3 (render incremental), M2.4 (onclick → listeners), M2.5+M2.6 (polling/ts endpoint), M2.10 (brief cache).

**Criterio de salida:** una sola implementación de chat; el sync de 10s no recarga imágenes; CPU del tab Eva en idle ≈ 0.

### Fase 3 — Cámaras y red (3-4 días)
M3.1 (batch YOLO), M3.2 (MJPEG selectivo), M3.3 (canvas), M3.7 (grid delta), M3.8 (badge offline), M4.4 (filtro cámara backend), M4.6 (separar centinela).

**Criterio de salida:** 16 cámaras en home con <150 req/min y <3 MB/min en idle.

### Fase 4 — Pulido de producción (2-3 días)
M0.1 (offline app-shell), M0.5 (cache-buster auto), P1 (minify+defer+lazy), M1.5 (CSP), M4.3 (infinite scroll), M5.1 (Pointer Events), M6.2 (deep-link único), M6.3 (tokens muertos), M0.3 (rewrite), M0.4 (rollback doc).

**Criterio de salida:** app abre offline; deploy sin pasos manuales; Lighthouse PWA ≥ 90.

---

## 7. Decisiones de arquitectura a confirmar antes de ejecutar

1. **S1 (auth de API):** ¿Rollout con período dual (token o query) o corte directo? Hay 1 solo cliente real → el corte directo es viable si se coordina con su re-login.
2. **M2.1 (chat único):** confirmar que el wizard de instalación pasa a vivir dentro de `EvaChat` como fase (el backend ya lo soporta con `next_phase`), y que `_minimalEva` se borra.
3. **M4.6 (centinela):** confirmar la regla de producto: centinela = alerta operativa visible en Eventos con badge propio, pero **fuera** del "último análisis de Eva" del chat.
4. **M3.2 (MJPEG selectivo):** ¿tiles estáticos a 1 fps con MJPEG solo en tile enfocado, o mantener MJPEG en todos con fps reducido? La primera opción ahorra ~90% de ancho de banda.
5. **P1 (minify):** ¿aceptamos un build step mínimo (`esbuild` en `deploy.py`) o seguimos sin build y solo con `defer`? Recomiendo esbuild: una sola dependencia, sin cambiar el flujo de edición.
