# Nodo OjoIA — Guía de Conexión Multi-Nodo

## Arquitectura actual

Este servidor (**ojoia**, IP local: 127.0.0.1) tiene el panel de control en
`http://127.0.0.1:9001` (megapanel) y expone el bus de modelos en
`http://127.0.0.1:8200/v1` (service_bus con auth bearer).

El **megapanel** está diseñado para gestionar múltiples nodos (ver
`CINEIA_AGENT_URL` en `/opt/ojoia/config/ojoia.env`). Ya hay una entrada
para `cineia` (puerto 8030). Para añadir **otro nodo gemelo** (sam-2 o el
que sea), sigue estos pasos.

## Estado del panel central (este nodo)

- Puerto: 9001
- Auth: `MEGAPANEL_TOKEN` desde `/opt/ojoia/config/ojoia.env`
- Servicios Docker: `ai-qwen-9b-1`, `qwen-7b`, `qwen-35b-a3b`,
  `whisper-turbo`, `yolo-pose` (mapeo en `DOCKER_MAP` en `megapanel.py`)
- Service Bus: puerto 8200, OpenAI-compatible, auth bearer
- API Keys de billing: `client_id="kilo_local"` plan enterprise (generadas
  en Redis con `BillingStore.create_key()`)

## Cómo añadir un nuevo nodo al panel

### 1. En el nodo NUEVO (la otra PC)

#### a) Clonar el repo y pull los últimos cambios

```bash
cd /opt/ojoia
git clone https://github.com/samuel0117-coder/ojoia-eva.git code_new
cd code_new
git pull origin main
```

#### b) Configurar `ojoia.env` (NO commitear secretos)

```bash
sudo cp /opt/ojoia/config/ojoia.env /opt/ojoia/config/ojoia.env.backup  # si ya existe
sudo tee /opt/ojoia/config/ojoia.env <<EOF
REDIS_PASSWORD=<misma_redis_password_que_este_nodo_si_comparten_o_generar_una_nueva>
REDIS_URL=redis://:<password>@127.0.0.1:6379/0
CINEIA_AGENT_URL=http://<IP_ESTE_NODO>:8300
NODE_ID=<nombre_unico_del_nuevo_nodo>
CINEIA_AGENT_TOKEN=<token_compartido>
MEGAPANEL_TOKEN=oj_admin_<generar_con_secrets.token_urlsafe(32)>
EOF
sudo chmod 600 /opt/ojoia/config/ojoia.env
```

#### c) Instalar el service bus y el agent remoto

```bash
sudo cp /opt/ojoia/code_new/services/ojoia-bus.service /etc/systemd/system/
sudo cp /opt/ojoia/code_new/services/agent-remote.service /etc/systemd/system/  # si existe
sudo systemctl daemon-reload
sudo systemctl enable --now ojoia-bus
```

#### d) Exponer el agent remoto en `0.0.0.0:8300` (para que el panel central
lo pueda consultar). En `/etc/cloudflared/config.yml` del nuevo nodo, añadir:

```yaml
ingress:
  - hostname: <nuevo-nodo>.ojoia.com.do
    service: http://localhost:8300
  - hostname: bus-<nuevo-nodo>.ojoia.com.do
    service: http://localhost:8200
```

### 2. En ESTE nodo (panel central)

#### a) Añadir el nuevo nodo a la lista `SERVICES` en `megapanel.py`

Buscar la lista `SERVICES = [` (alrededor de línea 119) y añadir entradas
para los servicios del nuevo nodo con `"node": "<nombre_unico>"`:

```python
# ── Nodo <NOMBRE_NUEVO> ──
{"id": "qwen9b-<nuevo>.service", "node": "<nombre_nuevo>", "port": 8018, "level": "user", "gpu": 0, "name": "Qwen 9B (<nuevo>)", "kind": "llm"},
{"id": "qwen-<nuevo>.service",   "node": "<nombre_nuevo>", "port": 8004, "level": "user", "gpu": 1, "name": "Qwen 7B (<nuevo>)", "kind": "llm"},
# ... etc
```

Si los servicios del nuevo nodo son Docker containers, añadir también al
`DOCKER_MAP`:

```python
DOCKER_MAP = {
    "qwen9b.service":  "ai-qwen-9b-1",
    "qwen.service":    "qwen-7b",
    "qwen35b.service": "qwen-35b-a3b",
    "qwen9b-<nuevo>.service":  "ai-qwen-9b-1-<nuevo>",
    # ...
}
```

#### b) Reiniciar el megapanel

```bash
sudo systemctl restart megapanel
```

#### c) El panel hará auto-discovery vía `CINEIA_AGENT_URL` configurado en
`/opt/ojoia/config/ojoia.env`. Para añadir múltiples nodos, ampliar la
variable de entorno con una lista separada por comas y actualizar
`megapanel.py` para iterar sobre todos.

### 3. Verificar

Abrir `http://server-admin.ojoia.com.do` (o `http://<IP_ESTE_NODO>:9001`)
y verificar que la columna "Node" muestra los servicios del nuevo nodo.

Los botones ▶ ■ ↻ en cada servicio controlan el contenedor Docker
correspondiente en el nodo donde corre (vía `DOCKER_MAP`).

## Endpoints clave del panel

| Endpoint | Método | Descripción |
|----------|--------|-------------|
| `/api/status` | GET | Snapshot completo (gpus, servicios, energía, etc.) |
| `/api/control/{service_id}/{action}` | POST | start/stop/restart |
| `/api/nodes` | GET | Estado de todos los nodos |
| `/api/billing/clients` | GET | Uso por cliente (Kilo keys) |
| `/api/billing/stats` | GET | Stats agregadas (24h) |

## Cambios recientes importantes (commits)

1. **Energía total del sistema**: CPU (RAPL) + GPUs (nvidia-smi) = total
   - `measure_cpu_power_w()` en `megapanel.py:163` lee RAPL con fallback
   - Sección `power` en `/api/status`
   - Tarjeta "⚡ Energía" en el front (overview + infra)

2. **Service bus OpenAI-compatible** con auth bearer:
   - Endpoint `/v1/models` y `/v1/chat/completions` en `service_bus.py`
   - Routing por modelo del body
   - Soporte streaming con `reasoning_content` (thinking mode)

3. **Mapeo Docker** para control de contenedores desde el panel:
   - `DOCKER_MAP` en `megapanel.py:167`
   - `level=system` services ahora detectan Docker y ejecutan
     `docker start/stop/restart`

4. **Regla udev RAPL** (para medición real de CPU watts):
   - `/home/sam/setup/ojoia/udev/99-rapl-power.rules`
   - `/home/sam/setup/ojoia/services/rapl-permissions.service`

5. **Chat HTML de prueba**:
   - `/home/sam/chatrd/test_qwen35b.html` (sirve en `:8200/test/`)
   - Thinking visible, historial persistente, contexto entre mensajes

## Problema conocido (no resuelto aquí)

Otro agente configuró una Access Policy en un team diferente de
Cloudflare (`aged-field-5db3.cloudflareaccess.com`) que está bloqueando
`api.ojoia.com.do`. Esto hace que el tunnel no reciba tráfico (530).
**Fix**: revisar la Access App en el dashboard de Cloudflare y excluir
`api.ojoia.com.do` o mover el tunnel a otro team.
