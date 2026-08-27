# Estado y Plan del Sistema de Billing — OjoIA

> Última actualización: 2026-08-21

## Arquitectura actual (funcionando)

### Service Bus (`service_bus.py`) — puerto 8200
- Única puerta de entrada a TODOS los modelos (qwen7b, qwen9b, qwen35b, whisper, yolo)
- Auth obligatoria: API key `ojoia_live_*` → 401 si falta/invalida
- Rate limiting + quota por plan (Redis, sliding window)
- Token tracking en Redis (tokens, costo, requests por cliente/modelo/día/mes)
- **Streaming SSE real** (StreamingResponse con yield por chunk) — token a token
- Endpoint `/v1/*` para Kilo Code (OpenAI-compatible, routa a qwen9b)
- Serving estático `/test/*` → `/home/sam/chatrd/` (página de test)

### Billing (`billing.py`)
- API keys por cliente (create, validate, revoke, list)
- Precios por modelo: `MODEL_PRICES` (dict en código, NO editable en caliente aún)
- Planes: `PLANS` (free/dev/pro/enterprise — quotas, rpm)
- Tracking en Redis: `ojoia_billing:usage:*`, `ojoia_billing:cost:*`

### Request Log (`billing_log.py`) — NUEVO
- SQLite en `/home/sam/ojoia-billing-db/billing.db` (NVMe, 189G libres)
- Guarda cada request: metadata (cliente, modelo, tokens, costo, latency, status, timestamp) + contenido (prompt + response completos)
- Rating del cliente (up/down/neutral) para medir calidad de respuestas
- Auto-purge: registros >30 días (configurable `BILLING_LOG_RETENTION_DAYS`)
- Stats para dashboard: totales, por modelo, por cliente, serie horaria, votos
- Storage info: tamaño DB, registros totales, disco libre

### Megapanel (`megapanel.py`) — puerto 8030
- Panel admin actual con billing básico:
  - `/api/billing/clients` — uso mensual por cliente + quota
  - `/admin/keys` (GET/POST) — listar/crear API keys
  - `/admin/keys/revoke` (POST) — revocar key
  - **FALTAN**: endpoints para editar precios/planes, ver log de requests, ver mensajes, ratings, dashboard

### Configuración Kilo Code
- Proveedor `ojoia` en `kilo.jsonc` → `http://127.0.0.1:8200/v1` con API key
- Proveedor `local` (LEGACY, sin billing) → `http://127.0.0.1:8090/v1`

### Página de test
- `/home/sam/chatrd/test_qwen35b.html` (única copia)
- Servida por el bus en `http://localhost:8200/test/test_qwen35b.html`
- Llama al bus relativo (mismo dominio), streaming token a token
- 3 modelos: qwen35 (9B), qwen36-35b-a3b (35B), qwen3-7b (7B)

### Cloudflare Tunnel
- `/etc/cloudflared/config.yml` con ingress rules
- `chatrd-test.ojoia.com.do` → :8200 (SSL funciona, pero routing 404 pendiente)
- Otros subdominios sobre `*.ojoia.com.do` funcionan

## Plan de trabajo pendiente

### Fase 2: Precios y planes editables en caliente
- [ ] Mover `MODEL_PRICES` y `PLANS` de billing.py a Redis (key `ojoia_billing:config:*`)
- [ ] `billing.py`: cargar config de Redis al startup, fallback a defaults
- [ ] `billing.py`: métodos `update_model_price()`, `update_plan()`, `get_config()`
- [ ] Que el service bus lea precios live (no del import estático)

### Fase 3: Endpoints del panel
- [ ] `GET /api/billing/config` — precios + planes actuales
- [ ] `PUT /api/billing/prices` — actualizar precios por modelo
- [ ] `PUT /api/billing/plans` — actualizar planes
- [ ] `GET /api/billing/log` — log de requests con filtros (cliente, modelo, errores)
- [ ] `GET /api/billing/log/{id}` — detalle de un request (prompt + response)
- [ ] `PUT /api/billing/log/{id}/rating` — setear rating
- [ ] `GET /api/billing/stats` — stats para dashboard
- [ ] `GET /api/billing/storage` — info de almacenamiento
- [ ] `POST /api/billing/purge` — purge manual

### Fase 4: UI del panel (megapanel.py HTML)
- [ ] Tab de Dashboard: tarjetas KPI (tokens, costo, requests, errores), gráfico horario, top modelos, top clientes
- [ ] Tab de Request Log: tabla con filtros, click → modal con prompt+response completo, botones up/down rating
- [ ] Tab de Clientes + Keys: crear/revocar keys, cambiar plan, ver uso detallado
- [ ] Tab de Precios + Planes: editor de precios input/output por modelo, editor de quotas/rpm por plan, guardar → live
- [ ] Mostrar storage usado/disponible + retención

### Fase 5: Auto-purge + storage
- [ ] Worker/timer que corra `purge_old()` periódicamente (ya integrado en service_bus)
- [ ] Mostrar storage en panel (ya en `get_storage_info()`)

### Fase 6: Detección de abuso
- [ ] Alertas en panel: picos de uso, muchas requests rápidas, quota cerca del límite
- [ ] Opción de bloqueo temporal de cliente desde el panel
- [ ] Notificación (log/email/webhook) en detección

## Configuración técnica

- Python venv: `/opt/ojoia/venv`
- Redis: `redis://:hq1V4pQr1c99AWYYAIGBnCu7695jL75@127.0.0.1:6379/0`
- Prefix Redis: `ojoia_billing`
- SQLite: `/home/sam/ojoia-billing-db/billing.db` (mover a disco de data cuando organices discos)
- API key de test: `ojoia_live_DoOkAJ4xGme31WgljVcXfQleStbdiGn9iNWSFarFI-w` (cliente `test_page`, plan `dev`)
- Servicios systemd: `ojoia-bus.service` (system), `tunnel.service` (system)

## Cómo continuar
1. Reiniciar `ojoia-bus.service` tras cada cambio en service_bus.py
2. Reiniciar megapanel (verificar cómo se gestiona) tras cambios en megapanel.py
3. Probar con la página de test: `http://localhost:8200/test/test_qwen35b.html`
4. Ver billing en Redis: ver comando en README o usar `billing_log.py` stats
