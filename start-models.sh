#!/usr/bin/env bash
# start-models.sh — Arranque ordenado de modelos GPU para producción.
# Ejecutado por ojoia-models.service (oneshot) tras docker.service en el boot,
# o manualmente tras cambios de VRAM.
#
# Layout producción (2x RTX 3090 24GB):
#   GPU 0: qwen-7b (sglang, ~10GB) → yolo-pose (~1GB) → whisper-turbo (~1.3GB)
#          → qwen3vl8b (~7.1GB)  [ÚLTIMO: el 7b reserva VRAM primero]
#   GPU 1: qwen38-syv (27B kvarn, 23.4GB) [docker start directo]
#   Manual: qwen-9b (GPU 1, profile manual). Frío: qwen-35b-a3b.
#
# ORDEN OBLIGATORIO GPU 0: el 7b calcula su presupuesto de VRAM contra la
# memoria LIBRE al arrancar (mem-fraction-static). Si el vl8b (7GB) ya está
# cargado, el 7b crashea en loop ("Loaded weights leave no GPU memory for the
# KV cache"). Por eso: 7b PRIMERO, vl8b AL FINAL.
# NOTA: docker restart-policy=unless-stopped puede levantar todo en paralelo
# tras un reboot; este script corrige el orden si el 7b quedó en crash-loop:
# detecta el ValueError, detiene el vl8b, revive el 7b y re-arranca el vl8b.
set -uo pipefail

CONT_7B="qwen-7b"
CONT_VL8B="qwen3vl8b"
CONT_YOLO="yolo-pose"
CONT_WHISPER="whisper-turbo"
CONT_38="qwen38-syv"

wait_http() { # <url> <name> <max_s>
  local url=$1 name=$2 max=${3:-60} code=""
  for i in $(seq 1 "$max"); do
    sleep 1
    code=$(curl -s --max-time 2 -o /dev/null -w "%{http_code}" "$url" 2>/dev/null)
    [ "$code" = "200" ] && { echo "  [OK] $name (${i}s)"; return 0; }
  done
  echo "  [FAIL] $name no responde en ${max}s (http=$code)"; return 1
}

container_running() { [ "$(docker inspect "$1" --format '{{.State.Status}}' 2>/dev/null)" = "running" ]; }

ensure_7b_first() {
  # Detecta crash-loop del 7b por carrera de VRAM y lo recupera:
  # 1) si el 7b no está healthy y el vl8b corre → pausar vl8b
  # 2) esperar 7b healthy 3) re-arrancar vl8b
  local code
  code=$(curl -s --max-time 2 -o /dev/null -w "%{http_code}" http://127.0.0.1:8004/health 2>/dev/null)
  if [ "$code" = "200" ]; then echo "  [OK] qwen-7b ya healthy"; return 0; fi

  if container_running "$CONT_VL8B"; then
    echo "  [WARN] 7b no healthy y vl8b corre — pausando vl8b para liberar VRAM..."
    docker stop "$CONT_VL8B" >/dev/null 2>&1
    # evitar que health-monitor resucite el vl8b a mitad de la recuperación
    touch /tmp/.paused_qwen3vl8b 2>/dev/null
    sleep 3
  fi

  if ! container_running "$CONT_7B"; then docker start "$CONT_7B" >/dev/null 2>&1; fi
  echo "  Esperando qwen-7b (GPU0)..."
  wait_http http://127.0.0.1:8004/health "qwen-7b" 90 || return 1
  rm -f /tmp/.paused_qwen3vl8b 2>/dev/null

  if ! container_running "$CONT_VL8B"; then
    echo "  Re-arrancando qwen3vl8b..."
    docker start "$CONT_VL8B" >/dev/null 2>&1
    wait_http http://127.0.0.1:8019/v1/models "qwen3vl8b" 60
  fi
  return 0
}

echo "=== Arranque ordenado de modelos (producción) ==="

# ── GPU 1 primero: el 38 no compite con nadie (23.4GB dedicados) ──
if container_running "$CONT_38"; then
  echo "[GPU1] qwen38-syv ya corriendo"
else
  echo "[GPU1] Iniciando qwen38-syv (27B kvarn)..."
  docker start "$CONT_38" >/dev/null 2>&1
fi

# ── GPU 0: 7b PRIMERO (reserva VRAM) ──
echo "[GPU0] Asegurando qwen-7b primero..."
ensure_7b_first

# ── Servicios ligeros GPU 0 ──
for pair in "$CONT_YOLO:http://127.0.0.1:8002/health" "$CONT_WHISPER:http://127.0.0.1:8008/health"; do
  c="${pair%%:*}"; url="${pair#*:}"
  if container_running "$c"; then echo "[GPU0] $c ya corriendo"
  else
    echo "[GPU0] Iniciando $c..."
    docker start "$c" >/dev/null 2>&1
    wait_http "$url" "$c" 45
  fi
done

# ── VL8B AL FINAL (7GB; debe entrar en la VRAM que dejó el 7b) ──
if container_running "$CONT_VL8B"; then
  echo "[GPU0] qwen3vl8b ya corriendo"
else
  echo "[GPU0] Iniciando qwen3vl8b (después del 7b)..."
  docker start "$CONT_VL8B" >/dev/null 2>&1
  wait_http http://127.0.0.1:8019/v1/models "qwen3vl8b" 60
fi

# ── Verificación final ──
echo "=== Verificación final ==="
for pair in "qwen-7b:8004:/health" "qwen3vl8b:8019:/v1/models" "whisper-turbo:8008:/health" "yolo-pose:8002:/health" "qwen38-syv:18020:/v1/models"; do
  c="${pair%%:*}"; rest="${pair#*:}"; p="${rest%%:*}"; h="${rest#*:}"
  code=$(curl -s --max-time 5 -o /dev/null -w "%{http_code}" "http://127.0.0.1:${p}${h}" 2>/dev/null)
  if [ -n "$code" ] && [ "$code" != "000" ]; then echo "  [OK] $c :$p HTTP $code"
  else echo "  [FAIL] $c :$p"; fi
done

echo "=== Modelos listos para producción ==="
echo "NOTA: qwen-9b (manual): docker compose -f /srv/ai/docker-compose.yml --profile manual up -d qwen-9b"
echo "      qwen-35b-a3b: frío en disco (imagen intacta)"
