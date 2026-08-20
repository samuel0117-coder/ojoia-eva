# 📋 Plan de Acción — Estado Actual vs. Falantes

> **Fecha:** 2026-08-19
> **Fuente:** Análisis del plano maestro de implementación vs. código real

---

## ✅ HECHOS (15 de 26 items)

### Track A — Seguridad (5 de 6)
| Item | Estado | Evidencia |
|------|--------|-----------|
| A1. AUTH_ENFORCE=True | ✅ | `api_eva.py:185` |
| A2. Megapanel con auth | ✅ | `megapanel.py:39-104` — Bearer token obligatorio |
| A3. CORS | ✅ | Lista explícita, `allow_credentials=False` |
| A4. Sanitizar `/admin/stats` | ✅ | `iterdir()` puro, sin `sh -c` |
| A5. `secrets.compare_digest` | ✅ | `hmac.compare_digest` en tokens |
| A6. Permisos `admin_config.json` 600 | ✅ | Cambiado de 644 → 600 |

### Track B — Estabilidad (5 de 7)
| Item | Estado | Evidencia |
|------|--------|-----------|
| B1. Log activo | ✅ | `RotatingFileHandler` 10MB x 5 |
| B2. `journalctl --vacuum-size=500M` | ✅ | Ejecutado (0B liberados, ya vacío) |
| B3. `du -h --max-depth=2 /` | ✅ | Ejecutado — verificación completa |
| B4. Logrotate instalado | ✅ | `/etc/logrotate.d/ojoia` — probado con `logrotate -vf` |
| B5. Retención de eventos | ✅ | Config existe (`api_eva.py:6030-6119`) — falta activar script |
| B6. `KeyError: 'msgs'` | ✅ | `setdefault` en `api_eva.py:893` |
| B7. systemd `Restart=always` | ✅ | `api-eva.service` — verificado con `systemctl show` |

### Track C — Fase 0 (Pre-requisitos)
| Item | Estado | Evidencia |
|------|--------|-----------|
| C0.1. `camera_builder.py` único | ✅ | `camera_config_builder.py` v12 está muerto (cero imports) |
| C0.2. `zones` en `normalize_camera_vigilance_config` | ✅ | `camera_builder.py:199` |
| C0.3. `save_camera_config` escribe `zones` | ✅ | `camera_builder.py:254-302` |

### Track C — Fase 1 (Backend)
| Item | Estado | Evidencia |
|------|--------|-----------|
| C1.1. CRUD de zonas | ✅ | `GET/POST/DELETE /api/cameras/{id}/zones` |
| C1.2. `parent_zone_id` | ✅ | `camera_zones.py:55` |

### Track D — Escalado (4 de 5)
| Item | Estado | Evidencia |
|------|--------|-----------|
| D1. Pool de workers | ✅ | `WORKER_COUNT = 4` |
| D3. Lock por cámara | ✅ | `CAMERA_LOCKS` por `(user_id, camera_id)` |
| D4. `AsyncClient` compartido | ✅ | `_shared_client` lazy-init |
| D5. PIL grid en `ThreadPoolExecutor` | ✅ | `asyncio.to_thread(create_grid_image, ...)` |

### Túnel Cloudflare
| Item | Estado | Evidencia |
|------|--------|-----------|
| T2. Backoff exponencial en polling | ✅ | `eva-chat-v7.js:462-481` |

---

## ❌ FALTAN (11 de 26 items)

### Track B — Estabilidad
| Item | Estado | Notas |
|------|--------|-------|
| B5. Activar script de retención | ❌ | Config existe pero `cleanup_frames.py` no se ejecuta. Verificar cron. |

### Track C — Fase 1 (Backend)
| Item | Estado | Notas |
|------|--------|-------|
| C1.3. Endpoint sugerir zonas con Qwen | ❌ | Nuevo endpoint `/api/cameras/{id}/suggest-zones` |

### Track C — Fase 2 (Frontend)
| Item | Estado | Notas |
|------|--------|-------|
| C2.1. Visor con frame vivo + canvas | ✅ | Drawer existente en `app-v12.js:3151-3534` |
| C2.2. Mostrar zonas sugeridas por Qwen | ❌ | Requiere C1.3. Proceso automático, no botón. |
| C2.3. Selector de tipo de zona | ✅ | Drawer existente — 15 tipos |
| C2.4. Soporte sub-zonas (1 nivel) | ❌ | `parent_zone_id` existe en backend, falta frontend |
| C2.5. Contador de progreso | ❌ | Añadir al drawer existente |

### Track C — Fase 3 (Integración con Eva)
| Item | Estado | Notas |
|------|--------|-------|
| C3.1. Fase ZONES en state machine | ❌ | Añadir `SetupPhase.ZONES` entre ANALYZE y CONTEXT |
| C3.2. CONTEXT zone-aware | ❌ | Pregunta por zona, no global |
| C3.3. Prueba de alertas con counter | ❌ | Sistema de prueba de reglas con notificación real |
| C3.4. Feedback de reglas | ❌ | Consolidación cada 3-4 correcciones |

### Track C — Fase 4 (Mejoras)
| Item | Estado | Notas |
|------|--------|-------|
| C4.1. Nivel 3 de zonas | ❌ | Posición dentro de sub-zona |
| C4.2. Correlación narrativa | ❌ | Entre cámaras del mismo negocio |

### Track D — Escalado
| Item | Estado | Notas |
|------|--------|-------|
| D2. Cola por `(user_id, camera_id)` | ❌ | Reemplazar `FRAME_QUEUE` global |

### Túnel Cloudflare
| Item | Estado | Notas |
|------|--------|-------|
| T1. `--edge-ip-version 4` | ❌ | Añadir a `/etc/cloudflared/config.yml` |

---

## 📊 Resumen

```
✅ HECHOS:  15 de 26
❌ FALTAN:  11 de 26
```

### Por prioridad:

**🟢 BAJO RIESGO (puede hacerse ahora):**
- B5 — Activar cleanup_frames.py + cron
- D2 — Cola por cámara
- T1 — Cloudflare edge-ip-version 4

**🟡 MEDIO (depende de otros):**
- C1.3 — Endpoint sugerir zonas con Qwen
- C2.2 — Mostrar zonas sugeridas (proceso automático)
- C2.4 — Sub-zonas en frontend
- C2.5 — Contador de progreso
- C3.1 — Fase ZONES en state machine
- C3.2 — CONTEXT zone-aware

**🔴 ALTO (más complejo):**
- C3.3 — Sistema de prueba de reglas con notificación real (WOW #3)
- C3.4 — Feedback de reglas
- C4.1 — Nivel 3 de zonas
- C4.2 — Correlación narrativa

---

## 🎯 Próximos pasos

Ver `PLAN_PRINCIPAL_INSTALACION_CAMARA.md` para el plan de ejecución de los items críticos (WOW #1, #2, #3).