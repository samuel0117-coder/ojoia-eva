# Megapanel Centralizado Multi-Nodo (Firebase Firestore)

Guía para que **cada nodo** alimente el megapanel centralizado y controle
servicios desde un **único panel web** en Firebase.

## Arquitectura (sin Cloud Functions)

Cada nodo corre su propio `megapanel.py` local (`:9001`) pero además:

- **PUSH** cada 5s de su estado → Firestore `/nodes/{node_id}/status`
- **POLLING** de comandos en `/control/{node_id}/cmds/*` → los ejecuta y escribe el resultado

El **panel web** (SPA en Firebase Hosting) lee todos los nodos y escribe comandos.

```
[Panel web]  — megapanel-ojoia.web.app —  (Firebase Hosting)
   │  lee /nodes/* + escribe /control/*
   ▼
[Firestore] ojoia-67216
   ├─ /nodes/{node_id}/status        ← cada nodo escribe su estado (5s)
   └─ /control/{node_id}/cmds/{cmd}  ← SPA escribe, nodo ejecuta y responde
        │
        ├──────────────┬──────────────┐
        ▼              ▼              │
   [cineia]        [ojoia]            │  (nodos: push + poll vía SDK admin)
   :9001           :9001              │
   push+poll       push+poll          ▼
```

## Requisitos en cada nodo

1. `firebase-admin` instalado en el venv del megapanel.
2. Service account key de Firebase en `/opt/ojoia/config/firebase-key.json`
   (o en `/home/sam/ai_system/firebase-key.json`).
3. `ojoia_sync.py` junto a `megapanel.py`.

## Pasos en el NODO NUEVO

### 1) Traer el código actualizado
```bash
cd /opt/ojoia
# La rama por defecto del repo es `master` (origin/HEAD -> origin/master).
# La integración del megapanel cluster vive en master, NO en main.
git clone https://github.com/samuel0117-coder/ojoia-eva.git code 2>/dev/null || \
  (cd code && git fetch && git pull --rebase origin master)
```

### 2) Configurar `ojoia.env`
```bash
sudo tee /opt/ojoia/config/ojoia.env <<EOF
NODE_ID=<tu_nombre_unico>          # ej: ojoia
MEGAPANEL_TOKEN=oj_admin_<genera>
CINEIA_AGENT_URL=http://<IP_del_otro>:8300
CINEIA_AGENT_TOKEN=<compartido>
REMOTE_NODE_ID=<id_del_nodo_remoto>
REDIS_URL=redis://127.0.0.1:6379/0
SERVICE_BUS_PORT=8200
EOF
sudo chmod 600 /opt/ojoia/config/ojoia.env
```
Actualizar también `/home/sam/.ojoia_megapanel_token` con el `MEGAPANEL_TOKEN`.

### 3) Copiar el service account key
```bash
# El service account con permisos sobre ojoia-67216
cp /home/sam/ai_system/firebase-key.json /opt/ojoia/config/firebase-key.json
chmod 600 /opt/ojoia/config/firebase-key.json
```

### 4) Instalar `firebase_admin` si falta
```bash
/opt/ojoia/venv/bin/pip install firebase-admin
```

### 5) Copiar los archivos de sincronización
```bash
cp /opt/ojoia/code/ai_system/ojoia_sync.py /opt/ojoia/ai_system/ojoia_sync.py
cp /opt/ojoia/code/ai_system/megapanel.py /opt/ojoia/ai_system/megapanel.py   # versión con ojoia_sync
```

### 6) Arrancar el megapanel
```bash
sudo systemctl restart megapanel
# o
systemctl --user restart megapanel
```
En logs debe aparecer:
```
[ojoia_sync] Conectado a Firestore (proyecto ojoia-67216), nodo=<tu_nombre>
```

### 7) Verificar en Firestore
```bash
# Debe existir /nodes/<tu_nombre> con online:true, gpus, services (cada 5s)
```

## Acceso al panel central

- URL: `https://megapanel-ojoia.web.app`
- Login: cuenta de correo (Firebase Auth). Crea una cuenta o usa una existente.
- Si no puedes crear cuenta (rules restrictivas), pedir al administrador que cree
  una en Firebase Console → Authentication.

## Notas importantes

- Los nodos usan el **SDK admin** (`firebase_admin`) que **bypasa las reglas** de
  Firestore → el push y poll funcionan siempre.
- La **SPA web** usa reglas de seguridad: requiere usuario autenticado para
  leer `/nodes` y escribir `/control`.
- Cloud Functions NO se usan (la API está deshabilitada y el service account no
  tiene permisos para habilitarla). El modelo es 100% push/poll contra Firestore.
- El megapanel web se sirve desde `megapanel-ojoia.web.app` (site separado),
  **sin tocar** el dominio principal `ojoia.com.do`.

## Control de servicios desde el panel

Cuando pulsas ▶ ■ ↻ en la SPA:
1. La SPA escribe `{service_id, action}` en `/control/{node_id}/cmds/{cmd}`
2. El nodo destino lee el comando (polling ≤5s)
3. El nodo ejecuta el control y escribe `{status:done, result}` en el mismo doc
4. La SPA muestra el resultado si lo consulta

## Diagnóstico

| Síntoma | Revisar |
|---------|---------|
| Nodo aparece offline | `[ojoia_sync]` en logs del megapanel; key de firebase; NODE_ID |
| No escribe estado | `service_account` sin acceso a ojoia-67216 |
| Comando no se ejecuta | `poll_control` corre? ver logs; service_id tiene `.service` |
| Panel no carga datos | Auth de la cuenta; reglas de Firestore |