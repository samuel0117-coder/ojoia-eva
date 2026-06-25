"""
FLUJO DE CONFIGURACIÓN EVA — Diseño Final

Este documento describe el flujo paso a paso de la configuración inicial
con Eva, desde el registro hasta el chat OS activo.
"""

# ═══════════════════════════════════════════════════════════════
# PANTALLA 1: LOGIN / REGISTRO
# ═══════════════════════════════════════════════════════════════

LOGIN = """
┌─────────────────────────────────────────────────────────────────────────────┐
│                              O J O I A                                     │
│                                                                             │
│  📱 Teléfono: [809-XXX-XXXX]                                               │
│  🔑 PIN: [____]                                                             │
│                                                                             │
│  [ Entrar ]                                                                 │
│                                                                             │
│  ¿No tienes cuenta? [ Registrarme ]                                         │
│  ¿Olvidaste tu PIN? [ Reenviar PIN ]                                        │
└─────────────────────────────────────────────────────────────────────────────┘
"""

REGISTRO = """
┌─────────────────────────────────────────────────────────────────────────────┐
│                              O J O I A                                     │
│                        Crear nueva cuenta                                   │
│                                                                             │
│  👤 Nombre: [_______________]                                               │
│  👤 Apellido: [_______________]                                             │
│  📱 Teléfono: [809-XXX-XXXX]                                                │
│  🔑 PIN (4 dígitos): [____]                                                 │
│  🔑 Confirmar PIN: [____]                                                   │
│                                                                             │
│  [ Crear cuenta ]                                                           │
│                                                                             │
│  Te enviaremos un código de confirmación por WhatsApp.                       │
└─────────────────────────────────────────────────────────────────────────────┘
"""


# ═══════════════════════════════════════════════════════════════
# PANTALLA 2: EVA CONFIGURACIÓN — PASO 1: SALUDO Y CONTEXTO
# ═══════════════════════════════════════════════════════════════

PASO1_SALUDO = """
┌─────────────────────────────────────────────────────────────────────────────┐
│ 🤖 Eva                                                                      │
│                                                                             │
│ Eva: "¡Hola Samuel! 👋 Soy Eva, tu asistente de seguridad inteligente.      │
│       Vamos a configurar tu sistema de vigilancia.                           │
│       ¿Cómo se llama tu negocio?"                                           │
│                                                                             │
│ [Escribe un mensaje...]                                                     │
└─────────────────────────────────────────────────────────────────────────────┘

--- Usuario responde: "Finca La Esperanza" ---

EVA_EXTRAE_TIPO_NEGOCIO = """
Eva analiza el nombre del negocio y extrae/infiere el tipo:

- "Finca La Esperanza" → tipo: "finca" (por la palabra "Finca")
- "Colmado La Esquina" → tipo: "colmado"
- "Restaurante El Sabor" → tipo: "restaurante"
- "Tienda Don Pepe" → tipo: "tienda"
- "Bodega San Juan" → tipo: "bodega"
- "Hospital Central" → tipo: "hospital"
- Si no puede inferir → pregunta directamente
"""

PASO1_CONFIRMACION_TIPO = """
┌─────────────────────────────────────────────────────────────────────────────┐
│ Eva: "¡Finca La Esperanza! 🌿 ¿Es una finca de ganado, agrícola,           │
│       o de qué tipo?"                                                       │
│                                                                             │
│ [Escribe un mensaje...]                                                     │
└─────────────────────────────────────────────────────────────────────────────┘

--- Usuario responde: "De ganado, tenemos vacas y pollos" ---

PASO1_PREOCUPACIONES = """
┌─────────────────────────────────────────────────────────────────────────────┐
│ Eva: "¡Me encanta! 🐄🐔 ¿Cuáles son tus principales preocupaciones          │
│       de seguridad? Por ejemplo:                                            │
│       • ¿Que no te roben los animales?                                      │
│       • ¿Vigilar que todo esté en orden?                                    │
│       • ¿Controlar quién entra a la finca?                                  │
│       • ¿Otra cosa?"                                                        │
│                                                                             │
│ [Escribe un mensaje...]                                                     │
└─────────────────────────────────────────────────────────────────────────────┘

--- Usuario responde: "Que no me roben los animales y vigilar que todo esté bien" ---

PASO1_HORARIO = """
┌─────────────────────────────────────────────────────────────────────────────┐
│ Eva: "Entendido. ¿Cuál es el horario de tu finca?                           │
│       ¿A qué hora abren y cierran?"                                         │
│                                                                             │
│ [Escribe un mensaje...]                                                     │
└─────────────────────────────────────────────────────────────────────────────┘

--- Usuario responde: "De 6am a 6pm" ---

PASO1_RESUMEN = """
┌─────────────────────────────────────────────────────────────────────────────┐
│ Eva: "Perfecto, Samuel. Tengo todo claro:                                    │
│                                                                             │
│       🏢 Finca La Esperanza — Finca de ganado                               │
│       🐄 Vacas y pollos                                                     │
│       🔒 Preocupaciones: robo de animales, vigilancia general               │
│       ⏰ Horario: 6am a 6pm                                                 │
│                                                                             │
│       ¿Está bien así?"                                                      │
│                                                                             │
│ [Escribe un mensaje...]                                                     │
└─────────────────────────────────────────────────────────────────────────────┘
"""


# ═══════════════════════════════════════════════════════════════
# PANTALLA 2: EVA CONFIGURACIÓN — PASO 2: CONECTAR PRIMERA CÁMARA
# ═══════════════════════════════════════════════════════════════

PASO2_CONECTAR_CAMARA = """
┌─────────────────────────────────────────────────────────────────────────────┐
│ Eva: "¡Genial! Ahora vamos a conectar tu primera cámara.                    │
│       ¿Tienes la cámara OjoIA contigo?"                                     │
│                                                                             │
│ [Escribe un mensaje...]                                                     │
└─────────────────────────────────────────────────────────────────────────────┘

--- Usuario responde: "Sí" ---

PASO2_INSTRUCCIONES = """
┌─────────────────────────────────────────────────────────────────────────────┐
│ Eva: "¡Vamos! Sigue estos pasos:                                            │
│                                                                             │
│       1. 🔌 Conecta la cámara a la corriente eléctrica                      │
│       2. ⏳ Espera a que el LED parpadee en azul (unos 30 segundos)         │
│       3. 📱 En tu teléfono, ve a Configuración → WiFi                       │
│       4. 📡 Verás una red llamada 'OJO-XXXX' — conéctate a ella             │
│       5. 🌐 Se abrirá una página para conectar la cámara a tu WiFi          │
│          (ponle el nombre y contraseña de tu WiFi de la finca)              │
│       6. ✅ Cuando la cámara se conecte, el LED dejará de parpadear         │
│       7. 💬 Escríbeme 'listo' cuando veas el LED fijo                       │
│                                                                             │
│       Tómate tu tiempo, estoy aquí si tienes dudas. 👍"                     │
│                                                                             │
│ [Escribe un mensaje...]                                                     │
└─────────────────────────────────────────────────────────────────────────────┘

--- Usuario tarda 2-5 minutos conectando la cámara ---

--- Usuario responde: "listo" ---

PASO2_MOSTRAR_FRAME = """
┌─────────────────────────────────────────────────────────────────────────────┐
│ 📷 [IMAGEN DEL PRIMER FRAME DE LA CÁMARA]                                   │
│                                                                             │
│ Eva: "¡Excelente! 🎉 La cámara está funcionando.                            │
│       Veo un área como un corral de vacas.                                  │
│       ¿Vas a poner esta cámara en el corral o quieres que vigile             │
│       el corral desde donde está?"                                          │
│                                                                             │
│ [Escribe un mensaje...]                                                     │
└─────────────────────────────────────────────────────────────────────────────┘

--- Usuario responde: "Sí, es el corral" ---

PASO2_CONFIGURAR_ZONA = """
┌─────────────────────────────────────────────────────────────────────────────┐
│ Eva: "Perfecto. ¿Qué quieres que vigile esta cámara en el corral?           │
│       Por ejemplo:                                                          │
│       • Contar las vacas y avisar si falta alguna                           │
│       • Ver si las vacas están bien (pastando, enfermas, etc.)              │
│       • Detectar personas que entren al corral                             │
│       • Todo lo anterior                                                    │
│       • Otra cosa                                                           │
│                                                                             │
│ [Escribe un mensaje...]                                                     │
└─────────────────────────────────────────────────────────────────────────────┘

--- Usuario responde: "Que cuente las vacas y me avise si algo está mal" ---

PASO2_MAS_PRIORIDADES = """
┌─────────────────────────────────────────────────────────────────────────────┐
│ Eva: "¿Algo más importante para esta cámara?"                               │
│                                                                             │
│ [Escribe un mensaje...]                                                     │
└─────────────────────────────────────────────────────────────────────────────┘

--- Usuario responde: "Que me avise si ve algo sospechoso" ---

PASO2_PROMPT_CREADO = """
┌─────────────────────────────────────────────────────────────────────────────┐
│ Eva: "¡Listo! ✅ Voy a crear el sistema de vigilancia para el corral.       │
│                                                                             │
│       [Generando sistema de vigilancia...]                                  │
│        ✓ Prompt de cámara creado                                            │
│        ✓ Vigilancia activa                                                  │
│        ✓ Alertas configuradas                                               │
│                                                                             │
│       Cada vez que detecte algo importante, te avisaré aquí.                │
│       También puedes preguntarme '¿Cuántas vacas hay?' cuando quieras.      │
│                                                                             │
│       ¿Tienes otra cámara para configurar?"                                 │
│                                                                             │
│ [Escribe un mensaje...]                                                     │
└─────────────────────────────────────────────────────────────────────────────┘
"""


# ═══════════════════════════════════════════════════════════════
# PANTALLA 2: EVA CONFIGURACIÓN — PASO 3: CÁMARAS ADICIONALES
# ═══════════════════════════════════════════════════════════════

PASO3_CAMARA_ADICIONAL = """
┌─────────────────────────────────────────────────────────────────────────────┐
│ --- Usuario responde: "Sí, tengo otra" ---                                  │
│                                                                             │
│ Eva: "¡Vamos con la segunda! ¿Dónde la vas a poner?"                        │
│                                                                             │
│ [Escribe un mensaje...]                                                     │
└─────────────────────────────────────────────────────────────────────────────┘

--- Usuario responde: "En la granja de pollos" ---

PASO3_QUE_VIGILAR = """
┌─────────────────────────────────────────────────────────────────────────────┐
│ Eva: "¿Qué es lo más importante vigilar en la granja de pollos?"            │
│                                                                             │
│ [Escribe un mensaje...]                                                     │
└─────────────────────────────────────────────────────────────────────────────┘

--- Usuario responde: "Que vea si los pollos están bien y cuente los sacos de alimento" ---

PASO3_CONECTAR_RAPIDO = """
┌─────────────────────────────────────────────────────────────────────────────┐
│ Eva: "Entendido. Conecta la cámara igual que la primera:                    │
│       corriente → LED azul → WiFi 'OJO-XXXX' → conectar a tu WiFi          │
│       → escríbeme 'listo' cuando el LED esté fijo."                         │
│                                                                             │
│ [Escribe un mensaje...]                                                     │
└─────────────────────────────────────────────────────────────────────────────┘

--- Usuario responde: "listo" ---

PASO3_MOSTRAR_FRAME_2 = """
┌─────────────────────────────────────────────────────────────────────────────┐
│ 📷 [IMAGEN DEL FRAME DE LA SEGUNDA CÁMARA]                                  │
│                                                                             │
│ Eva: "¡Perfecto! 🐔 Veo los pollos y unos sacos de alimento.                │
│       ¿Es la granja de pollos o quieres que vigile otra zona?"              │
│                                                                             │
│ [Escribe un mensaje...]                                                     │
└─────────────────────────────────────────────────────────────────────────────┘

--- Usuario responde: "Sí, es la granja" ---

PASO3_CONFIGURAR_RAPIDO = """
┌─────────────────────────────────────────────────────────────────────────────┐
│ Eva: "¿Algo más importante para esta cámara?"                               │
│                                                                             │
│ [Escribe un mensaje...]                                                     │
└─────────────────────────────────────────────────────────────────────────────┘

--- Usuario responde: "Que me avise si los pollos se ven enfermos" ---

PASO3_PROMPT_CREADO_2 = """
┌─────────────────────────────────────────────────────────────────────────────┐
│ Eva: "¡Listo! ✅ Cámara 2 configurada.                                      │
│       • Vigilará el estado de los pollos                                    │
│       • Contará los sacos de alimento                                       │
│       • Te avisará si ve algo sospechoso o enfermos                         │
│                                                                             │
│       ¿Tienes otra cámara?"                                                 │
│                                                                             │
│ [Escribe un mensaje...]                                                     │
└─────────────────────────────────────────────────────────────────────────────┘
"""


# ═══════════════════════════════════════════════════════════════
# PANTALLA 2: EVA CONFIGURACIÓN — PASO 4: FINALIZACIÓN
# ═══════════════════════════════════════════════════════════════

PASO4_FINALIZACION = """
┌─────────────────────────────────────────────────────────────────────────────┐
│ --- Usuario responde: "No, eso es todo" ---                                 │
│                                                                             │
│ Eva: "¡Tu sistema está listo! 🎉                                            │
│                                                                             │
│       RESUMEN DE TU SISTEMA:                                                │
│       📷 Cámara 1 — Corral: cuenta vacas, estado, alertas                  │
│       📷 Cámara 2 — Granja: estado pollos, sacos, alertas                  │
│                                                                             │
│       ¿QUÉ PUEDES HACER AHORA?                                              │
│       • Pregúntame: '¿Cuántas vacas hay ahora?'                             │
│       • Pregúntame: '¿Los pollos están bien?'                               │
│       • Pregúntame: '¿Viste algo raro hoy?'                                 │
│       • Te avisaré automáticamente si detecto algo importante               │
│                                                                             │
│       ¿Tienes alguna pregunta o quieres ajustar algo?"                      │
│                                                                             │
│ [Escribe un mensaje...]                                                     │
└─────────────────────────────────────────────────────────────────────────────┘
"""


# ═══════════════════════════════════════════════════════════════
# PANTALLA 2: EVA CHAT OS (Siempre activo después de configuración)
# ═══════════════════════════════════════════════════════════════

CHAT_OS = """
┌─────────────────────────────────────────────────────────────────────────────┐
│ 🤖 Eva | Finca La Esperanza | 2 cámaras activas                            │
│                                                                             │
│ ── ALERTAS EN TIEMPO REAL ──────────────────────────────────────────────── │
│                                                                             │
│ 🚨 14:32 — Cámara Corral: Vaca cerca de la cerca (posible escape)          │
│ ✅ 12:00 — Cámara Corral: Conteo — 15 vacas, todas pastando                │
│ ⚠️ 08:15 — Cámara Granja: Saco de alimento bajo (quedan 3)                 │
│                                                                             │
│ ─────────────────────────────────────────────────────────────────────────── │
│                                                                             │
│ Usuario: "¿Cuántas vacas hay ahora?"                                        │
│                                                                             │
│ Eva: "Ahora mismo veo 15 vacas en el corral, todas pastando.                │
│        La última actualización fue hace 2 horas.                            │
│        ¿Quieres que te muestre la imagen más reciente?"                     │
│                                                                             │
│ Usuario: "Sí"                                                               │
│                                                                             │
│ 📷 [IMAGEN MÁS RECIENTE DEL CORRAL]                                         │
│                                                                             │
│ Eva: "Aquí tienes. Las 15 vacas están pastando cerca del bebedero.          │
│        Todo se ve normal. ✅"                                               │
│                                                                             │
│ ─────────────────────────────────────────────────────────────────────────── │
│                                                                             │
│ [Escribe un mensaje...]                                                     │
└─────────────────────────────────────────────────────────────────────────────┘
"""


# ═══════════════════════════════════════════════════════════════
# BUSINESS.JSON — Esquema después de configuración
# ═══════════════════════════════════════════════════════════════

BUSINESS_JSON = {
    "user_id": "809-XXX-XXXX",
    "pin": "1234",
    "owner": {
        "name": "Samuel",
        "apellido": "Rodriguez",
        "phone": "809-XXX-XXXX"
    },
    "business_name": "Finca La Esperanza",
    "business_type": "finca",
    "schedule": {"open": "06:00", "close": "18:00"},
    "main_concerns": ["robo de animales", "vigilancia general"],
    "cameras": {
        "cam_001": {
            "name": "Corral de vacas",
            "zone": "corral",
            "active": True,
            "prompt_vigilancia": "...",
            "tareas": ["contar vacas", "estado vacas", "detectar personas", "alertas"],
            "last_frame_ts": 0,
            "today_summary": {
                "date": "2026-06-07",
                "total_analisis": 0,
                "conteo_vacas": {"manana": None, "tarde": None},
                "estado_vacas": "normal",
                "alertas": 0,
                "qwen_analisis": []
            }
        },
        "cam_002": {
            "name": "Granja de pollos",
            "zone": "granja_pollos",
            "active": True,
            "prompt_vigilancia": "...",
            "tareas": ["estado pollos", "contar sacos", "alertas"],
            "last_frame_ts": 0,
            "today_summary": {
                "date": "2026-06-07",
                "total_analisis": 0,
                "estado_pollos": "normal",
                "sacos_alimento": 0,
                "alertas": 0,
                "qwen_analisis": []
            }
        }
    },
    "people": {"known": [], "suspicious": []},
    "daily_summaries": {},
    "conversation_context": {
        "configuracion_completa": True,
        "camaras_configuradas": 2,
        "weaknesses": ["robo de animales"],
        "priorities": ["vigilar corral", "estado pollos"]
    }
}
