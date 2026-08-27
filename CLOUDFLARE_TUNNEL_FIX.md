# Cloudflare tunnel — diagnóstico y recovery

## Estado al 2026-08-26

- Tunnel activo en este server: `0a51c161-a577-45d6-a95e-36e659204bf5` (PID 109180)
- Credenciales: `/etc/cloudflared/0a51c161-a577-45d6-a95e-36e659204bf5.json`
- Config: `/etc/cloudflared/config.yml`
- DNS: `api.ojoia.com.do` está apuntando a IPs de Cloudflare directamente, **NO** a un CNAME de tunnel

## Problema detectado

El config original (backup `config.yml.bak.20260821`) apuntaba a:
- tunnel `35385fbc-...` con `api.ojoia.com.do → 127.0.0.1:8005` (api_eva.py)

En algún momento (2026-08-26 14:54) el config fue modificado a:
- tunnel `0a51c161-...` con `api.ojoia.com.do → 127.0.0.1:8200` (service_bus.py — INCORRECTO)
- Archivo `35385fbc-...json` ELIMINADO del disco

## Fix aplicado del lado servidor

1. `config.yml` editado: `api.ojoia.com.do → 127.0.0.1:8005` (api_eva.py, correcto)
2. cloudflared relanzado con el config corregido (conectado al edge, 4 ubicaciones)

## Fix PENDIENTE del lado Cloudflare (requiere acceso al dashboard)

El DNS de `api.ojoia.com.do` en el dashboard de Cloudflare está apuntando a IPs
de Cloudflare directamente (no a un CNAME de tunnel). Eso significa que aunque el
tunnel esté vivo, las peticiones a `api.ojoia.com.do` nunca llegan al tunnel.

**Acción requerida en dash.cloudflare.com → ojoia.com.do → DNS → Records:**

1. Eliminar los records A/AAAA actuales de `api.ojoia.com.do`
2. Crear un record CNAME:
   - Name: `api`
   - Target: `0a51c161-a577-45d6-a95e-36e659204bf5.cfargotunnel.com`
   - Proxy: **ON** (naranja)

O alternativamente, en **Zero Trust → Tunnels → 0a51c161 → Public Hostname**,
añadir una ruta para `api.ojoia.com.do` si no está.

## Credenciales faltantes

El archivo `35385fbc-a181-4f71-b49f-f36c2f7e0b55.json` (tunnel original) ya
no existe en disco. Si quieres revivir el tunnel original, necesitas:
- Recrear el tunnel en el dashboard de Cloudflare
- O restaurar las credenciales desde un backup
