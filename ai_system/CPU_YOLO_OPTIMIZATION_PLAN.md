# Plan CPU YOLO + Viewer fluido

## Objetivo temporal

Mantener YOLO en CPU durante la etapa de pruebas del producto, optimizando velocidad y experiencia de usuario sin introducir cambios grandes como WebSocket, MJPEG o GPU.

## Cambios backend

1. Optimizar `yolo_server.py` para CPU:
   - `torch.set_num_threads(4)`
   - `torch.set_num_interop_threads(1)`
   - `yolov8s`
   - `imgsz=416`
   - `conf=0.25`
   - semaphore para evitar inferencias concurrentes solapadas.

2. Optimizar ingesta en `api_eva.py`:
   - guardar `latest_raw.jpg` de cada frame recibido antes de YOLO.
   - exponer `/frames/latest-raw.jpg` para viewer rápido.
   - guardar últimas detecciones de YOLO por cámara en memoria.
   - exponer detecciones en `/frames/latest`.
   - limitar análisis YOLO por cámara a cada 3 segundos por defecto.

## Cambios frontend

1. Viewer de inicio:
   - actualizar imagen directamente desde `/frames/latest-raw.jpg`.
   - no esperar JSON para mostrar imagen.
   - polling JPEG cada 500ms.
   - mantener JSON solo para metadatos y boxes.

2. Boxes:
   - dibujar boxes en frontend con canvas.
   - usar últimas detecciones del backend.
   - TTL visual de 3-5 segundos.
   - fade-out y etiquetas con clase/confianza.

## Configuración recomendada ESP32

- `framesize`: VGA
- `quality`: 8
- `interval_ms`: 250-300ms

## Capacidad esperada CPU

Con i7-4820K, 4 threads, `imgsz=416`, `conf=0.25`, YOLO cada 3s:

- 6-8 cámaras cómodas.
- 8-12 cámaras posibles si se baja a `imgsz=320`.

## Criterio de aceptación

- Viewer muestra frames más fluidos.
- YOLO no analiza cada frame.
- Boxes se ven en frontend sin recomprimir imágenes en backend.
- La configuración queda guardada en Git.
