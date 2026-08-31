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
| 2026-08-31 | Baseline (qwen38 etc.) + este plan | — | ✅ |
