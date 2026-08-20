# 🎯 Plan Principal — Mejorar Instalación de Cámara (WOW #1, #2, #3)

> **Fecha:** 2026-08-19
> **Objetivo:** Transformar el flujo de instalación de cámaras en una experiencia de usuario que genere "moments of wow" en cada paso.

---

## Visión Actual vs. Objetivo

### Flujo actual (débil):
```
GREET → ZONE → HARDWARE → WAIT_IMAGE → ANALYZE → CONTEXT → PROMPT_BUILD → CONFIRM → DONE
```

**Problema:** Cuando el usuario dice "listo", Eva simplemente confirma y pasa a CONTEXT. No muestra la imagen, no analiza la posición, no sugiere zonas. El usuario no VE su negocio estructurado.

### Flujo objetivo (WOW):
```
GREET → ZONE → HARDWARE → WAIT_IMAGE → ANALYZE (WOW #1) → ZONES (WOW #2) → CONTEXT → PROMPT_BUILD → CONFIRM → TEST_RULES (WOW #3) → DONE
```

---

## WOW #1 — Asistente de Colocación de Cámara (EXISTENTE, MEJORAR)

**Estado actual:** `eva_v2.py:1928-1958` `_handle_analyze` existe pero es débil.

**Problema:** El análisis de posición es básico. Qwen dice si la zona coincide pero no da recomendaciones prácticas.

**Mejoras propuestas:**

1. **Validación de calidad de imagen:**
   - Iluminación (buena/regular/mala)
   - Contraluz (sí/no)
   - Orientación (vertical/horizontal/torcida)
   - Visibilidad de lo que se quiere vigilar

2. **Message mejorado en `_handle_analyze`:**
   ```
   📷 Cámara conectada ✅
   Zona configurada: cocina
   Zona detectada: cocina principal
   
   Descripción: [Qwen describe lo que ve]
   
   ✅ La posición se ve bien para vigilar la cocina.
   ⚠️ La iluminación es regular — podrías mejorarla.
   💡 Veo un poco de contraluz, la cámara está apuntando hacia la luz.
   
   ¿La dejamos en el mismo lugar o querés ajustarla?
   ```

3. **Iteración hasta que Eva esté satisfecha:**
   - Si el usuario dice "la movemos" → Eva vuelve a analizar
   - Si dice "la dejamos" → pasa a ZONES
   - Si dice "muestra la imagen" → Eva muestra el frame con el análisis superpuesto

**Archivo a modificar:** `eva_v2.py` — `_handle_analyze` (líneas 1928-1958)

---

## WOW #2 — "Esto es exactamente tu mostrador" (NUEVO)

**Objetivo:** El usuario VE su propio negocio estructurado por primera vez, con zonas marcadas.

**Flujo:**
1. Después de confirmar la posición, Eva llama a Qwen para sugerir zonas de interés
2. Qwen analiza el frame y sugiere 3-5 zonas con coords 0-1
3. Eva muestra la imagen con las zonas superpuestas en el chat
4. El usuario confirma o ajusta en la pestaña "Ajustes de cámara" (drawer existente)
5. Cuando el usuario guarda las zonas, Eva vuelve al chat con: "✅ Tus zonas están listas. ¿Qué querés que vigile?"

**Endpoint nuevo (C1.3):** `POST /api/cameras/{id}/suggest-zones`
```python
# api_eva.py — nuevo endpoint
@app.post("/api/cameras/{camera_id}/suggest-zones")
async def suggest_zones(camera_id: str, user_id: str):
    # 1. Obtener último frame
    # 2. Llamar a Qwen con prompt de sugerencia de zonas
    # 3. Devolver [{id, name, type, coords: {x,y,w,h}, color, icon}]
```

**Fase ZONES en state machine (C3.1):**
```python
# eva_v2.py — añadir SetupPhase.ZONES
class SetupPhase(str, Enum):
    GREET = "greet"
    ZONE = "zone"
    HARDWARE = "hardware"
    WAIT_IMAGE = "wait_image"
    ANALYZE = "analyze"
    ZONES = "zones"          # ← NUEVO
    CONTEXT = "context"
    PROMPT_BUILD = "prompt_build"
    CONFIRM = "confirm"
    TEST_RULES = "test_rules"  # ← NUEVO
    DONE = "done"
```

**Handler `_handle_zones`:**
- Llama a suggest-zones endpoint
- Muestra imagen con zonas superpuestas
- Pregunta: "¿Estas son las zonas que querés vigilar?"
- Si el usuario dice "sí" → CONTEXT
- Si dice "ajustarlas" → redirige al drawer

**Archivo a modificar:** `eva_v2.py` — añadir `SetupPhase.ZONES` y `_handle_zones`

---

## WOW #3 — Notificación Real al Probar una Regla (NUEVO)

**Objetivo:** El usuario recibe una notificación push/WhatsApp REAL disparada por SU PROPIA acción. Esto demuestra que el sistema funciona y genera confianza.

**Flujo:**
1. Después de generar las 3 reglas (PROMPT_BUILD → CONFIRM)
2. Eva dice: "Ahora vamos a probar cada regla. Por favor, haz lo siguiente..."
3. Muestra counter: "0/3 reglas probadas"
4. Por cada regla:
   - Instrucción: "Abre el cajón como si estuvieras robando"
   - Espera confirmación del usuario
   - Llama al endpoint de prueba
   - Si se dispara: "✅ Regla 1 probada — te acabo de enviar una notificación"
   - Counter actualizado: "1/3 reglas probadas ✅"
5. Cuando todas estén probadas: "🎉 Todas las reglas están funcionando. Cámara lista."

**Endpoint nuevo:** `POST /api/cameras/{id}/test-rule`
```python
# api_eva.py — nuevo endpoint
@app.post("/api/cameras/{camera_id}/test-rule")
async def test_rule(camera_id: str, user_id: str, rule_index: int, test_action: str):
    # 1. Simular la acción de prueba
    # 2. Evaluar si la regla se dispara
    # 3. Si se dispara → enviar notificación FCM real
    # 4. Devolver: {triggered: bool, notification_sent: bool}
```

**Efecto Zeigarnik:** El counter visible "2/3 reglas probadas" crea una tarea visiblemente incompleta que el cerebro quiere cerrar. Esto aumenta la tasa de completación.

**Archivo a modificar:** `eva_v2.py` — añadir `SetupPhase.TEST_RULES` y `_handle_test_rules`

---

## 📋 Resumen de Cambios

| Archivo | Cambio | Líneas aprox |
|---------|--------|-------------|
| `eva_v2.py` | Mejorar `_handle_analyze` (WOW #1) | +30 |
| `eva_v2.py` | Añadir `SetupPhase.ZONES` + `_handle_zones` (WOW #2) | +60 |
| `eva_v2.py` | Añadir `SetupPhase.TEST_RULES` + `_handle_test_rules` (WOW #3) | +80 |
| `api_eva.py` | Añadir `POST /api/cameras/{id}/suggest-zones` | +40 |
| `api_eva.py` | Añadir `POST /api/cameras/{id}/test-rule` | +50 |
| `app-v12.js` | (Opcional) Botón "Sugerir con IA" en drawer | +15 |

**Total: ~275 líneas de código nuevo**

---

## 🚀 Orden de Ejecución

```
1. Fase 1 (WOW #1)     → 30 min  Mejorar ANALYZE
2. C1.3 endpoint       → 30 min  Sugerir zonas con Qwen
3. C3.1 ZONES phase    → 40 min  Integrar en state machine
4. Fase 3 (WOW #3)     → 60 min  Sistema de prueba de reglas
```

**Total estimado: ~3 horas**

---

## Notas de Diseño

- **No duplicar drawer:** El drawer de zonas en `app-v12.js:3151-3534` ya existe y está completo. Se reutiliza.
- **No botón "Sugerir con IA":** El proceso es automático — Eva llama al endpoint cuando el usuario confirma la posición.
- **Qwen reutilizado:** Se usan `_describe_frame` y `_analyze_frame_for_prompt` existentes, no se duplica.
- **Notificaciones FCM:** Se reutiliza `send_fcm_notification()` de `orchestrator.py:344-483`.
- **Counter de progreso:** El mismo patrón que C2.5 (counter de zonas) se aplica a las reglas.