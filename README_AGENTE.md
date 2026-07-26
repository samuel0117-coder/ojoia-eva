# ✅ REPOSITORIO CANÓNICO — FRONTEND OjoIA

> **AVISO PARA AGENTES DE CÓDIGO Y HUMANOS**
> Este es el repositorio **oficial y único** del frontend de OjoIA.

## Estado

- Frontend (PWA) desplegado en Firebase Hosting como `ojoia.com.do`.
- App principal: `frontend/app-v12.js` (con `frontend/eva-chat-v5.js` para chat de Eva).
- Service Worker: `frontend/sw.js` (cache `ojoia-v7`).
- Deploy: `python3 frontend/deploy.py` → Firebase Hosting (`ojoia-67216`).
- Backend (NO en este repo): `/opt/ojoia/code/` (API en `api.ojoia.com.do` vía Cloudflare tunnel).

## Remote

- GitHub: `samuel0117-coder/ojoia-eva` (remoto `origin`, configurado sin credenciales en la URL).
- Auth push vía `gh` CLI (token en `~/.config/gh/hosts.yml`, NUNCA en `.git/config`).

## Reglas

1. **No commitear secretos**: servicio Firebase, service account, API keys. `.gitignore` los bloquea.
2. **No commitear datos de cliente**: `storage/` está `.gitignore`-do.
3. **No commitear logs**: `*.log` está `.gitignore`-do.
4. Antes de cualquier push, ejecutar `git status` y `git diff --cached` para confirmar cero secretos.

## Otros repositorios locales de OjoIA (NO canónicos, referencia únicamente)

- `/home/sam/ojoia-new-repo/` — mirror histórico deprecated con token revocado en config. **No tocar.**
- `/opt/ojoia/code/` — backend (repo git propio, no mezclar con este).

---
Última actualización: 2026-07-26
