#!/usr/bin/env bash
# start-models.sh — Arranque ordenado de modelos GPU para producción
#
# Distribución de VRAM (24GB por GPU, 2 GPUs):
#   GPU 0 → qwen-7b (sglang)  ~10GB
#          → yolo-pose         ~1GB
#          → whisper-turbo     ~1GB
#   GPU 1 → qwen-3.8 27B (kvarn)  ~23GB
#          → qwen-9b (disponible, NO auto — perfil manual)
#          → qwen-35b (quitado del arranque, frio en disco)
#
# ORDEN OBLIGATORIO: qwen-7b PRIMERO en GPU0, después yolo, después whisper.
# Esto reserva VRAM en GPU0 antes de que los demás compitan por ella.
# El 3.8 27B corre en GPU1 (manejado por systemd ojoia-bus, no por este script).
#
# Estrategia:
#   1. qwen-7b (GPU0)  - reserva 10GB en GPU0
#   2. yolo-pose (GPU0) - ligero
#   3. whisper-turbo (GPU0) - ligero
#   (qwen-9b y qwen-35b NO se arrancan automaticamente)
set -euo pipefail

COMPOSE_DIR="/srv/ai"
COMPOSE_FILE="$COMPOSE_DIR/docker-compose.yml"

echo "=== Arranque ordenado de modelos (producción) ==="

# 1. qwen-7b PRIMERO (GPU0) - reserva VRAM
echo "[1/3] Iniciando qwen-7b (GPU0)..."
docker compose -f "$COMPOSE_FILE" up -d qwen-7b 2>&1 | tail -3
echo "  Esperando healthy..."
for i in $(seq 1 40); do
  sleep 3
  ST=$(docker inspect qwen-7b --format '{{.State.Health.Status}}' 2>/dev/null)
  if [ "$ST" = "healthy" ]; then echo "  qwen-7b healthy ✅"; break; fi
  if [ "$ST" = "unhealthy" ]; then
    echo "  qwen-7b UNHEALTHY — logs:"; docker logs qwen-7b --tail 8 2>&1 | tail -5
    exit 1
  fi
done

# 2. yolo-pose (GPU0) - ligero
echo "[2/3] Iniciando yolo-pose (GPU0)..."
docker compose -f "$COMPOSE_FILE" up -d yolo-pose 2>&1 | tail -3
sleep 5

# 3. whisper-turbo (GPU0) - ligero
echo "[3/3] Iniciando whisper-turbo (GPU0)..."
docker compose -f "$COMPOSE_FILE" up -d whisper-turbo 2>&1 | tail -3
sleep 5

# Verificación final
echo "=== Verificación final ==="
for endpoint in "qwen-7b:8004:/health" "whisper-turbo:8008:/health" "yolo-pose:8002:/health"; do
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

# Verificar el 3.8 (manejado por docker run, no compose)
echo "--- GPU 1 (qwen-3.8 27B) ---"
code=$(curl -sf --max-time 5 -o /dev/null -w "%{http_code}" "http://127.0.0.1:18020/v1/models" 2>/dev/null)
if [ -n "$code" ] && [ "$code" != "000" ]; then
  echo "  ✅ qwen38-syv (port 18020) HTTP $code"
else
  echo "  ❌ qwen38-syv (port 18020) FAIL"
fi

echo ""
echo "NOTA: qwen-9b y qwen-35b NO se arrancan automaticamente."
echo "  - qwen-9b: docker compose -f $COMPOSE_FILE --profile manual up -d qwen-9b"
echo "  - qwen-35b: frio en disco (imagen y modelo intactos, no en compose)"
echo "=== Modelos listos para producción ==="
