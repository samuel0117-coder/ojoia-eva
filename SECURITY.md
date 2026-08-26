# Security Policy

## Secrets management

**Regla absoluta**: ningún secret se commitea al repo. El `.gitignore` excluye
explícitamente los patrones `*.bak`, `*.backup`, `*.backup2`, `firebase-key.json`,
`.env`, `*.key`, `*.p12`, y modelos grandes (`*.gguf`, `*.safetensors`).

Si necesitas guardar un secret de servicio (HF, OpenAI, AWS, etc.):

1. **Variables de entorno** en `/opt/ojoia/config/ojoia.env` (chmod 600).
2. O **secret manager** dedicado (no archivos planos).
3. O **Firebase Secrets / Google Secret Manager**.

## Firebase config pública

Las credenciales de Firebase en `frontend/app-2026.js` (apiKey, authDomain,
projectId) son públicas por diseño — Firebase espera que el cliente las conozca.
No rotarlas pensando que son secretas; sí rotar las **service account keys**
(servidor) si se exponen.

## Reporte de exposición

Si accidentalmente commiteas un secret:

1. **Revocar inmediatamente** el token en su proveedor (HuggingFace, GitHub, etc.).
2. **Rotar** las credenciales.
3. **Limpiar el historial git** con `git filter-repo` o `BFG` antes de pushear.
4. **Nunca** hacer `git push --force` sin antes coordinar con colaboradores.

## Historial de incidentes

- 2026-08-26: HF_TOKEN (`hf_UZRj...`) expuesto en `ai_system/flux_server.py.bak`
  en el repo público. Token revocado y archivos redactados localmente.
  Pendiente: limpieza de historial git y `force-push` (requiere decisión del owner).
