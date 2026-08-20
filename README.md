# OjoIA — Sistema de Vigilancia con IA

> Tu guardia inteligente silencioso.

## Descripción

OjoIA es un sistema de vigilancia con inteligencia artificial que monitorea cámaras,
detecta anomalías y provee un asistente conversacional (Eva) para gestionar la
seguridad del negocio.

## Estructura del proyecto

```
/opt/ojoia/
├── code/                  # ← Este repositorio (código fuente)
│   ├── frontend/          # Interfaz web (PWA)
│   │   ├── index.html     # Página principal
│   │   ├── app-v12.js     # Lógica de la aplicación (header, tabs, viewer)
│   │   ├── eva-chat-v7.js # Chat con Eva (motor de estados, sync, scroll)
│   │   ├── app.css        # Estilos
│   │   ├── manifest.json  # PWA manifest
│   │   ├── sw.js         # Service worker
│   │   └── server.py     # Servidor estático local
│   ├── api_eva.py        # API backend de Eva (/config/chat, /api/chat/eva/*)
│   ├── orchestrator.py   # Orquestador principal del sistema
│   ├── eva_v2.py         # Motor de IA de Eva
│   ├── ai_system/        # Subsistemas de IA (ComfyUI, YOLO, etc.)
│   ├── scripts/          # Scripts de arranque y mantenimiento
│   ├── docs/             # Documentación
│   └── .gitignore        # Ignora venv, modelos, secrets, storage
├── config/               # Configuración runtime (NO versionada)
│   ├── firebase-key.json # Secretos Firebase (ignorado por .gitignore)
│   └── ojoia.env         # Variables de entorno (ignorado)
├── data/                 # Datos runtime (NO versionada)
├── logs/                 # Logs (NO versionados)
├── scripts/              # Scripts del sistema (boot, shutdown, watchdog)
└── venv/                 # Entorno Python (NO versionado)
```

## Deploy — Cómo publicar el frontend

### Destino
- **Firebase Hosting** — Proyecto `ojoia-67216`
- **Dominio**: `https://ojoia.com.do`
- **Cuenta de servicio**: `firebase-adminsdk-fbsvc@ojoia-67216.iam.gserviceaccount.com`
- **Llave**: `/opt/ojoia/config/firebase-key.json` (NO commiteada)

### Script de deploy
`/tmp/deploy_fix.py` — Script Python que:
1. Genera JWT con la llave de servicio Firebase
2. Intercambia por OAuth2 access token
3. Crea una nueva versión en Firebase Hosting
4. Sube los archivos del frontend
5. Publica la versión

### Comando
```bash
python3 /tmp/deploy_fix.py
```

### Archivos que sube
El script camina `/opt/ojoia/code/frontend/` y sube todos los archivos (excepto
directorios ignorados: `.git`, `node_modules`, `__pycache__`, etc.).

### Configuración de hosting (firebase.json embebida en el script)
```json
{
  "headers": [
    {"headers": {"Cache-Control": "no-cache, no-store, must-revalidate"}, "glob": "**/*.@(js|css|html)"},
    {"headers": {"Cache-Control": "max-age=86400"}, "glob": "**/*.@(png|jpg|jpeg|gif|svg|ico)"}
  ],
  "rewrites": [
    {"glob": "/api/**", "path": "https://api.ojoia.com.do/"},
    {"glob": "/admin/**", "path": "/admin2/index.html"},
    {"glob": "/admin2/**", "path": "/admin2/index.html"},
    {"glob": "/**", "path": "/index.html"}
  ]
}
```

### Cache-busting
Los archivos JS/CSS/HTML llevan un `?v=YYYYMMDDx` en el `index.html` para evitar
cache del navegador. Cuando se modifica `eva-chat-v7.js` o `app-v12.js`, hay que:
1. Modificar el archivo
2. Actualizar el `?v=` en `index.html`
3. Ejecutar `python3 /tmp/deploy_fix.py`

### Dominios y redirecciones
- `ojoia.com.do` → Frontend (Firebase Hosting)
- `api.ojoia.com.do` → API backend (Cloudflare tunnel → 127.0.0.1:8005)
- `ui.ojoia.com.do` → UI server (Cloudflare tunnel → 127.0.0.1:8080)
- `chatrd.ojoia.com.do` → Chat RD (Cloudflare tunnel → 127.0.0.1:8010)
- `admin.ojoia.com.do` → Admin (Cloudflare tunnel → 127.0.0.1:8030)
- `server-admin.ojoia.com.do` → Server admin (Cloudflare tunnel → 127.0.0.1:9001)
- `project.ojoia.com.do` → Proyecto (Cloudflare tunnel → 127.0.0.1:8012)

Configuración del túnel: `/etc/cloudflared/config.yml`

## Historial de cambios recientes (frontend)

### 2026-08-19 — Correcciones v14/v15

**eva-chat-v7.js:**
- **Scroll restore**: forzar `scrollToBottom(true)` tras cada `render()` para que el
  chat entre al fondo al abrir la página (el `innerHTML` resetea el scroll a 0).
- **Motor de estados de cámara**: eliminado el early-return que impedía que el mensaje
  de "instalar cámara" llegara al API. Ahora el backend dirige el wizard
  (WIZARD_QR, WIZARD_ZONES_DRAW).
- **Filtro de historial eliminado**: `_isSetupArtifact` comentado (borraba msgs válidos
  con palabras como "configuración").
- **Typo corregido**: `instarlar` → `instalar` en `_isInstallCameraIntent`.
- **Init idempotente**: `_initializedFor` evita duplicar listeners al cambiar de tab.
- **Cross-tab sync**: merge por firma `role|content|timestamp` para no duplicar msgs.
- **Polling remoto**: cada 10s, con dedup y `visibilitychange`.
- **apiFetch global**: agrega `mode: 'cors'` y headers por defecto (fix 401).

**app-v12.js:**
- **apiFetch fix**: `return fetch(url, { mode: 'cors', ...opts, headers })` — soluciona
  errores 401 al hacer fetch a la API.

**index.html:**
- Versión de cache-busting actualizada a `?v=20260819g`.

## Configuración de la máquina

- **SO**: Linux
- **Python**: 3.14 (venv en `/opt/ojoia/venv`)
- **Cloudflare tunnel**: `/etc/cloudflared/config.yml`
- **Puertos locales**:
  - 8005 — API Eva
  - 8010 — Chat RD
  - 8030 — Admin
  - 8080 — UI server
  - 9001 — Server admin
  - 8012 — Proyecto

## Token de GitHub

El token de GitHub está en:
`/home/sam/planes de accion y documentos/tokens/github_token_samuel0117-coder.txt`

Cuenta: `samuel0117-coder`. Repo: `ojoia`.

## No commitear

- `firebase-key.json` (secrets)
- `.env`, `*.env.local`
- `venv/`, modelos IA (`*.engine`, `*.pt`, `*.safetensors`)
- `storage/` (PII de usuarios)
- `*.log`
- Imágenes y vídeos (`*.jpg`, `*.png`, `*.mp4`, etc.) — excepto `frontend/img/` y `frontend/assets/`

## Proximos pasos

1. Verificar que el motor de estados de cámara funciona correctamente tras el fix.
2. Revisar endpoint `/config/chat` del backend (`api_eva.py`) para asegurar que
   responde con `next_phase` correcto.
3. Considerar mover `/tmp/deploy_fix.py` a `/opt/ojoia/scripts/` para que no se pierda.