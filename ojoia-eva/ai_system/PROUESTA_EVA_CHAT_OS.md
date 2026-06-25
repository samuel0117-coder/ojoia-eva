# 🧠 EVA CHAT OS — Propuesta Técnica Completa

## Visión

El chat con Eva se convierte en la **interfaz principal del sistema**. Todo lo que el usuario necesita —ver eventos, buscar personas, obtener resúmenes, configurar cámaras— se hace conversando con Eva. Eva usa un JSON RAG del negocio como memoria, busca en eventos guardados, y presenta resultados directamente en el chat (carrusel de frames, clips, estadísticas).

---

## 1. ARQUITECTURA DEL SISTEMA

```
┌─────────────────────────────────────────────────────────────────────┐
│                        EVA CHAT OS                                   │
│                                                                      │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐                          │
│  │ Web Chat │  │ WhatsApp │  │  Voz     │  ← Interfaces            │
│  │ (PWA)    │  │ (Baileys)│  │ (VAD+TTS)│                          │
│  └────┬─────┘  └────┬─────┘  └────┬─────┘                          │
│       └──────────────┴──────────────┘                                │
│                      │                                               │
│            ┌─────────┴─────────┐                                     │
│            │   EVA CHAT ENGINE │  ← Nuevo: eva_chat_os.py           │
│            │   (Orquestador)   │                                     │
│            └─────────┬─────────┘                                     │
│                      │                                               │
│    ┌─────────────────┼─────────────────┐                            │
│    │                 │                 │                            │
│    ▼                 ▼                 ▼                            │
│ ┌──────────┐  ┌──────────┐  ┌──────────┐                          │
│ │ RAG      │  │ TOOLS    │  │ CONTEXT  │                          │
│ │ ENGINE   │  │ ENGINE   │  │ BUILDER  │                          │
│ └────┬─────┘  └────┬─────┘  └────┬─────┘                          │
│      │              │              │                                │
│      ▼              ▼              ▼                                │
│ ┌──────────────────────────────────────────┐                       │
│ │           BUSINESS JSON (RAG)             │                       │
│ │  /storage/users/{uid}/business.json       │                       │
│ └──────────────────────────────────────────┘                       │
│                                                                      │
│ ┌──────────────────────────────────────────┐                       │
│ │           EVENT STORAGE                   │                       │
│ │  /storage/users/{uid}/cameras/{cid}/      │                       │
│ │    events/                                │                       │
│ │      {event_id}.json  ← metadatos         │                       │
│ │      {event_id}.jpg   ← frame             │                       │
│ │  /storage/users/{uid}/daily_reports/      │                       │
│ │    {YYYY-MM-DD}.json  ← resumen diario    │                       │
│ └──────────────────────────────────────────┘                       │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 2. EL BUSINESS JSON (RAG de Eva)

Este es el archivo que Eva lee y actualiza constantemente. Es su "memoria de trabajo".

### Estructura: `/storage/users/{uid}/business.json`

```json
{
  "user_id": "moXcjYsfYogCFfvHq0TmadF8ytt2",
  "business_name": "Super colmado La Esquina",
  "business_type": "retail",
  "owner": {
    "name": "Samuel Rodriguez",
    "phone": "809-XXX-XXXX",
    "whatsapp": "809-XXX-XXXX"
  },
  "schedule": { "open": "07:00", "close": "19:00" },
  "main_concerns": ["robo interno", "despacho sin factura"],
  
  "cameras": {
    "OJO-E17604": {
      "name": "Cámara caja",
      "zone": "caja",
      "active": true,
      "last_frame_ts": 1780722349,
      "rules": ["empleado debe usar camisa", "producto en funda"],
      "system_prompt": "La cámara captura el mostrador del cajero...",
      "today_summary": {
        "total_events": 45,
        "total_persons": 127,
        "alerts": 2,
        "false_alarms": 0,
        "peak_hour": "12:30",
        "peak_persons": 8,
        "qwen_descriptions": [
          {
            "time": "08:15",
            "persons": 2,
            "description": "Empleado organizando productos en el mostrador. Un cliente esperando."
          },
          {
            "time": "12:30",
            "persons": 8,
            "description": "Pico de clientes. Empleado cobrando, 3 personas en fila."
          }
        ]
      }
    }
  },

  "people": {
    "known": [
      {
        "id": "emp_001",
        "name": "Pedro",
        "role": "cajero",
        "visual_tags": ["delantal azul", "camisa blanca", "30s"],
        "first_seen": "2026-01-15",
        "patterns": {
          "usual_arrival": "06:55",
          "usual_departure": "15:00",
          "common_zone": "detrás del mostrador"
        }
      }
    ],
    "suspicious": [
      {
        "id": "susp_001",
        "visual_tags": ["gorra negra", "camisa negra", "joven"],
        "first_seen": "2026-05-20",
        "incidents": 3,
        "last_seen": "2026-06-04 14:32",
        "notes": "Recibe producto sin pagar. Siempre viene los viernes."
      }
    ]
  },

  "daily_summaries": {
    "2026-06-05": {
      "date": "2026-06-05",
      "total_events": 156,
      "total_persons": 342,
      "alerts": 3,
      "confirmed": 1,
      "false_alarms": 2,
      "peak_hour": "12:30",
      "peak_persons": 12,
      "cameras_active": 1,
      "qwen_highlights": [
        "08:15 — Empleado llegó puntual, organizando productos",
        "12:30 — Pico de 12 personas, fila de 4 en caja",
        "14:22 — Alerta: persona con gorra negra recibió producto sin pasar por caja",
        "18:45 — Último cliente del día"
      ],
      "sent_at": "2026-06-06T06:00:00"
    }
  },

  "conversation_context": {
    "weaknesses": [
      "Empleado puede vender sin factura a amigos",
      "Dinero al bolsillo cerca de la caja"
    ],
    "agreed_rules": [
      "Alerta si empleado entrega producto sin pasar por caja",
      "Alerta si hay persona con gorra negra cerca del mostrador",
      "Alerta si hay movimiento después de las 7pm"
    ],
    "last_chat_summary": "Samuel está preocupado por robo interno. Pedro es el cajero principal. Hay una persona sospechosa con gorra negra que viene los viernes."
  }
}
```

---

## 3. FLUJO DE DATOS: Qwen → JSON → Eva RAG

### 3.1 Cuando Qwen analiza un grid (cada 16 frames con YOLO detection)

**ANTES** (actual):
```
Qwen → "Todo en orden" → Se guarda evento genérico
```

**DESPUÉS** (nuevo):
```
Qwen → JSON estructurado → Se guarda en camera daily_summary 
       → Se actualiza business.json → Eva puede consultarlo
```

### 3.2 Nuevo formato de respuesta de Qwen

El prompt de Qwen cambia para que devuelva un JSON estructurado:

```python
# Prompt nuevo para Qwen (en process_grid)
QWEN_GRID_PROMPT = """
Analiza este grid de {n} frames de seguridad de {zone}.

Responde SOLO con un JSON válido:
{{
  "description": "Descripción breve de lo que pasó (1-2 oraciones en español)",
  "persons": {person_count},
  "persons_details": [
    {{"role": "empleado/cliente/desconocido", "action": "qué está haciendo", "clothing": "descripción ropa"}}
  ],
  "activity_level": "bajo/medio/alto",
  "anomaly": true/false,
  "anomaly_detail": "si hay anomalía, describe cuál",
  "timestamp_context": "{time_range}"
}}

Reglas de vigilancia activas:
{rules}

Horario: {schedule_open}-{schedule_close}
{after_hours_note}
"""
```

### 3.3 Evento guardado (nuevo formato)

```json
{
  "event_id": "evt_1780421350_OJO-E17604",
  "user_id": "moXcjYsfYogCFfvHq0TmadF8ytt2",
  "camera_id": "OJO-E17604",
  "camera_name": "Cámara caja",
  "event_type": "normal",
  "timestamp": 1780421350,
  "datetime": "2026-06-06T08:15:00-04:00",
  "hour": "08:15",
  "day_of_week": "friday",
  
  "qwen_analysis": {
    "description": "Empleado organizando productos en el mostrador. Un cliente esperando.",
    "persons": 2,
    "persons_details": [
      {"role": "empleado", "action": "organizando productos", "clothing": "delantal azul, camisa blanca"},
      {"role": "cliente", "action": "esperando en fila", "clothing": "camiseta roja"}
    ],
    "activity_level": "bajo",
    "anomaly": false,
    "anomaly_detail": null
  },
  
  "yolo": {
    "count": 2,
    "detections": [
      {"class": "person", "confidence": 0.92, "bbox": [100, 200, 300, 500]},
      {"class": "person", "confidence": 0.87, "bbox": [400, 180, 550, 480]}
    ]
  },
  
  "frame_path": "/storage/users/{uid}/cameras/OJO-E17604/events/evt_1780421350_OJO-E17604.jpg",
  "has_image": true
}
```

---

## 4. EVA CHAT ENGINE — El Nuevo Módulo

### 4.1 Archivo: `eva/eva_chat_os.py`

```python
"""
Eva Chat OS — Motor principal de conversación con Eva.

Flujo:
1. Usuario envía mensaje (texto/voz/WhatsApp)
2. Eva clasifica la intención
3. Si necesita datos → busca en business.json (RAG)
4. Si necesita frames → busca en eventos por descripción/tiempo
5. Si necesita acción → ejecuta tool
6. Responde en contexto del negocio
7. Si hay frames → genera carrusel para el chat
"""

# ── TOOLS QUE EVA PUEDE USAR ──
TOOLS = [
    {
        "name": "search_events",
        "description": "Busca eventos por descripción, persona, hora o cámara. "
                       "Útil cuando el usuario pregunta por algo específico que pasó.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Búsqueda libre: 'persona con gorra negra', 'Pedro', 'alertas de ayer'"},
                "camera_id": {"type": "string", "description": "ID de cámara o null para todas"},
                "date": {"type": "string", "description": "Fecha: 'today', 'yesterday', '2026-06-05'"},
                "event_type": {"type": "string", "description": "violation, normal, night_alert, o null para todos"},
                "limit": {"type": "integer", "description": "Máximo de resultados (default: 10)"}
            },
            "required": ["query"]
        }
    },
    {
        "name": "get_daily_summary",
        "description": "Obtiene el resumen de un día específico. "
                       "Útil para '¿Cómo estuvo ayer?', 'Resumen del viernes'",
        "parameters": {
            "type": "object",
            "properties": {
                "date": {"type": "string", "description": "Fecha: 'today', 'yesterday', '2026-06-05'"}
            },
            "required": ["date"]
        }
    },
    {
        "name": "get_traffic_analysis",
        "description": "Analiza patrones de tráfico de personas. "
                       "Útil para '¿A qué hora hay más clientes?', 'Picos de tráfico'",
        "parameters": {
            "type": "object",
            "properties": {
                "days": {"type": "integer", "description": "Últimos N días (default: 7)"},
                "camera_id": {"type": "string", "description": "Cámara específica o null para todas"},
                "group_by": {"type": "string", "description": "hour, day, weekday"}
            }
        }
    },
    {
        "name": "find_person",
        "description": "Busca a una persona por descripción visual en los eventos. "
                       "Útil para 'Busca una persona con camisa blanca y gorra negra'",
        "parameters": {
            "type": "object",
            "properties": {
                "description": {"type": "string", "description": "Descripción visual: 'camisa blanca, gorra negra, delantal azul'"},
                "date": {"type": "string", "description": "Fecha o rango: 'today', 'yesterday', 'this_week'"},
                "camera_id": {"type": "string", "description": "Cámara específica o null"}
            },
            "required": ["description"]
        }
    },
    {
        "name": "get_camera_frames",
        "description": "Obtiene frames recientes de una cámara con análisis de Qwen. "
                       "Útil para '¿Qué ves ahora?', 'Muéstrame la caja'",
        "parameters": {
            "type": "object",
            "properties": {
                "camera_id": {"type": "string", "description": "ID de cámara"},
                "count": {"type": "integer", "description": "Cantidad de frames (default: 5)"},
                "time_range": {"type": "string", "description": "Rango: 'last_hour', 'today', 'yesterday'"}
            },
            "required": ["camera_id"]
        }
    },
    {
        "name": "send_daily_summary",
        "description": "Envía el resumen diario por WhatsApp al dueño. "
                       "Se ejecuta automáticamente cada mañana o cuando el usuario lo pida.",
        "parameters": {
            "type": "object",
            "properties": {
                "date": {"type": "string", "description": "Fecha del resumen: 'yesterday', 'today'"},
                "channel": {"type": "string", "description": "whatsapp, push, email"}
            }
        }
    },
    {
        "name": "update_business_context",
        "description": "Actualiza el contexto del negocio con nueva información. "
                       "Útil cuando el usuario dice cosas como 'Ahora abro los domingos'",
        "parameters": {
            "type": "object",
            "properties": {
                "field": {"type": "string", "description": "Campo a actualizar: schedule, concerns, rules, employee_count"},
                "value": {"type": "string", "description": "Nuevo valor"}
            },
            "required": ["field", "value"]
        }
    }
]
```

### 4.2 Flujo de conversación

```
Usuario: "Eva, busca una persona con camisa blanca y gorra negra"

1. CLASIFICACIÓN: find_person
2. TOOL CALL: find_person(description="camisa blanca, gorra negra", date="this_week")
3. BÚSQUEDA RAG:
   - Lee business.json → people.known y people.suspicious
   - Busca en eventos de la última semana
   - Filtra por qwen_analysis.persons_details.clothing
4. RESULTADO: 3 eventos encontrados
5. RESPUESTA DE EVA:
   "Encontré 3 coincidencias esta semana:
   
   📅 Viernes 14:32 — Cámara caja
   Persona con camisa blanca y gorra negra recibió producto.
   [Ver frame]
   
   📅 Miércoles 11:15 — Cámara caja  
   Persona similar, solo mirando.
   [Ver frame]
   
   📅 Lunes 08:45 — Cámara caja
   Empleado con camisa blanca (sin gorra).
   [Ver frame]
   
   ¿Es la persona del viernes la que buscas?"
```

---

## 5. RESUMEN DIARIO AUTOMÁTICO

### 5.1 Scheduler (cron job cada mañana a las 6am)

```python
# scripts/daily_summary_job.py
"""
Cada mañana a las 6:00am:
1. Lee todos los eventos del día anterior
2. Agrega por cámara: total personas, pico, alertas
3. Genera resumen con Qwen (opcional, para narrativa)
4. Guarda en business.json → daily_summaries
5. Envía por WhatsApp al dueño
"""

RESUMEN_TEMPLATE = """
📊 *Resumen de {date}* — {business_name}

📷 *Cámara {camera_name}:*
• {total_persons} personas detectadas
• Pico: {peak_hour} ({peak_persons} personas)
• {alerts} alertas ({confirmed} confirmadas)

{highlights}

💡 *Sugerencia de Eva:*
{suggestion}

_Ver detalles: https://ojoia.com.do/_
"""
```

### 5.2 Integración con el grid analysis

Cada vez que Qwen analiza un grid, se actualiza el `today_summary` de la cámara:

```python
# En process_grid(), después del análisis de Qwen:
def update_camera_daily_summary(user_id, camera_id, qwen_result, yolo_count):
    """Actualiza el resumen diario de la cámara con cada análisis."""
    today = datetime.now().strftime("%Y-%m-%d")
    business = load_business_json(user_id)
    
    cam = business["cameras"].get(camera_id, {})
    if "today_summary" not in cam:
        cam["today_summary"] = {
            "date": today,
            "total_events": 0,
            "total_persons": 0,
            "alerts": 0,
            "qwen_descriptions": []
        }
    
    summary = cam["today_summary"]
    summary["total_events"] += 1
    summary["total_persons"] += yolo_count
    
    # Guardar descripción de Qwen con hora
    summary["qwen_descriptions"].append({
        "time": datetime.now().strftime("%H:%M"),
        "persons": yolo_count,
        "description": qwen_result["description"]
    })
    
    # Actualizar pico
    current_hour = datetime.now().strftime("%H:%M")
    if yolo_count > summary.get("peak_persons", 0):
        summary["peak_hour"] = current_hour
        summary["peak_persons"] = yolo_count
    
    save_business_json(user_id, business)
```

---

## 6. BÚSQUEDA POR DESCRIPCIÓN (RAG de frames)

### 6.1 Índice de búsqueda

Cada evento guardado tiene `qwen_analysis.description` y `qwen_analysis.persons_details`. Eva puede buscar por:

- **Descripción libre**: "persona con gorra negra"
- **Hora**: "a las 2 de la tarde"
- **Día**: "ayer", "viernes pasado"
- **Cámara**: "en la caja"
- **Número de personas**: "cuando había 5 personas"
- **Actividad**: "organizando productos", "cobrando"

### 6.2 Implementación de búsqueda

```python
def search_events_rag(user_id: str, query: str, date: str = None, 
                       camera_id: str = None, limit: int = 10) -> list:
    """
    Busca eventos usando el JSON de cada evento como RAG.
    Filtra por:
    1. Coincidencia de texto en qwen_analysis.description
    2. Coincidencia en persons_details.clothing/action
    3. Rango de fecha/hora
    4. Cámara
    
    Retorna lista de eventos con frames adjuntos.
    """
    business = load_business_json(user_id)
    results = []
    
    # Determinar rango de fechas
    date_range = parse_date_range(date)  # "today", "yesterday", "this_week"
    
    # Buscar en todas las cámaras (o la especificada)
    cameras = [camera_id] if camera_id else business["cameras"].keys()
    
    for cam_id in cameras:
        events_dir = f"{STORAGE_ROOT}/users/{user_id}/cameras/{cam_id}/events"
        if not os.path.exists(events_dir):
            continue
            
        for event_file in sorted(os.listdir(events_dir), reverse=True):
            if not event_file.endswith(".json"):
                continue
                
            with open(f"{events_dir}/{event_file}") as f:
                event = json.load(f)
            
            # Filtrar por fecha
            if date_range and not is_in_date_range(event["timestamp"], date_range):
                continue
            
            # Buscar coincidencia en descripción
            qwen = event.get("qwen_analysis", {})
            searchable_text = " ".join([
                qwen.get("description", ""),
                " ".join([p.get("clothing", "") + " " + p.get("action", "") 
                         for p in qwen.get("persons_details", [])])
            ]).lower()
            
            # Coincidencia simple (después se puede mejorar con embeddings)
            query_terms = query.lower().split()
            matches = sum(1 for term in query_terms if term in searchable_text)
            
            if matches > 0:
                results.append({
                    "event": event,
                    "relevance": matches / len(query_terms),
                    "matched_terms": [t for t in query_terms if t in searchable_text]
                })
    
    # Ordenar por relevancia
    results.sort(key=lambda x: x["relevance"], reverse=True)
    return results[:limit]
```

---

## 7. CARRUSEL DE FRAMES EN EL CHAT

### 7.1 Formato de respuesta con frames

Cuando Eva encuentra frames, los presenta así:

```
Eva: "Encontré 3 eventos con personas que coinciden con 'camisa blanca y gorra negra':

┌─────────────────────────────────────────┐
│ 📅 Viernes 6/6 — 14:32 — Cámara caja   │
│ 👥 3 personas                           │
│ 📝 Persona con gorra negra recibió     │
│    producto sin pasar por caja          │
│ [🖼️ Frame] [🖼️ Grid completo]          │
├─────────────────────────────────────────┤
│ 📅 Miércoles 4/6 — 11:15 — Cámara caja │
│ 👥 2 personas                           │
│ 📝 Persona similar mirando productos    │
│ [🖼️ Frame]                              │
├─────────────────────────────────────────┤
│ 📅 Lunes 2/6 — 08:45 — Cámara caja     │
│ 👥 1 persona                            │
│ 📝 Empleado organizando (sin gorra)     │
│ [🖼️ Frame]                              │
└─────────────────────────────────────────┘

¿Quieres que te muestre el video del viernes?"
```

### 7.2 Implementación en el frontend

```javascript
// En el chat de Eva, cuando la respuesta incluye frames:
function renderEventCarousel(events) {
    return `
    <div class="eva-carousel">
        ${events.map(evt => `
        <div class="carousel-card">
            <div class="card-time">📅 ${evt.datetime} — ${evt.camera_name}</div>
            <div class="card-persons">👥 ${evt.persons} personas</div>
            <div class="card-desc">📝 ${evt.description}</div>
            <div class="card-frames">
                <img src="${evt.frame_url}" class="frame-thumb" 
                     onclick="openViewer('${evt.event_id}')">
                ${evt.grid_url ? `<img src="${evt.grid_url}" class="grid-thumb">` : ''}
            </div>
            <div class="card-actions">
                <button onclick="playClip('${evt.event_id}')">▶ Ver clip</button>
                <button onclick="downloadFrame('${evt.event_id}')">⬇ Descargar</button>
            </div>
        </div>
        `).join('')}
    </div>`;
}
```

---

## 8. INTEGRACIÓN CON EL FLUJO DE CONFIGURACIÓN INICIAL

### 8.1 El chat inicial con Eva (ya existe, se mejora)

Cuando el usuario configura por primera vez:

```
Eva: "Hola Samuel 👋 Vi que tienes Super colmado La Esquina. 
      Vamos a configurar tu primera cámara.

      Antes de conectar el hardware, cuéntame:
      ¿Cuáles son tus principales preocupaciones de seguridad?"

Samuel: "Que los empleados me roben, que vendan sin factura"

Eva: "Entiendo. Dos problemas comunes en colmados. 
      Voy a crear tu perfil de vigilancia con eso en mente.
      
      📋 *Debilidades identificadas:*
      • Robo interno por empleados
      • Ventas sin factura
      
      Cuando conectes la cámara, voy a analizar la imagen 
      y proponerte reglas específicas para tu negocio.
      
      Ahora, vamos a conectar la cámara..."
```

### 8.2 Eva extrae el contexto y guarda en business.json

```python
# Después del chat inicial, Eva actualiza:
business["conversation_context"] = {
    "weaknesses": ["robo interno por empleados", "ventas sin factura"],
    "agreed_rules": [],  # Se llenan después del análisis de imagen
    "last_chat_summary": "Samuel está preocupado por robo interno..."
}
```

### 8.3 El prompt de Qwen se inyecta con el contexto del negocio

```python
# Cuando Qwen analiza grids, el prompt incluye:
system_prompt = f"""
{business['cameras'][camera_id]['system_prompt']}

DEBILIDADES CONOCIDAS (del chat con el dueño):
{chr(10).join(business['conversation_context']['weaknesses'])}

REGLAS ACORDADAS:
{chr(10).join(business['conversation_context']['agreed_rules'])}

PERSONAS CONOCIDAS:
{format_known_people(business['people'])}
"""
```

---

## 9. PLAN DE IMPLEMENTACIÓN

### Fase 1: Base del RAG (Semana 1)
1. Crear `business.json` estructurado (migrar de `user.json`)
2. Modificar `save_event_to_disk()` para guardar el nuevo formato con `qwen_analysis` estructurado
3. Modificar `process_grid()` para que Qwen devuelva JSON estructurado
4. Crear `update_camera_daily_summary()` — se llama en cada análisis de grid

### Fase 2: Eva Chat Engine (Semana 2)
1. Crear `eva/eva_chat_os.py` con el motor de conversación
2. Implementar `search_events_rag()` — búsqueda por descripción
3. Implementar `find_person()` — búsqueda por tags visuales
4. Implementar `get_daily_summary()` — resumen de un día
5. Modificar endpoint `/config/chat` para usar el nuevo engine

### Fase 3: Resumen Diario + Carrusel (Semana 3)
1. Crear `scripts/daily_summary_job.py` (cron a las 6am)
2. Implementar carrusel de frames en el chat del frontend
3. Implementar `get_traffic_analysis()` — análisis de patrones
4. Conectar resumen diario con envío por WhatsApp

### Fase 4: WhatsApp + Voz (Semana 4)
1. Integrar Baileys para recibir/enviar mensajes
2. Implementar `send_daily_summary()` por WhatsApp
3. Agregar VAD + TTS del CHATRD para voz bidireccional

---

## 10. RESUMEN DE ARCHIVOS A CREAR/MODIFICAR

| Archivo | Acción | Descripción |
|---|---|---|
| `eva/eva_chat_os.py` | **CREAR** | Motor principal de chat con tool use |
| `eva/tools.py` | **CREAR** | Implementación de cada tool (search_events, find_person, etc.) |
| `orchestrator.py` | **MODIFICAR** | Qwen devuelve JSON estructurado, guardar en business.json |
| `api_eva.py` | **MODIFICAR** | Nuevo endpoint `/api/chat/eva` para el chat OS |
| `scripts/daily_summary.py` | **CREAR** | Job de resumen diario (cron) |
| `scripts/business_json.py` | **CREAR** | Migración de user.json → business.json |
| `frontend/chatUI.js` | **MODIFICAR** | Carrusel de frames, renderizado de eventos |
| `frontend/app.js` | **MODIFICAR** | Integración del nuevo chat con Eva |
| `baileys/gateway.js` | **CREAR** | Gateway WhatsApp (del CHATRD) |
