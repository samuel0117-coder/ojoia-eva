# Conexión CineIA → Modelos de OjoIA (facturado)

El nodo **OjoIA** (este servidor, IP de LAN `10.0.0.71`) expone un **Service Bus**
de modelos en `http://10.0.0.71:8205`. El nodo **CineIA** (IP de LAN
`10.0.0.44`) puede consumir esos modelos por la red local, y **cada request
queda registrada y facturada** en Redis de OjoIA mediante una API key propia.

```
[CineIA 10.0.0.44]  ──LAN──►  http://10.0.0.71:8205/{backend}/v1/chat/completions
                                   │  Authorization: Bearer <OJOIA_BUS_KEY>
                                   ▼
                            Service Bus OjoIA (billing + rate-limit + proxy al modelo)
```

> Nota: el bus escucha SOLO en la IP de LAN (10.0.0.71), no en internet.
> Requiere `Bearer` válido. Si CineIA no está en la subnet 10.0.0.0/24,
> usar en su lugar el túnel público (`api.ojoia.com.do`) con la misma key.

## 1. En el nodo CineIA: clonar / actualizar el repo

```bash
cd /opt/ojoia
git clone https://github.com/samuel0117-coder/ojoia-eva.git code 2>/dev/null || \
  (cd code && git pull origin main)
```

## 2. Configurar las variables de entorno

```bash
export OJOIA_BUS_URL="http://10.0.0.71:8205"
export OJOIA_BUS_KEY="ojoia_live_H_wmOM1EspjnjYx9TOBkw6A0eFvHBw8jLfFVZG3xquQ"
```

> La key `ojoia_live_H_...` es del cliente `cineia` (plan enterprise) y ya está
> creada en OjoIA. Para rotarla: en OjoIA corre
> `POST /admin/keys` del megapanel (`server-admin.ojoia.com.do`).

## 3. Usar el cliente (Python)

```python
from cineia_agent.ojoia_models_client import OjoIAModels  # o copia suelto

m = OjoIAModels()  # lee OJOIA_BUS_URL / OJOIA_BUS_KEY del entorno

# Chat con qwen35b
r = m.chat("qwen35b", [{"role": "user", "content": "Resume esta escena"}])
print(r["choices"][0]["message"]["content"])

# Health del bus
print(m.health())
```

Endpoint crudo equivalente:

```bash
curl http://10.0.0.71:8205/qwen35b/v1/chat/completions \
  -H "Authorization: Bearer $OJOIA_BUS_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"qwen36-35b-a3b","messages":[{"role":"user","content":"hola"}],"max_tokens":50}'
```

## 4. Backends disponibles (en OjoIA)

| backend | puerto local OjoIA | modelo para facturar |
|---------|-------------------|----------------------|
| `qwen7b`  | 8004 | `qwen3-7b` |
| `qwen9b`  | 8018 | `qwen35` |
| `qwen35b` | 8019 | `qwen36-35b-a3b` |
| `whisper` | 8008 | `whisper-large-v3` |
| `yolo`    | 8002 | `yolo-pose` |

Si un backend aparece caído (connection failed), el contenedor Docker
correspondiente no está corriendo en OjoIA; avisar para levantarlo.

## 5. Ver el consumo facturado

En OjoIA, el megapanel (`server-admin.ojoia.com.do` → sección usage) y Redis
muestran el uso del cliente `cineia`:

```
ojoia_billing:usage:cineia:total
ojoia_billing:cost:cineia:total
ojoia_billing:usage:cineia:model:qwen36-35b-a3b
```

## 6. Levantar el agente CineIA (métricas/control)

```bash
cd /opt/ojoia/code/backend/cineia-agent
/opt/ojoia/venv/bin/python cineia-agent.py   # :8300
# o con systemd user:
systemctl --user enable --now cineia-agent.service
```
