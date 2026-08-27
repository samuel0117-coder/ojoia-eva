# 🎯 Plan Principal — Mejorar Instalación de Cámara (WOW #1, #2, #3)

> **Fecha:** 2026-08-19
> **Estado:** ✅ EJECUTADO (3 de 3 fases)
> **Commits:** `c90d7e6` → `9135355` → `741993b` → `3e88d5a`

---

## Flujo Antes vs. Ahora

### Antes (débil):
```
GREET → ZONE → HARDWARE → WAIT_IMAGE → ANALYZE → CONTEXT → PROMPT_BUILD → CONFIRM → DONE
```

**Problema:** Cuando el usuario dice "listo", Eva simplemente confirma y pasa a CONTEXT. No muestra la imagen, no analiza la posición, no sugiere zonas. El usuario no VE su negocio estructurado.

### Ahora (WOW):
```
GREET → ZONE → HARDWARE → WAIT_IMAGE → ANALYZE (WOW #1) → ZONES (WOW #2) → CONTEXT → PROMPT_BUILD → CONFIRM → TEST_RULES (WOW #3) → DONE
```

---

## ✅ Fase 1 — WOW #1: Asistente de Colocación de Cámara

**Commit:** `c90d7e6`

**Cambios en `eva_v2.py`:**
- `_analyze_frame_for_prompt`: añadidos campos `contraluz`, `orientacion`, `visibilidad_objetivo` al JSON de Qwen
- `_describe_frame`: prompt mejorado con evaluación de calidad de imagen (iluminación, contraluz, orientación, visibilidad del objetivo)
- `_handle_analyze`: feedback detallado de calidad. Eva actúa como asistente de colocación:
  - "💡 Iluminación: regular"
  - "⚠️ Veo contraluz — la cámara está apuntando hacia la luz"
  - "🔄 Orientación: torcida"
  - "👁️ Visibilidad del área: mala"
  - "🔍 Sugerencia: [Qwen sugiere ajuste]"

---

## ✅ Fase 2 — C1.3 + C3.1: WOW #2 "Esto es exactamente tu mostrador"

**Commits:** `9135355` (C1.3), `741993b` (C3.1)

**C1.3 — Endpoint `/api/cameras/{id}/suggest-zones` (`api_eva.py`):**
- Toma el último frame de la cámara (grid + latest_vigilance.jpg + latest_raw.jpg)
- Pide a Qwen que sugiera 3-6 zonas de interés con coords relativas 0-1
- Devuelve: `[{id, name, type, coords: {x,y,w,h}, color, icon, suggested_by: "qwen"}]`
- Normalización automática de coords (clamp 0-1)
- `_zone_color_for_type` / `_zone_icon_for_type`: matching con drawer existente

**C3.1 — Fase ZONES en state machine (`eva_v2.py`):**
- `SetupPhase.ZONES` añadido entre ANALYZE y CONTEXT
- `_handle_zones`: Eva llama a suggest-zones, muestra imagen con zonas superpuestas en el chat
- `_suggest_zones_via_api`: cliente HTTP al endpoint C1.3
- `_format_zones_summary`: formatea zonas como texto legible
- El usuario puede confirmar o ajustar en la pestaña "Ajustes de cámara" (drawer existente)

---

## ✅ Fase 3 — WOW #3: Notificación Real al Probar una Regla

**Commit:** `3e88d5a`

**Endpoint `/api/cameras/{id}/test-rule` (`api_eva.py`):**
- Evalúa si una acción de prueba dispara una regla (usa Qwen)
- Si se dispara → envía notificación FCM real al usuario
- Devuelve: `{triggered, notification_sent}`

**`_handle_test_rules` (`eva_v2.py`):**
- Flujo de prueba de reglas con counter visible "X/3 reglas probadas"
- Efecto Zeigarnik: tarea visiblemente incompleta que el cerebro quiere cerrar
- Por cada regla:
  - Eva dice: "Abre el cajón como si estuvieres robando"
  - Espera confirmación del usuario
  - Llama al endpoint de prueba
  - Si se dispara: "✅ Regla 1 probada — te acabo de enviar una notificación"
  - Counter actualizado: "1/3 reglas probadas ✅"
- Cuando todas estén probadas: "🎉 Todas las reglas están funcionando. Cámara lista."

**`_handle_confirm` modificado:** Ahora va a TEST_RULES en vez de DONE directamente.

---

## 📋 Resumen de Cambios

| Archivo | Cambio | Líneas |
|---------|--------|--------|
| `eva_v2.py` | Fase 1 (WOW #1) + Fase 2 (C3.1) + Fase 3 (WOW #3) | +250 |
| `api_eva.py` | C1.3 (suggest-zones) + WOW #3 (test-rule) | +230 |

---

## 🚀 Flujo de Usuario Final

```
1. fronts: "Quiero instalar una cámara nueva"
2. Eva: "¿Dónde la vas a poner?" → "cocina"
3. Eva: Instrucciones de conexión (LED, WiFi, etc.)
4. fronts: "listo"
5. Eva: 📷 Muestra la imagen + análisis (WOW #1)
   → "Tu cámara está en cocina. La iluminación es regular..."
   → "¿La dejamos aquí o la movemos?"
6. fronts: "la dejamos"
7. Eva: 🔍 Sugiere zonas con IA (WOW #2)
   → "Esto es exactamente tu mostrador, con cada área marcada"
   → Muestra imagen con zonas superpuestas
   → "¿Qué querés que vigile?"
8. fronts: "el mostrador y la caja"
9. Eva: Crea las reglas de atención
10. fronts: "sí, apruebo"
11. Eva: 🔍 Sistema de prueba (WOW #3)
    → "Vamos a probar cada regla"
    → Counter: "0/3 reglas probadas"
    → "Abre el cajón como si estuvieres robando"
12. fronts: "lo hice"
13. Eva: "✅ Regla 1 probada — te acabo de enviar una notificación"
    → Counter: "1/3 reglas probadas ✅"
14. ... (repite para las 3 reglas)
15. Eva: "🎉 Todas las reglas están funcionando. Cámara lista."
```

---

## 🔧 Próximos pasos (fuera del plan actual)

- [ ] B5 — Activar `cleanup_frames.py` con la configuración de retención
- [ ] D2 — Cola por `(user_id, camera_id)` en vez de `FRAME_QUEUE` global
- [ ] T1 — `--edge-ip-version 4` en `/etc/cloudflared/config.yml`
- [ ] C3.2 — CONTEXT zone-aware (pregunta por zona, no global)
- [ ] C3.4 — Feedback de reglas (consolidación cada 3-4 correcciones)
- [ ] C4.1 — Nivel 3 de zonas
- [ ] C4.2 — Correlación narrativa entre cámaras