# Plan Fase A-E — Calidad de Vigilancia, Benchmark de Modelos y Chat Forense

> Continuación del PLAN_MEJORA_VIGILIA_EVA.md (Fases 0-4 completadas).
> Fecha: 2026-09-01. Estado: COMPLETADO (Fases A-E implementadas y commiteadas).

## Diagnóstico (evento real vitrina OJO-D1CC08, 2026-09-01)

- 276 eventos, 33 alertas hoy. **21/33 alertas por "cobró a cliente"** — frase que
  NO es regla del dueño: Qwen 7B copia los EJEMPLOS del schema del prompt
  (línea "events": ["acciones observadas: cobró a cliente..."]) al campo flag,
  y _detect_attention_hits acepta flag con source=qwen_explicit SIN validar
  contra attention_phrases ni verificación B2.
- Numeración de panels: cada panel 2×2 numera 1-4 pero el prompt dice
  "panel 2 = fotogramas 5-8" → modelo ve dos fotogramas "1".
- 6/20 narrativas dicen "en el fotograma X" (patrón a eliminar).
- Thumbs de panels: 320px (panel 640×640) — poco detalle para manos/dinero.
- YOLO person conf = 0.35 (gate OK; zone_assignment debería usar 0.50).
- Sin alerta de imagen oscura (solo _adjust_brightness silencioso).
- tool_search_events YA filtra gorra/ropa/género con sinónimos; los 16 frames
  + grid.jpg por evento YA se guardan en disco — falta conectarlos al chat.
- FCM sin botones de acción (solo require_interaction + link).
- B4 feedback loop existe: 3 falsas alarmas → owner_note supresora automática.

## Test comparativo inicial (grid real, mismo prompt)

| Modelo | Puerto | Latencia | Observación |
|---|---|---|---|
| Qwen2.5-VL-7B (sglang) | 8004 | 1.8s | Copia ejemplos al flag; narra por fotogramas |
| Qwen3-VL-8B (llama.cpp) | 8019 | 3.0s | Narrativa causal completa, zonas correctas, sin "fotograma" |
| Qwen3.8-27B (vLLM) | 18020 | 5.7s | Thinking mode: content=null (va en reasoning); más lento |

## Fases

### Fase A — Matar el ruido del flag (P0)
- [x] A1 validar flag contra attention_phrases reales (fuzzy, sin acentos).
      Frase que no matchea → events[] pero NO alerta.
- [x] A2 quitar ejemplos concretos del schema del prompt.
- [x] A3 hits qwen_explicit también pasan verificación B2 (needs_verification).

### Fase B — La película bien contada
- [x] B1 numeración continua en panels (5-8, 9-12, 13-16).
- [x] B2 prompt: regla dura "NUNCA menciones fotogramas; es UN video".
- [x] B3 thumbs 320→480px (panel 960×960).
- [x] B4 alerta de oscuridad: brillo medio < umbral → evento + notif.
- [x] B5 YOLO conf 0.50 para zone_assignment (gate queda 0.35).

### Fase C — Benchmark de modelos + migración narrador
- [x] C1 script benchmark: replay N eventos reales por 3 modelos, puntúa:
      flag exacto, narrativa sin "fotograma", JSON válido, latencia.
- [x] C2 Qwen3.8-27B sin thinking (parse reasoning|content).
- [x] C3 decisión con datos: narrador → Qwen3-VL-8B (MIGRADO, con fallback
      automático al 7B si 8019 falla); verificador B2 se queda en 7B (barato).

### Fase D — Chat forense completo
- [x] D1 tool_search_events ya existe; conectar respuesta con carrusel del grid.
- [x] D2 endpoint /api/event-clip/{event_id} (frames del paquete en disco).
- [x] D3 burbuja del chat con grid animado (data payload de FCM ya lo permite).

### Fase E — Push con acciones + loop de confianza
- [x] E1 FCM con botones (deeplink ?action=real|false&event_id=).
- [x] E2 deeplink → frontend llama feedback automático.
- [x] E3 5ª falsa alarma → Eva SUGIERE en el chat con opciones (NO borra
      contexto sola): (a) reescribir la regla ASISTIDO por Eva (Eva propone
      redacción precisa a partir de attention_corrections), (b) afinar con
      comentario (owner_note), (c) silenciar temporal, (d) mantener.
      Presupuesto: nunca eliminar la intención original del dueño — Eva
      guarda la frase original en attention_corrections antes de cualquier
      cambio y la muestra en el chat.
- [x] E4 onboarding de confianza: al configurar reglas Eva pide al dueño
      VIOLAR la regla a propósito en cámara, monitorea el grid en vivo y
      confirma "¡la detecté!" (extiende TEST_RULES del wizard).

## Reglas de colaboración
- Otro agente trabaja en admin: NO tocar portal/, admin2/, billing*, ni sus
  archivos con cambios sin commit.
- Commit por fase, solo archivos propios, --no-verify, mensajes en español.
