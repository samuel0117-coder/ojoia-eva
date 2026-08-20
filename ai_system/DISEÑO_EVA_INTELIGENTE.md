# EVA INTELLIGENTE — Diseño Completo del Sistema

## Filosofía

Eva es un agente conversacional inteligente que:
1. **Conversa naturalmente** con el usuario (no es un formulario)
2. **Extrae datos** de la conversación usando function calling del LLM
3. **Guarda datos estructurados** en business.json
4. **Usa herramientas** para buscar frames, eventos, etc.

## Flujo Conversacional Diseñado

```
PANTALLA: Login (teléfono + PIN)
    ↓
PANTALLA: EVA CHAT
    ↓
┌─────────────────────────────────────────────────────────────────────────────┐
│ TURNO 1: EVA SE PRESENTA                                                    │
│                                                                             │
│ Eva: "¡Hola! 👋 Soy Eva, tu asistente de seguridad inteligente.            │
│       Voy a ayudarte a configurar el sistema de vigilancia para tu negocio. │
│       ¿Cómo te llamas y cómo se llama tu negocio?"                          │
│                                                                             │
│Usuario: "Hola Eva, me llamo Samuel y tengo Finca La Esperanza"             │
│                                                                             │
│ → EXTRACTOR: {owner_name: "Samuel", business_name: "Finca La Esperanza"}   │
│ → GUARDAR en business.json                                                  │
│                                                                             │
├─────────────────────────────────────────────────────────────────────────────┤
│ TURNO 2: EVA PREGUNTA TIPO Y ANIMALES                                       │
│                                                                             │
│ Eva: "¡Encantada, Samuel! 🌿 Ya veo que tienes una finca.                  │
│       ¿Qué tipo de finca es y qué animales tienes?"                         │
│                                                                             │
│Usuario: "Es de ganado, tengo vacas y pollos"                               │
│                                                                             │
│ → EXTRACTOR: {business_type: "finca", subtype: "ganado",                    │
│               animals: ["vacas", "pollos"]}                                  │
│ → GUARDAR en business.json                                                  │
│                                                                             │
├─────────────────────────────────────────────────────────────────────────────┤
│ TURNO 3: EVA PREGUNTA PREOCUPACIONES                                        │
│                                                                             │
│ Eva: "¡Genial! 🐄🐔 ¿Cuáles son tus principales preocupaciones             │
│       de seguridad? Por ejemplo: ¿robo de animales, intrusos,              │
│       vigilar empleados?"                                                   │
│                                                                             │
│Usuario: "Que no me roben las vacas y que vigile quién entra"               │
│                                                                             │
│ → EXTRACTOR: {concerns: ["robo de animales", "control de acceso"]}         │
│ → GUARDAR en business.json                                                  │
│                                                                             │
├─────────────────────────────────────────────────────────────────────────────┤
│ TURNO 4: EVA PREGUNTA HORARIO                                               │
│                                                                             │
│ Eva: "Entendido. ¿Cuál es el horario de la finca?"                         │
│                                                                             │
│Usuario: "De 6am a 10pm"                                                    │
│                                                                             │
│ → EXTRACTOR: {schedule: {open: "06:00", close: "22:00"}}                   │
│ → GUARDAR en business.json                                                  │
│                                                                             │
├─────────────────────────────────────────────────────────────────────────────┤
│ TURNO 5: EVA RESUME Y PREGUNTA CÁMARAS                                      │
│                                                                             │
│ Eva: "Perfecto, Samuel. Tengo todo claro:                                   │
│       🏢 Finca La Esperanza — Finca de ganado                               │
│       🐄 Vacas y pollos                                                     │
│       🔒 Preocupaciones: robo de animales, control de acceso                │
│       ⏰ Horario: 6am a 10pm                                                │
│       ¿Está bien así? ¿Tienes cámaras para configurar?"                     │
│                                                                             │
│Usuario: "Sí, está bien. Tengo 2 cámaras"                                   │
│                                                                             │
│ → EXTRACTOR: {camera_count: 2}                                              │
│                                                                             │
├─────────────────────────────────────────────────────────────────────────────┤
│ TURNO 6: EVA CONFIGURA CÁMARA 1                                             │
│                                                                             │
│ Eva: "¡Vamos con la primera! ¿Dónde la vas a poner y qué quieres           │
│       que vigile?"                                                           │
│                                                                             │
│Usuario: "En el corral de vacas, que cuente las vacas y detecte personas"   │
│                                                                             │
│ → EXTRACTOR: {camera_1_zone: "corral", camera_1_tasks:                     │
│               ["contar vacas", "detectar personas"]}                        │
│ → GUARDAR PROMPT DE VIGILANCIA                                              │
│                                                                             │
│ Eva: "¡Perfecto! ¿Tienes la cámara OjoIA conectada?"                       │
│                                                                             │
│Usuario: "Sí"                                                                │
│                                                                             │
│ → EVA GUÍA CONEXIÓN: "Sigue estos pasos: corriente → LED azul → WiFi..."  │
│                                                                             │
│Usuario: "Listo"                                                             │
│                                                                             │
│ → EVA MUESTRA FRAME + ANÁLISIS DE QWEN                                     │
│   "¡Excelente! 🎉 La cámara está funcionando.                              │
│    Veo un corral con varias vacas pastando. ¿Es correcto?"                  │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

## Schema JSON del Negocio (business.json)

```json
{
  "user_id": "809-XXX-XXXX",
  "pin_hash": "...",
  "owner": {
    "name": "Samuel",
    "apellido": "Rodriguez",
    "phone": "809-XXX-XXXX"
  },
  "business_name": "Finca La Esperanza",
  "business_type": "finca",
  "subtype": "ganado",
  "animals": ["vacas", "pollos"],
  "schedule": {"open": "06:00", "close": "22:00"},
  "main_concerns": ["robo de animales", "control de acceso"],
  "cameras": {
    "cam_001": {
      "name": "Corral de vacas",
      "zone": "corral",
      "active": true,
      "prompt_vigilancia": "...",
      "tareas": ["contar vacas", "detectar personas"],
      "last_frame_ts": 0,
      "today_summary": {
        "date": "2026-06-07",
        "total_analisis": 0,
        "conteo_vacas": {"manana": null, "tarde": null},
        "alertas": 0,
        "qwen_analisis": []
      }
    }
  },
  "people": {"known": [], "suspicious": []},
  "daily_summaries": {},
  "conversation_context": {
    "configuracion_completa": true,
    "camaras_configuradas": 1,
    "weaknesses": ["robo de animales"],
    "priorities": ["vigilar corral"]
  }
}
```

## Sistema de Extracción de Datos

Usa **function calling nativo del LLM** (no regex ni reglas):

```python
# Tools disponibles para Eva
TOOLS = [
    {
        "name": "save_business_data",
        "description": "Guarda datos del negocio extraídos de la conversación",
        "parameters": {
            "type": "object",
            "properties": {
                "field": {"type": "string", "description": "Campo a guardar: business_name, business_type, concerns, schedule_open, schedule_close, owner_name, camera_zone, etc."},
                "value": {"type": "string", "description": "Valor extraído del mensaje del usuario"}
            },
            "required": ["field", "value"]
        }
    },
    {
        "name": "search_events",
        "description": "Busca eventos por descripción, hora o cámara",
        "parameters": {
            "type": "object", "properties": {
                "query": {"type": "string"},
                "date": {"type": "string"},
                "camera_id": {"type": "string"}
            }
        }
    },
    {
        "name": "get_camera_frames",
        "description": "Obtiene frames recientes de una cámara",
        "parameters": {
            "type": "object",
            "properties": {
                "camera_id": {"type": "string"},
                "count": {"type": "integer"}
            }
        }
    }
]
```

## Prompt del Sistema de Eva

```
Eres Eva, asistente de seguridad inteligente de OjoIA. 
Tu trabajo es configurar el sistema de vigilancia del negocio del usuario.

REGLAS DE CONVERSACIÓN:
- Tono dominicano, cercano, natural. Como una persona real.
- NO hagas preguntas de formulario. Conversa naturalmente.
- Extrae datos de CUALQUIER mensaje del usuario, no solo de respuestas directas.
- Si el usuario da múltiples datos en un mensaje, extrae TODOS.
- Usa las herramientas (tools) para guardar datos extraídos y consultar información.

FLUJO DE CONFIGURACIÓN (3-5 turnos):
1. Saludo y presentación → Preguntar nombre y negocio
2. Extraer tipo de negocio y qué vigilar
3. Extraer horario
4. Resumir datos y preguntar por cámaras
5. Configurar cada cámara: zona + tareas + conectar

EJEMPLO DE CONVERSACIÓN:
Usuario: "Hola, me llamo Samuel y tengo una finca con vacas"
Eva: "¡Hola Samuel! 👋 Soy Eva. Una finca con vacas, ¡qué bien! 🐄 
       ¿Cuál es el nombre de tu finca y cuál es tu principal 
       preocupación de seguridad?"
→ save_business_data(field="owner_name", value="Samuel")
→ save_business_data(field="business_type", value="finca")
→ save_business_data(field="animals", value="vacas")

Usuario: "Se llama Finca La Esperanza y me preocupa que me roben"
Eva: "Entendido, Samuel. Anoto todo:
       🏢 Finca La Esperanza (finca de ganado)
       🔒 Preocupación: robo de animales
       ¿Cuál es el horario de la finca?"
→ save_business_data(field="business_name", value="Finca La Esperanza")
→ save_business_data(field="concerns", value="robo de animales")

NOTA: Usa save_business_data CADA VEZ que extraigas un dato del usuario.
No esperes a tener todo. Extrae y guarda dato por dato.
```

## Arquitectura del Sistema

```
┌─────────────────────────────────────────────────────────────────────┐
│                        EVA CHAT OS                                  │
│                                                                     │
│  ┌──────────┐       ┌──────────────┐       ┌──────────────┐        │
│  │ Frontend │◄─────►│  EVA CHAT    │◄─────►│   Qwen LLM   │        │
│  │ (Chat UI)│       │  ENGINE      │       │   (8004)     │        │
│  └──────────┘       └──────┬───────┘       └──────────────┘        │
│                             │                                       │
│                             ▼                                       │
│                    ┌────────────────┐                               │
│                    │ Function Calls │                               │
│                    │ - save_business│                               │
│                    │ - search_events│                               │
│                    │ - get_frames   │                               │
│                    └───────┬────────┘                               │
│                            │                                        │
│                    ┌───────▼────────┐                               │
│                    │  BUSINESS.JSON │                               │
│                    │  (Datos del    │                               │
│                    │   negocio)     │                               │
│                    └────────────────┘                               │
└─────────────────────────────────────────────────────────────────────┘
```

## Ventajas de Eva Inteligente vs Determinista

| Aspecto | Determinista (actual) | Inteligente (nuevo) |
|---|---|---|
| **Conversación** | Formulario frío | Natural, como persona |
| **Extracción** | Regex fijo | LLM entiende contexto |
| **Respuestas** | Siempre iguales | Adaptativas |
| **Errores** | Se rompe si no sigue flujo | Se adapta al usuario |
| **Herramientas** | No usa | Busca frames, eventos, etc. |
| **Prompt vigilancia** | Fijo | Dinámico por cámara |
| **Experiencia** | "Llenando formulario" | "Hablando con alguien inteligente" |
