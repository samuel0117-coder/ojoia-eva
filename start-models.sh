#!/usr/bin/env bash
# start-models.sh — Arranque ordenado de modelos GPU para producción
#
# Distribución de VRAM (24GB por GPU, 2 GPUs):
#   GPU 0 → qwen-9b (vLLM)            ~20GB
#   GPU 1 → qwen-7b (sglang)  ~10GB
#          → yolo-pose         ~1GB
#          → whisper-turbo     ~1GB
#          → qwen-35b (llama)  ~22GB   (necesita casi toda la GPU 1)
#
# ORDEN OBLIGATORIO: qwen-7b PRIMERO en GPU1, después qwen-35b.
# Si qwen-35b arranca antes que qwen-7b, no le queda VRAM.
# Si arrancamos yolo/whisper ANTES de qwen-35b, no hay problema porque
# son ligeros. PERO si arrancamos qwen-9b en GPU0 después de qwen-35b,
# este último podría haber consumido VRAM de GPU0 por defecto.
#
# Estrategia: arrancar en este orden para máxima estabilidad:
#   1. qwen-7b (GPU1)  - reserva 10GB en GPU1
#   2. qwen-35b (GPU1) - ocupa los 14GB restantes
#   3. yolo (GPU1)     - se queda en CPU o usa poca GPU
#   4. whisper (GPU1)   - idem
#   5. qwen-9b (GPU0)  - independiente, GPU0
set -euo pipefail

echo "=== Arranque ordenado de modelos (producción) ==="

# 1. qwen-7b PRIMERO (GPU1) - reserva VRAM en GPU1
echo "[1/5] Iniciando qwen-7b (GPU1)..."
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

# 2. qwen-35b INMEDIATAMENTE DESPUÉS de qwen-7b (misma GPU1)
#    Necesita ~8.6GB; quedan ~14GB libres en GPU1.
echo "[2/5] Iniciando qwen-35b (GPU1)..."
docker start qwen-35b-a3b 2>/dev/null || echo "  (ya corriendo)"
echo "  Esperando healthy..."
for i in $(seq 1 30); do
  sleep 3
  ST=$(docker inspect qwen-35b-a3b --format '{{.State.Health.Status}}' 2>/dev/null)
  if [ "$ST" = "healthy" ]; then echo "  qwen-35b healthy ✅"; break; fi
  if [ "$ST" = "unhealthy" ]; then
    echo "  qwen-35b UNHEALTHY — logs:"; docker logs qwen-35b-a3b --tail 8 2>&1 | tail -5
    echo "  ⚠ Si falla por OOM, parar ai-qwen-9b-1 primero (libera GPU0 como fallback)"
    exit 1
  fi
done

# 3. yolo-pose (GPU1) - ligero, ~1GB VRAM
echo "[3/5] Iniciando yolo-pose (GPU1)..."
docker start yolo-pose 2>/dev/null || echo "  (ya corriendo)"
sleep 5

# 4. whisper-turbo (GPU1) - ligero, ~1GB VRAM
echo "[4/5] Iniciando whisper-turbo (GPU1)..."
docker start whisper-turbo 2>/dev/null || echo "  (ya corriendo)"
sleep 5

# 5. qwen-9b (GPU0) - usa toda la GPU0 (independiente)
echo "[5/5] Iniciando qwen-9b (GPU0)..."
docker start ai-qwen-9b-1 2>/dev/null || echo "  (ya corriendo)"
sleep 5

# Verificación final
echo "=== Verificación final ==="
for endpoint in "qwen-7b:8004:/health" "qwen-35b-a3b:8019:/health" "ai-qwen-9b-1:8018:/v1/models" "whisper-turbo:8008:/health" "yolo-pose:8002:/health"; do
  c=$(echo $endpoint | cut -d: -f1)
  p=$(echo $endpoint | cut -d: -f2)
  h=$(echo $endpoint | cut -d: -f3)
  code=$(curl -sf --max-time 5 -o /dev/null -w "%{http_code}" "http://127.0.0.1:${p}${h}" 2>/dev/null)
  if [ -n "$code" ] && [ "$code" != "000" ]; then
    echo "  ✅ $c (port $p) HTTP $code"
  else
    echo "  ❌ $c (port $p) FAIL"
  fi
done
echo "=== Modelos listos para producción ==="
