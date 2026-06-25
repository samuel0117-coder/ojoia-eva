# Migración Arquitectura Testigo Puro - Eva v7

## Fecha: 2026-06-25

## Problema Original

Eva estaba diseñada como "juez" — decidía si algo era "violación" o "anomalía".
Esto causaba:
- Falsas alarmas que acusaban a empleados sin razón
- Alucinaciones de Qwen inventando eventos no vistos
- Notificaciones agresivas tipo "🚨 ALERTA"
- El sistema juzgaba a personas sin contexto del dueño

## Nueva Arquitectura: Testigo Puro

### Principio fundamental
**Qwen es TESTIGO, no juzga.** Solo describe hechos visibles.
El sistema NUNCA acusa. El usuario decide si algo es falta o no.

### Separación de responsabilidades

| Componente | Rol | NO hace |
|------------|-----|---------|
| **Qwen (Testigo)** | Describe lo que ve en lenguaje natural | No juzga, no acusa |
| **Sistema (keywords)** | Detecta palabras clave en la descripción | No inventa, no asume intención |
| **Usuario** | Juzga si algo es falta o no | No recibe acusaciones del sistema |
| **Eva (Chat)** | Presenta datos, permite configurar | No juzga, no acusa |

## Cambios Realizados

### 1. Backend (`/home/sam/backend/`)

#### `api_eva.py`
- Agregado `sys.path.insert(0, _BACKEND_DIR)` al inicio para imports correctos
- Guardado de `latest_raw.jpg` siempre (sin importar detección YOLO)
- Endpoint `/frames/latest-raw.jpg` para streaming en vivo
- `thumb_url` cambiado a URL relativa (`/api/event-thumb/...`)

#### `orchestrator.py`
- Agregado `sys.path.insert(0, _BACKEND_DIR)` al inicio
- Prompt de Qwen cambiado a "testigo puro" (nunca juzga)
- Nuevo campo `counts` en JSON: `clientes`, `platos_visibles`, `bebidas_visibles`, `fundas_visibles`
- Nuevo campo `attention_hits`: frases de atención que coincidieron
- `_detect_attention_hits()` reemplaza `_apply_rules()` — ya no juzga, solo observa
- `_is_scene_unchanged()`: optimización de tokens (no llama a Qwen si la escena no cambió)

#### `eva/camera_builder.py`
- Reescrito con `VIGILANCE_TEMPLATES` por vertical (restaurante, colmado, finca, entrada, almacén)
- `build_witness_prompt()` reemplaza `build_vigilance_prompt()` — prompt observacional
- `normalize_camera_vigilance_config()` usa `attention_phrases` y `owner_notes`
- Alias: `build_vigilance_prompt = build_witness_prompt` (compatibilidad)

#### `eva/eva_chat.py`
- Fase `DONE` ahora delega a `handle_eva_chat_os()` (modo OS)
- Agregado `sys.path.insert` para imports correctos
- Fix: mensajes de error usan `text` en vez de `message` (variable no definida)

#### `eva/eva_chat_os.py`
- Eventos en `all_events` ahora incluyen `thumb_url`
- Import relativo: `from .eva_setup_flow import...`

#### `eva/tools.py`
- `tool_get_activity_summary()` reescrito: cuenta `platos`, `bebidas`, `fundas`, `clientes` en vez de "violations"
- `tool_find_anomalies()` busca `attention_hits` en vez de `violation`
- `tool_search_events()` retorna `thumb_url`
- Agregado `resolve_user_events_dirs()` (función faltante)

#### `eva/eva_setup_flow.py`
- Agregado `SETUP_PHASES` (lista de fases de configuración)

### 2. Frontend (`frontend/`)

#### `app-v12.js`
- Alertas en tab cámara: más grandes, con descripción y botones "Ver detalle" / "Falsa alarma"
- Eventos usan `attention_hits` en vez de `violation`
- Modal de evento muestra `attention_hits` y modo centinela
- `go()` acepta `eventId` para redirigir y abrir evento específico
- `_pageEvents()` recibe `openEventId` para abrir evento automáticamente

#### `eva-chat-v5.js`
- `openEventDetail()` redirige a tab eventos y abre evento
- Eventos muestran `thumb_url` en el carrusel
- Modal muestra `attention_hits` y botón "Falsa alarma"

## Problemas Encontrados y Solucionados

### 1. ModuleNotFoundError: No module named 'eva'
**Causa**: El servidor se ejecutaba desde un directorio donde `sys.path` no incluía `/home/sam/ai_system/`
**Solución**: Agregar `sys.path.insert(0, _BACKEND_DIR)` al inicio de `api_eva.py` y `orchestrator.py`

### 2. ImportError: cannot import name 'build_vigilance_prompt'
**Causa**: El `api_eva.py` viejo importaba `build_vigilance_prompt` pero el nuevo `camera_builder.py` exporta `build_witness_prompt`
**Solución**: Agregar alias `build_vigilance_prompt = build_witness_prompt` al final de `camera_builder.py`

### 3. ImportError: cannot import name 'resolve_user_events_dirs'
**Causa**: Función perdida durante la migración entre repos
**Solución**: Crear función en `tools.py`

### 4. ImportError: cannot import name 'SETUP_PHASES'
**Causa**: Variable perdida durante la migración
**Solución**: Crear lista en `eva_setup_flow.py`

### 5. Servidor se iniciaba pero no respondía
**Causa**: El proceso se colgaba durante la imports por timeout del shell
**Solución**: Usar `nohup` con redirección de stdin desde `/dev/null`

### 6. PYTHONPATH no se heredaba con nohup/disown
**Causa**: El shell pierde variables de entorno al hacer `&` y `disown`
**Solución**: Usar `sys.path.insert` directamente en el código Python

## Cómo Iniciar el Servidor

```bash
cd /home/sam/backend
nohup /home/sam/ai_system/venv/bin/python -m uvicorn api_eva:app \
  --host 0.0.0.0 --port 8005 --log-level warning \
  > /tmp/api_eva.log 2>&1 &
disown
```

## Cómo Verificar

```bash
curl -s http://localhost:8005/health
# Debe retornar: {"status":"ok","service":"eva-api","version":"7.0"}
```

## Archivos Importantes

- `/home/sam/backend/api_eva.py` — Punto de entrada
- `/home/sam/backend/orchestrator.py` — Lógica de vigilancia
- `/home/sam/backend/eva/eva_chat.py` — Chat de Eva (setup + OS)
- `/home/sam/backend/eva/eva_chat_os.py` — Chat modo OS
- `/home/sam/backend/eva/camera_builder.py` — Configuración de cámaras
- `/home/sam/backend/eva/tools.py` — Herramientas de Eva
- `/home/sam/backend/eva/eva_setup_flow.py` — Flujo de configuración
