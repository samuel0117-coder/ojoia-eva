#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════
# start-models.sh — ÚNICO script de carga de modelos (producción)
# Ejecutado por ojoia-models.service al encender el servidor,
# justo después de: network-online → docker → tunnel (cloudflared).
#
# ORDEN CONFIRMADO:
#   FASE 0 (paralelo, background — no bloquea el arranque):
#     1. tunnel (cloudflared)          — ya activo (prerequisito)
#     2. qwen-7b (GPU 0, ~10GB)       — PRIMERO en GPU 0, reserva VRAM
#     3. yolo-pose (~1GB)             ┐ paralelos entre sí
#     4. whisper-turbo (~1.3GB)       ┘ (ligeros, tras 7b)
#     5. qwen38-syv (GPU 1, ~23GB)   — independiente, background
#
#   FASE 1 (GPU 0, secuencial — SOLO cuando 7b está 100% arriba):
#     6. delay 8s (presupuesto VRAM estabilizado)
#     7. qwen3vl8b (~7GB)             — ÚLTIMO, ocupa la VRAM restante
#
# El script SALDRÁ en ~10s: las esperas largas van en subshells
# despegados (detached), así el boot del SO nunca se bloquea.
# ═══════════════════════════════════════════════════════════════
set -uo pipefail

CONT_7B="qwen-7b"
CONT_VL8B="qwen3vl8b"
CONT_YOLO="yolo-pose"
CONT_WHISPER="whisper-turbo"
CONT_38="qwen38-syv"
LOG_TAG="[start-models]"

log(){ echo "$LOG_TAG $(date +%H:%M:%S) $*"; }
container_running(){ [ "$(docker inspect "$1" --format '{{.State.Status}}' 2>/dev/null)" = "running" ]; }
http_ok(){ [ "$(curl -s --max-time 2 -o /dev/null -w "%{http_code}" "$1" 2>/dev/null)" = "200" ]; }

# Limpieza de flags stale de pausa (evita huérfanos del health-monitor)
rm -f /tmp/.paused_qwen3vl8b /tmp/.paused_qwen-7b 2>/dev/null

log "=== ARRANQUE UNIFICADO DE MODELOS ==="

# ── PREREQUISITOS: docker + tunnel activos ──────────────────────
if ! systemctl is-active --quiet tunnel.service 2>/dev/null; then
  log "[NET] tunnel inactivo — iniciando..."
  systemctl start tunnel.service 2>/dev/null || true
fi
log "[NET] tunnel: $(systemctl is-active tunnel.service 2>/dev/null || echo '?') — OK"

# ── FASE 0.5: qwen38-syv (GPU 1) EN BACKGROUND ──────────────────
# Independiente: 23GB en GPU 1, no compite con nadie. Tarda 4-5 min
# en compilar CUDA graphs. Lo lanzamos ya y NO esperamos.
if container_running "$CONT_38"; then
  log "[GPU1] qwen38-syv ya corriendo"
else
  log "[GPU1] Lanzando qwen38-syv (background, no bloquea)..."
  docker start "$CONT_38" >/dev/null 2>&1
fi
# Monitor despegado: loguea cuando el 38 responde, sin bloquear nada
( for i in $(seq 1 300); do
    http_ok http://127.0.0.1:18020/v1/models && { log "[GPU1] ✓ qwen38-syv LISTO (${i}s)"; exit 0; }
    sleep 1
  done
  log "[GPU1] ⚠ qwen38-syv no respondió en 300s — revisa docker logs" ) \
  >/dev/null 2>&1 & disown

# ── FASE 0: GPU 0 — CASCADA COMPLETA EN SUBSHELL DESPEGADO ──────
# El subshell garantiza el orden tras salir el script principal:
#   7b healthy → yolo ∥ whisper → delay 8s → vl8b
( 
  # 1/4 qwen-7b PRIMERO (reserva ~10GB VRAM en GPU 0)
  if http_ok http://127.0.0.1:8004/health; then
    log "[GPU0] qwen-7b ya healthy"
  else
    # Si vl8b quedó corriendo de un boot previo y el 7b está abajo,
    # hay carrera de VRAM: pausar vl8b hasta revivir el 7b.
    if container_running "$CONT_VL8B" && ! container_running "$CONT_7B"; then
      log "[GPU0] 7b caído + vl8b arriba — pausando vl8b (liberar VRAM)..."
      docker stop "$CONT_VL8B" >/dev/null 2>&1
    fi
    log "[GPU0] 1/4 Iniciando qwen-7b (reserva VRAM)..."
    docker start "$CONT_7B" >/dev/null 2>&1
    # 8s de grava: sglang calcula su presupuesto contra la VRAM libre
    log "[GPU0] delay 8s (presupuesto VRAM del 7b se estabiliza)..."
    sleep 8
    for i in $(seq 1 90); do
      http_ok http://127.0.0.1:8004/health && { log "[GPU0] ✓ qwen-7b healthy (${i}s)"; break; }
      sleep 1
    done
  fi

  # GATE: el 7b DEBE estar arriba antes de cargar lo demás en GPU 0
  if ! http_ok http://127.0.0.1:8004/health; then
    log "[GPU0] ✗ qwen-7b NO healthy tras 90s — ABORT cascada GPU0 (no cargamos vl8b)"
    exit 1
  fi
  # GATE VRAM: el 7b debe ocupar ≤11GB. Si salió inflado (>12GB, carrera de
  # arranque), el vl8b (7.1GB) no cabrá y morirá con OOM — mejor detectarlo
  # aquí, reiniciar el 7b limpio y volver a verificar.
  for attempt in 1 2; do
    VRAM_7B=$(nvidia-smi --query-compute-apps=pid,used_memory --format=csv,noheader,nounits 2>/dev/null \
              | grep -iE "sglang" | awk -F', ' '{s+=$2} END {print int(s)}')
    log "[GPU0] VRAM del 7b: ${VRAM_7B:-?} MiB (esperado ≤11000, límite 12000)"
    if [ "${VRAM_7B:-0}" -le 12000 ]; then break; fi
    log "[GPU0] ⚠ 7b inflado (${VRAM_7B}MiB) — reinicio limpio (intento $attempt/2)..."
    docker restart "$CONT_7B" >/dev/null 2>&1
    for i in $(seq 1 90); do
      http_ok http://127.0.0.1:8004/health && break
      sleep 1
    done
  done

  # 2/4 y 3/4 yolo + whisper EN PARALELO (ligeros, ~2.3GB juntos)
  ( container_running "$CONT_YOLO"    || { docker start "$CONT_YOLO"    >/dev/null 2>&1; log "[GPU0] 2/4 yolo-pose lanzado"; } ) &
  ( container_running "$CONT_WHISPER" || { docker start "$CONT_WHISPER" >/dev/null 2>&1; log "[GPU0] 3/4 whisper-turbo lanzado"; } ) &
  wait
  # Espera breve a que los ligeros respondan
  for i in $(seq 1 20); do
    y=$(curl -s --max-time 1 -o /dev/null -w "%{http_code}" http://127.0.0.1:8002/health 2>/dev/null)
    w=$(curl -s --max-time 1 -o /dev/null -w "%{http_code}" http://127.0.0.1:8008/health 2>/dev/null)
    [ "$y" = "200" ] && [ "$w" = "200" ] && { log "[GPU0] ✓ yolo + whisper listos (${i}s)"; break; }
    sleep 1
  done

  # 4/4 qwen3vl8b ÚLTIMO — SOLO con el 7b confirmado arriba
  if container_running "$CONT_VL8B" && http_ok http://127.0.0.1:8019/v1/models; then
    log "[GPU0] qwen3vl8b ya corriendo y healthy"
  else
    log "[GPU0] 4/4 Iniciando qwen3vl8b (último, VRAM restante)..."
    docker start "$CONT_VL8B" >/dev/null 2>&1
    for i in $(seq 1 60); do
      http_ok http://127.0.0.1:8019/v1/models && { log "[GPU0] ✓ qwen3vl8b listo (${i}s)"; break; }
      sleep 1
    done
  fi

  log "[GPU0] === Cascada GPU 0 completada ==="
) >/tmp/start-models-gpu0.log 2>&1 & disown
log "[GPU0] Cascada lanzada en background (PID $!) — log: /tmp/start-models-gpu0.log"

# Limpieza final de flags (defensa adicional)
rm -f /tmp/.paused_qwen3vl8b /tmp/.paused_qwen-7b 2>/dev/null

log "=== SCRIPT SALIÓ EN SEGUNDOS — cascadas corren en background ==="
log "Modelos cargando: qwen38 (GPU1) ∥ 7b→yolo∥whisper→vl8b (GPU0)"
