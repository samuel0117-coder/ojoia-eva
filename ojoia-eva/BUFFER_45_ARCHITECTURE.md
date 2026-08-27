# Plan de acción: buffer rodante de 45 minutos y clips de evidencia

## Objetivo
Permitir que la cámara siga funcionando como servicio base aunque expire la suscripción, manteniendo un buffer temporal de 45 minutos por cámara y permitiendo guardar clips/evidencia cuando haya eventos importantes o acción manual del usuario.

## Arquitectura
1. Servicio base sin suscripción:
   - cámara en vivo,
   - ingest de frames,
   - buffer rodante de 45 minutos,
   - botón manual “Guardar últimos 45 minutos”.

2. Suscripción activa:
   - Eva,
   - Qwen,
   - detección de eventos importantes,
   - guardado automático de evento con frames/video/metadata.

3. Premium/auditoría:
   - retención extendida,
   - más días de eventos,
   - más clips,
   - buffer de 24 horas si se habilita.

## Reglas del buffer
- Ventana: 45 minutos.
- Frecuencia objetivo: 1 frame cada 3 segundos.
- Límites por cámara:
  - máximo 45 minutos,
  - máximo 1000 frames,
  - máximo 200 MB.
- El buffer es rodante: al superar el límite se borran los frames más viejos.

## Estructura
```text
users/{uid}/cameras/{cam_id}/
  recent_frames/
    manifest.json
    frame_{timestamp}.jpg

  events/
    evt_{timestamp}_{cam_id}/
      evt_{timestamp}_{cam_id}.json
      evt_{timestamp}_{cam_id}.jpg
      evt_{timestamp}_{cam_id}.mp4
      frames/

  clips/
    clip_{timestamp}_{cam_id}/
      clip_{timestamp}_{cam_id}.json
      clip_{timestamp}_{cam_id}.mp4
      frames/
```

## Endpoints propuestos
- `GET /api/events/{event_id}`
- `GET /api/events/{event_id}/video.mp4`
- `GET /api/events/{event_id}/frame/{index}`
- `GET /api/events/{event_id}/frames`
- `GET /api/cameras/{camera_id}/recent-frames`
- `POST /api/cameras/{camera_id}/save-recent-clip`

## Implementación backend
1. Agregar helpers para guardar frame en `recent_frames/`.
2. Agregar cleanup rodante por tiempo, cantidad de frames y tamaño.
3. Agregar endpoint para consultar frames recientes.
4. Agregar endpoint para convertir últimos N minutos en clip permanente.
5. Generar video desde frames con `imageio/ffmpeg` o `cv2`.
6. Reutilizar frames recientes para eventos importantes.

## Implementación frontend
1. En tab Cámaras:
   - mini reproductor del último frame,
   - slider de frames recientes,
   - botón “Guardar últimos 45 minutos”.
2. En Eva/eventos:
   - tarjeta con miniatura,
   - botón “Ver video”,
   - botones “Frame anterior” y “Frame siguiente”.

## Validación
- Ingesta guarda frames en buffer.
- Buffer borra los frames más viejos al superar 45 min/1000 frames/200 MB.
- Endpoint de frames recientes devuelve frames ordenados.
- Guardar clip crea carpeta permanente con JSON, frames y MP4.
- Frontend muestra slider y botón de guardar.
- Backend responde 200 después del deploy.
