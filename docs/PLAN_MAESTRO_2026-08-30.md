# 🎯 Plan Maestro OjoIA — 2026-08-30

> **Origen:** Análisis completo del codebase (api_eva.py 6.5k LOC, orchestrator.py, eva_v2.py,
> eva/*, frontend app-2026.js/chat-2026.js, portal/, billing, infra GPU/docker).
> **Objetivo:** certificar OjoIA para muchos usuarios y muchas cámaras, con eficiencia total,
> manteniendo la promesa del producto: *vigilar lo que realmente importa y notificar solo
> cuando se viola una regla del usuario*.

---

## Visión del producto

OjoIA es un **guardia de vigilancia con IA**: el usuario define en lenguaje natural qué
quiere vigilar; el sistema observa (YOLO filtra → Qwen Vision narra como testigo → se
comparan las reglas del usuario) y **notifica solo cuando una regla se viola**.

Principio rector que guía todo este plan:
> **El modelo es testigo, no juez. Cada falso positivo destruye la confianza; cada
> violación perdida destruye el producto. La precisión de alertas es EL producto.**

---

## Hallazgos clave (verificados 2026-08-30)

### Seguridad
| # | Severidad | Hallazgo | Ubicación |
|---|---|---|---|
| V1 | 🔴 | Password Redis hardcoded como fallback (billing.py ya corregido; **portal.py sigue**) | `portal/portal.py:25` |
| V2 | 🔴 | 2 copias de `firebase-key.json` en el árbol de código + `.git.backup*` | raíz, `ai_system/` |
| V3 | 🟠 | Ingest público: solo se filtra por `camera_id` conocido → inyección de frames falsos / DoS de cola | `/ingest/*` |
| V4 | 🟠 | Auth depende de disciplina manual: si `user_id` va en body, el handler debe llamar `_verify_user_token` manualmente | `enforce_user_auth` |
| V5 | 🟡 | Tokens de 90 días sin rotación ni revocación por dispositivo | `_verify_user_token` |
| V6 | 🟡 | Reportes públicos por URL sin expiración ni firma | `/api/reportes/*` |
| V7 | 🟡 | README expone ruta de token GitHub personal | `README.md:137` |

### Arquitectura / escalabilidad (bloqueantes para "muchos usuarios")
1. `user.json` read-modify-write sin lock en ~18 sitios → corrupción/pérdida con concurrencia.
2. Todo el estado en filesystem local → sin réplicas, sin failover, sin escala horizontal.
3. FRAME_QUEUE in-process → reinicio = pérdida de frames; ingest no escala a otro proceso.
4. `_sessions` de Eva en memoria → pérdida al reiniciar, no escala entre procesos.
5. Cooldown de notificaciones en memoria → tormenta de notificaciones tras reinicio.
6. **Keyword matching frágil** en `_detect_attention_hits` (substring contra el relato) →
   falsos positivos por negación ("no se observa que X" → dispara). Crítico: destruye confianza.
7. Duplicación masiva: frontend v12/v13/2026 + eva-chat v5/v7/v8, orchestrator ×3, gpu1_image_server ×2.
8. Sin tests, sin CI, deploy manual.

### Onboarding (primera configuración de cámara)
- Flujo lineal frágil (si el usuario se sale del guion, se atasca).
- Solo contempla ESP32 propio; falta flujo para cámaras IP/RTSP existentes.
- TEST_RULES exige actuación física sin timeout ni opción de saltar.
- ANALYZE usa 1 solo frame para diagnosticar calidad.

---

## 🚀 Sprint A — Fugas inmediatas + deduplicación (1 día)
- [ ] A1: Quitar fallback de Redis password en `portal.py` → fallar ruidosamente. Rotar password.
- [ ] A2: Mover `firebase-key.json` fuera del árbol de código; verificar historial git; limpiar backups.
- [ ] A3: Eliminar duplicados (frontend v12/v13, eva-chat v5/v7, orchestrators paralelos, gpu1 duplicado).
- [ ] A4: Token de ingesta por cámara (`X-Camera-Key`, retrocompatible: obligatorio solo si `camera.json.ingest_key` existe).
- [ ] A5: Quitar ruta de token GitHub del README.

## 🎯 Sprint B — Precisión de alertas (el corazón del producto) (2-3 días)
- [ ] B1: Eliminar keyword-substring como disparador en `_detect_attention_hits`; solo hits
      estructurados de Qwen validados contra la lista exacta de frases del usuario.
- [ ] B2: Verificación de 2ª pasada: candidato de violación → llamada barata a qwen7b
      ("¿realmente ocurrió X? sí/no + evidencia") → solo notificar si confirma.
- [ ] B3: Cooldown persistente (disco/Redis) por cámara+regla + dedup por hash de escena.
- [ ] B4: Feedback loop: correcciones del usuario ("esto no era") ajustan frases automáticamente.

## 🏗 Sprint C — Escala multi-usuario/multi-cámara (1-2 semanas)
- [ ] C1: Capa `user_store.py` (SQLite WAL o lock por usuario + escritura atómica) para el
      estado caliente: user.json, sesiones Eva, cooldowns, FCM tokens.
- [ ] C2: Cola externa (Redis streams o SQLite persistente) para frames; workers desacoplados.
- [ ] C3: Servicio de ingest separado de la API Eva/chat.
- [ ] C4: Rate limit por plan (cablear billing → ingest: frames/seg por cámara).
- [ ] C5: Certificación de carga: N cámaras sintéticas, medir latencia frame→alerta, drops, GPU.
      SLA objetivo: alerta < 90s desde el evento.

## 🧭 Sprint D — Onboarding y prompt de vigilancia (1 semana)
- [ ] D1: Máquina de onboarding tolerante (responder preguntas fuera de guion y volver a la fase).
- [ ] D2: Soporte RTSP/cámara IP (allowlist de hosts, rate limit) además de ESP32.
- [ ] D3: TEST_RULES con timeout + "probar después".
- [ ] D4: Prompt de vigilancia versionado (`prompt_version` en cada evento) para medir regresiones.
- [ ] D5: Reglas negativas en el wizard ("¿qué es NORMAL aquí?") → `owner_notes` desde el día 1.
- [ ] D6: ANALYZE con 3-5 frames (promedio) en vez de 1.

## 🧪 Sprint E — Higiene continua
- [ ] E1: pytest: `_detect_attention_hits` (casos de negación), `_verify_user_token`, ingest queue.
- [ ] E2: CI básico (GitHub Actions): tests + syntax check.
- [ ] E3: Deploy con cache-bust automático por hash de archivo.
- [ ] E4: Alertas operativas: drops de FRAME_QUEUE, disco, errores de Qwen.

---

## Registro de ejecución

| Fecha | Sprint | Commit | Estado |
|---|---|---|---|
| 2026-08-31 | Baseline (qwen38 etc.) + este plan | `79f6f35` | ✅ |
| 2026-08-31 | A1 Redis password portal | `02ff116` | ✅ |
| 2026-08-31 | A2 firebase-key fuera del repo | `16ecc1b` | ✅ |
| 2026-08-31 | A3 deduplicación (~2.9MB) | `08a68e0` | ✅ |
| 2026-08-31 | A4 X-Camera-Key + A5 README | `cb6946f` | ✅ |
| 2026-08-31 | B1+B2+B3 precisión de alertas + cooldown persistente | `0ec02b4` | ✅ desplegado (api-eva reiniciado, health 200) |
| 2026-08-31 | B4 feedback loop auto-ajuste de reglas | `53ee10f` | ✅ desplegado |
| 2026-08-31 | C1 user.json read-modify-write seguro (13 sitios) | `1701557` | ✅ desplegado (test 200/200 concurrencia) |
| 2026-08-31 | C4 rate limit ingest (5fps default) | `0e5653a` | ✅ desplegado |
| 2026-08-31 | C5 script de carga + fix HTTPException tragada | `8e4c10a` | ✅ certificado: 30 cámaras × 1fps, p99 572ms, 0 drops |
| 2026-08-31 | D1 onboarding tolerante + D3 test-rules bugfix + D4 prompt versionado | (commit feat(onboarding)) | ✅ desplegado |
| 2026-08-31 | E1 suite pytest 16/16 | (commit test(E1)) | ✅ |
| — | E2 CI: workflow creado localmente; GitHub exige token con scope `workflow` para pushearlo | — | ⏳ pendiente token |
| — | D2 soporte RTSP/cámaras IP: requiere servicio puller nuevo; va como feature aparte | — | ⏳ planificado |

---

# 🚀 Fase 2 — Escala masiva y cámaras IP (decisión 2026-08-31)

**Decisión del usuario:** orden C2 → D2 → C3. Cámaras remotas por internet.
**Objetivo declarado:** aguantar la mayor cantidad de cámaras posible, recibir
ráfagas sin colapsar, imágenes lo más estables posible (tipo streaming),
aprovechar batch YOLO (hoy 1 imagen/request; el servidor puede hacer batches).

## C2 — Cola externa Redis Streams (PRIMERO)
- `INGEST_QUEUE=redis` (default) / `memory` (fallback si Redis cae).
- Endpoint hace XADD a stream `ojoia:frames` (MAXLEN ~2000, trimming aproximado).
- Workers con consumer group `workers` (XREADGROUP + XACK + XAUTOCLAIM para
  frames huérfanos) → at-least-once, sobrevive reinicios, workers escalables.
- Métricas en /health: pending, lag del group, drops.
- Test: matar API a mitad de ráfaga → frames siguen en Redis; al volver, se procesan.

## D2 — Cámaras RTSP remotas por internet (SEGUNDO)
- `rtsp_puller.py`: proceso que mantiene 1 conexión por cámara, extrae 1 fps
  (configurable), inyecta al pipeline vía la cola Redis (depende de C2).
- Pull directo sobre internet (el comercio abre puerto/DDNS en su router) —
  el agente en-local queda como variante futura para cámaras sin IP pública.
- Anti-SSRF: se bloquean loopback/link-local/metadata (169.254.169.254);
  resto permitido (caso remoto).
- Credenciales: url con user:pass guardada solo en camera.json (permisos 600),
  nunca en logs ni eventos.
- Watchdog: si no llega frame en N min → evento "cámara caída" + push al usuario.
- Wizard: rama en HARDWARE para "cámara IP que ya tengo" → pide URL RTSP →
  probe (primer frame) → muestra imagen → sigue flujo normal (ANALYZE/ZONES...).

## C6 (nuevo, sale del análisis) — Batching YOLO
- Hoy cada frame hace 1 POST /detect al yolo_server. Con ráfagas esto es el
  cuello CPU/GPU más inmediato.
- `yolo_server.py`: endpoint /detect_batch que acepta N imágenes y ejecuta
  un solo forward del modelo (ultralytics soporta batches hasta 32).
- El ingest agrupa frames en micro-batches (ventana 100-200ms o 16 imágenes)
  antes de llamar a YOLO → throughput ×N con la misma latencia perceptible.

## C3 — Servicio de ingest separado (TERCERO)
- `ingest_server.py` mínimo (auth A4 + rate limit C4 + XADD). Sin eva_v2 ni
  orchestrator. systemd `ojoia-ingest.service`, puerto 8006.
- Cloudflared: `/ingest/*` → 8006; resto → 8005. Rollback = 1 línea de config.

# 🚀 FASE 3+ — Escáner de red en ESP32 + Eva con consentimiento (aprobado 2026-09-01)

**Decisiones del usuario:** paraguas legal al registrarse + confirmación 1ª vez;
streaming de terceros NO en v9.3.2 (va en v9.4 aparte); OTA activado en
producción con el mismo bin; OJO-D1C560 designada cámara LAB permanente.

**Cadena certificada 2026-09-01:** main.cpp GitHub (tag `v9.2.2-prod`) =
firmware en producción (D1CC08 reporta v9.2.2) = compilado+flasheado a lab
camera desde este nodo con PlatformIO (boot limpio, frames en pipeline).

## Fases
- **F0 Congelar base** ✅ repo en /home/sam/esp32cam_project, tag v9.2.2-prod,
  bin v9.3.1 huérfano archivado como referencia
- **F1 Endpoints que el firmware YA pide (404 hoy):** /devices/announce,
  /ota/check/{id} (rollout gradual), /ota/firmware.bin (servir bin estable
  autenticado por IP pineada F1-bis)
- **F2 Firmware v9.3.2 escáner:** SSDP M-SEARCH → clasificar vendor →
  probe HTTP MJPEG → POST /devices/scan-results (lista+fotos, máx 5).
  Trigger por el polling de config existente (campo scan_request) — sin
  puertos nuevos. Watchdog 15s. Reglas de privacidad hardcodeadas:
  solo protocolos de cámara, solo IP/marca/puerto, purge 48h.
- **F3 Eva wizard SCAN_WAIT/SCAN_RESULTS:** consentimiento (consent_terms_v2
  en registro + confirmación 1ª vez), resultados con foto, credenciales
  por cámara, probe → registro → flujo normal (ANALYZE F2 → ZONES → reglas).
  Incompatibles: mensaje claro + estado pending_gateway para v9.4.
- **F4 Consentimiento en registro:** checkbox + cláusula versionada.
- **F5 Certificación:** E2E en lab camera + OTA rollout gradual
  (lab → 24h → D1CC08 → resto). Criterios: 0 WDT resets/24h, scan <10s.
- **F6 v9.4 gateway video:** ESP32 extrae 1fps MJPEG de cada cámara
  registrada → /ingest con X-Camera-Key de cada una (A4 ya existe).

| Fecha | Fase | Commit | Estado |
|---|---|---|---|
| 2026-09-01 | F0 congelar base firmware | `7adbd82` (esp32cam) | ✅ |
| 2026-09-01 | F1 announce + OTA rollout — E2E certificado en lab camera (fix bucle infinito OTA) | api_eva | ✅ |

## 📌 AGENDA POSTERIOR (cuando termine el plan F3+ — NO ahora)
1. **Panel admin ojoia.com.do/admin** (base ya existe):
   - Gestionar OTA desde la UI: publicar bin, rollout por cámara, versión
     de cada cámara, historial de actualizaciones
   - "Conectarse a una cámara" para arreglarla: estado vivo (frames, WDT,
     heap, RSSI, IP LAN), aplicar config remotamente, reiniciarla
2. **Panel de detecciones en tiempo real**: stream de alertas/eventos en
   vivo (websocket o polling) con grids y lo que "ve" Qwen en pantalla.
3. (Ya en agenda como F6): gateway de video MJPEG v9.4 en ESP32.

---

# 🏆 FASE 3 — Arquitectura ganadora (aprobada 2026-08-31)

**Diagnóstico medido del nodo:** 2×3090 (GPU0 21.6GB/24.5 usada, GPU1 23.9/24.5
— 96%, riesgo OOM), 20 cores, 61GB RAM. Contenedor qwen3vl8b (8019) unhealthy.
Techo actual: ~50 cámaras estables; el cuello NO es GPU ni red, es YOLO
síncrono + I/O dentro del request HTTP del ingest.

**Decisión del usuario:** todo el plan, sin cambios de firmware (las cámaras
siguen golpeando el mismo dominio/túnel).

## Cambios aprobados
1. **Ingest ultraligero**: /ingest SIN YOLO síncrono → guardar frame (thread
   pool) + XADD Redis + responder ~15-30ms. Trade-off aceptado: siluetas del
   viewer con ~1-2s de lag (poll de latest_yolo.json ya existente).
2. **Workers batched**: XREADGROUP count=16 → 1 forward YOLO por tanda →
   gate yolo_count>0 → grids → Qwen. (Reemplaza el micro-batch de request.)
3. **Qwen**: subir concurrencia de grids, medir p95 con 2/4/8 y fijar semáforo.
4. **Rebalanceo GPU**: YOLO pinned a GPU0, bajar gpu-memory-utilization de
   qwen9b vLLM para colchón, decidir sobre qwen3vl8b unhealthy.
5. **Certificación**: 200 cámaras × 1fps + ráfaga 3× + reinicio a mitad.

**Objetivo:** 150-250 cámaras/nodo con latencia de alerta intacta.

## Certificación objetivo (medir con scripts/load_test_ingest.py)
- 100 cámaras × 1fps sostenido: 0 crashes, drops < 1%, p99 ingest < 1s.
- Ráfaga 3×: cola absorbe y drena; ningún timeout 5xx.
- Reinicio de API en plena ráfaga: 0 frames perdidos (C2), alertas continúan.

## Resultados de certificación de carga (C5, 2026-08-31)

| Escenario | req/s | p50 | p95 | p99 | Drops |
|---|---|---|---|---|---|
| 10 cámaras × 1fps | 10 | 209ms | 231ms | 239ms | 0 |
| 30 cámaras × 1fps | 30 | 472ms | 556ms | 572ms | 0 |
| 5 cámaras × 10fps | 49 | 27ms | 103ms | 160ms | 0 (230× HTTP 429 por rate limit) |

**Decisión sobre C2/C3 (cola externa / ingest separado):** con la carga medida,
el monolito aguanta ~30+ cámaras a 1fps sin drops ni degradación. El verdadero
cuello de botella a escala es la GPU (Qwen por grid): ~16 frames = 1 grid/cámara →
N/16 grids/s. C2/C3 quedan como refactors programables cuando se supere
~50-100 cámaras concurrentes o se requiera alta disponibilidad multi-nodo.
Prioridad real inmediata: D (onboarding/prompts) y E (tests/CI).
| — | OPS pendiente: rotar password Redis y service account Firebase (estuvieron expuestos) | — | ⏳ manual |
| 2026-08-31 | F3 arquitectura ganadora: ingest ultraligero + workers batched | `9501658` | ✅ certificado 100 cámaras × 90s, 0 drops, pending→0 |
| — | OPS F3: rebalanceo GPU (qwen9b 96% VRAM en GPU1) + qwen3vl8b unhealthy + instalar units systemd (sudo) + ruta cloudflared /ingest→8013 | — | ⏳ manual |

## Resultado F3 (certificación 2026-08-31)

| Métrica | Antes (F2) | Después (F3) |
|---|---|---|
| Latencia ingest p50 | ~500ms (YOLO sync) | **~10ms** serial; 141 req/s burst |
| 100 cámaras × 1fps | p99 6.2s, cola presionada | **5601/5601, 0 drops, drenaje a 0** |
| YOLO | 1 forward por frame | **1 forward por tanda de 16** |
| Qwen grids | 1 grid / 16 frames | 1 grid / 32 frames (OJOIA_GRID_SIZE=32) |
| Workers | 4 | 12 (OJOIA_WORKER_COUNT) |
| BUG encontrado y fixeado | — | rescue_stale reclamaba sin procesar → mensajes eternos en pending |
