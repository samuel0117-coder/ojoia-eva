#!/usr/bin/env bash
# start-models.sh — Arranque ordenado de modelos GPU para producción
# ORDEN OBLIGATORIO: qwen-7b (GPU1) ANTES que qwen-35b (GPU1)
# Si qwen-35b arranca primero, no deja VRAM y qwen-7b falla.
set -euo pipefail

echo "=== Arranque ordenado de modelos (producción) ==="

# 1. qwen-7b PRIMERO (GPU1)
echo "[1/6] Iniciando qwen-7b..."
docker start qwen-7b 2>/dev/null || echo "  (ya corriendo)"
echo "  Esperando healthy..."
for i in $(seq 1 30); do
  sleep 3
  ST=$(docker inspect qwen-7b --format '{{.State.Health.Status}}' 2>/dev/null)
  if [ "$ST" = "healthy" ]; then echo "  qwen-7b healthy ✅"; break; fi
  if [ "$ST" = "unhealthy" ]; then
    echo "  qwen-7b UNHEALTHY — logs:"; docker logs qwen-7b --tail 8 2>&1 | tail -5
    exit 1
  fi
done

# 2. yolo-pose (GPU1) — con delay para no saturar GPU1
echo "[2/6] Iniciando yolo-pose..."
docker start yolo-pose 2>/dev/null || echo "  (ya corriendo)"
sleep 5

# 3. whisper-turbo (GPU0) — con delay para estabilidad
echo "[3/6] Iniciando whisper-turbo..."
docker start whisper-turbo 2>/dev/null || echo "  (ya corriendo)"
sleep 5

# 4. qwen-9b (GPU0) — con delay para no saturar GPU0
echo "[4/6] Iniciando qwen-9b..."
docker start ai-qwen-9b-1 2>/dev/null || echo "  (ya corriendo)"
sleep 8

# 5. qwen-35b DESPUÉS de qwen-7b (GPU1) — VRAM
echo "[5/6] Iniciando qwen-35b..."
docker start qwen-35b-a3b 2>/dev/null || echo "  (ya corriendo)"
echo "  Esperando healthy..."
for i in $(seq 1 20); do
  sleep 3
  ST=$(docker inspect qwen-35b-a3b --format '{{.State.Health.Status}}' 2>/dev/null)
  if [ "$ST" = "healthy" ]; then echo "  qwen-35b healthy ✅"; break; fi
  if [ "$ST" = "unhealthy" ]; then
    echo "  qwen-35b UNHEALTHY — logs:"; docker logs qwen-35b-a3b --tail 8 2>&1 | tail -5
    exit 1
  fi
done

# 6. Verificación final
echo "[6/6] Verificación final..."
echo -n "  qwen-7b: " && curl -sf --max-time 5 http://127.0.0.1:8004/health >/dev/null && echo "OK" || echo "FAIL"
echo -n "  qwen-9b: " && curl -sf --max-time 5 http://127.0.0.1:8018/v1/models >/dev/null && echo "OK" || echo "FAIL"
echo -n "  whisper: " && curl -sf --max-time 5 http://127.0.0.1:8008/health >/dev/null && echo "OK" || echo "FAIL"
echo -n "  yolo: " && curl -sf --max-time 5 http://127.0.0.1:8002/health >/dev/null && echo "OK" || echo "FAIL"
echo -n "  qwen-35b: " && curl -sf --max-time 5 http://127.0.0.1:8019/health >/dev/null && echo "OK" || echo "FAIL"
echo "=== Modelos listos para producción ==="
