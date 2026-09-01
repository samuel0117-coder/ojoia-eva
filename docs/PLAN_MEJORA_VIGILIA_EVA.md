# Plan de Mejora — Configuración de Cámaras y Vigilancia EVA

> Área: configuración de cámaras + módulo de vigilancia (NO admin — el admin lo trabaja otro agente).
> Fecha: 2026-09-01. Estado: COMPLETADO (Fases 0-4 implementadas y commiteadas).

## Commits
- `6abe8c5` F0 — bugs P0 (round(), persons, drawer editar, wizard 0-zonas, camera.json, cuarentena)
- `869c98b` F1 — placement check score 0-100 (endpoint + wizard ANALYZE)
- `0a25dd6` F2 — zonas asistidas + asignación geométrica determinística
- `c4eaa76` F3 — precisión primero (scene-unchanged, severidad, smoothing, KPIs)
- `1412117` F4 — preservar zonas/frases al confirmar wizard

## Notas de implementación
- El wizard REAL es eva_v2.py (SetupPhase enum); eva_setup_flow.py es legacy
  (se mantiene por compatibilidad, se le añadió WIZARD_PLACEMENT igualmente).
- WIZARD_QR/claim_token del frontend es código defensivo legacy (v14/v15),
  sin productor actual; no rompe nada.
- Tests ejecutados: geometría bbox∩zona (4 casos), severidad (4 niveles,
  flag+keyword), scene-unchanged (6 casos), preservación zonas/apz (2 flujos),
  derive attention_phrases (tasks + concerns fallback).

## Diagnóstico (resumen)

Pipeline actual: ESP32 → ingest → `yolo_worker` (batch YOLO) → gate por frame →
grid 16 frames/cámara → paneles 2×2 → Qwen narra (testigo) → `_detect_attention_hits`
(hits por flag/keyword) → verificación B2 (qwen7b) → cooldown por cámara+frase →
evento en disco (`evt_*.json`) + FCM.

### Bugs críticos
1. `frontend/app-2026.js:3517` — `round()` global no existe (solo `App.round`) →
   `ReferenceError` al soltar el mouse → NO se pueden dibujar zonas en producción.
2. `orchestrator.py:1482` — lee `vision.get("personas")` pero Qwen devuelve `persons`
   → presencia fuera de horario nunca dispara por esa vía.
3. Botón "Editar" del drawer decorativo (solo hay handler para 'draw').
4. `ai_system/orchestrator.py` copia muerta con IndentationError (riesgo de edición errónea).
5. `_is_scene_unchanged` nunca se llama → escenas estáticas re-analizadas con Qwen.
6. Zonas viajan solo como texto al modelo; no hay cálculo geométrico bbox∩zona en el servidor.
7. Setup escribe `business.json` pero el pipeline lee `camera.json` → frases del dueño
   pueden no llegar al orquestador (vigila con defaults del template).
8. Wizard acepta "listo" con 0 zonas.
9. Frontend espera `WIZARD_QR`+`claim_token`; productor de claim_token no hallado en este repo.
10. `suggest-zones` y `test-rule` huérfanos (sin UI).

### Inspiración (playbooks VAD 2026)
- Precision first: >3 alertas/cámara/día = usuario silencia todo.
- Determinístico primero (geometría de zonas + reglas de horario), LLM para verificar/enriquecer.
- Temporal smoothing (hit en 2 grids consecutivos para severidad baja).
- Thresholds/cooldown por zona y cámara, no globales.
- Loop de falsos positivos: "no es alerta" alimenta supresión.
- Anomalía de señal: cámara congelada / drift de confianza.
- Wizard UX: validación por paso, feedback de logro, recuperación simple.

## Fases

### Fase 0 — Estabilizar (bugs P0)
- [x] F0.1 Fix `round()` en app-2026.js (usar App.round con fallback).
- [x] F0.2 Fix `personas`→`persons` en `_detect_attention_hits`.
- [x] F0.3 Implementar handlers "editar" (mover/redimensionar/borrar por clic) en drawer.
- [x] F0.4 Bloquear "listo" en WIZARD_ZONES_DRAW con 0 zonas (backend eva_setup_flow).
- [x] F0.5 Al configurar cámara en el wizard: guardar camera.json con attention_phrases
      derivadas (tareas/preocupaciones) vía camera_builder.save_camera_config.
- [x] F0.6 Cuarentena ai_system/orchestrator.py → ai_system/orchestrator.py.broken (con nota).
- [x] F0.7 Sanity: py_compile de orchestrator.py, api_eva.py, eva/*.py modificados.

### Fase 1 — Verificación de encuadre (placement-check)
- [x] F1.1 Endpoint `POST /api/cameras/{id}/placement-check`: captura ~6 frames en ~8s,
      Qwen evalúa nitidez/obstrucción/brillo/altura/ángulo/cobertura → score 0-100 +
      checklist + consejo accionable.
- [x] F1.2 Integración en chat de Eva: fase WIZARD_PLACEMENT con loop aceptar/reintentar.

### Fase 2 — Zonas asistidas + asignación determinística
- [x] F2.1 Conectar suggest-zones al drawer (botón "Eva sugiere zonas", pre-llenado editable).
- [x] F2.2 Asignación geométrica de zona en yolo_worker: bbox YOLO ∩ rect zona →
      inyectar `zone_name` como dato de sensor al prompt de Qwen (ya no deduce coords).
- [x] F2.3 UI frases de atención por zona en el drawer (campo "¿qué vigilo aquí?").
- [x] F2.4 Backend ya soporta attention_phrases_zones; conectar guardado desde UI.

### Fase 3 — Vigilancia efectiva
- [x] F3.1 Activar _is_scene_unchanged antes de llamar Qwen (comparación con grid previo
      por cámara; si escena igual → evento normal breve sin llamada LLM).
- [x] F3.2 Regla determinística: persona detectada (YOLO) en zona restringida fuera de
      horario → candidato directo (sin esperar keyword); Qwen solo enriquece.
- [x] F3.3 Severidad por frase/zona (baja/media/alta/crítica) + cooldown por zona.
- [x] F3.4 Smoothing 2 grids para severidad baja (hit debe repetirse en grid siguiente).
- [x] F3.5 KPIs por cámara en /grid/status: alertas/día, FPs, drift de confianza,
      cámara congelada (sin frames > X min).

### Fase 4 — Unificación del wizard
- [x] F4.1 Ruta canónica de setup: INTENT → PAIR(QR) → FRAME → PLACEMENT_CHECK →
      ZONES(sugeridas+edit) → RULES_CONFIRM → ACTIVE (fases existentes reordenadas).
- [x] F4.2 claim_token: verificado — lo produce api_eva.py (claim-cam/claim-qr existen);
      frontend correcto.
- [x] F4.3 Deprecar fases CAMERA_* legacy del flujoviejo (mantenidas por compatibilidad,
      sin borrar para no romper sesiones viejas).

## Reglas de colaboración
- Otro agente trabaja en admin: NO tocar admin2/, portal/, billing*, megapanel, ni
  health_monitor.py / service_bus.py (tiene cambios sin commit del otro agente).
- Commit por fase, solo con los archivos propios.
