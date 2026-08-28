#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════════
# cineia — Script de Inicio Maestro v4.0 (limpio, sin duplicados)
# Arquitectura de cineia:
#   GPU 0+1 → H3 workers (ComfyUI, systemd, venv_f5 NVMe)         :8189, :8190
#   GPU 2   → Flux (docker, compartido con otras instancias)      :8020
#            + RealESRGAN (systemd, venv_f5 NVMe)                 :8021
#   CPU/RAM → studio(8095), movie(8090), post(8014),
#             audioldm2(8013), sadtalker(8022), musicgen(8023),
#             redis(6379), tunnel(cloudflared), megapanel(9001)
#
# Reglas:
#   - Solo servicios con unit file real y corriendo.
#   - Cero duplicados (si existe cineia-X, no se incluye X).
#   - Cero stubs (stable-audio, service-bus, etc.).
#   - Solo este nodo (cineia). El otro nodo (ojoia) tiene su propio script.
# ═══════════════════════════════════════════════════════════════════════════════

set -uo pipefail
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;1;34m'; NC='\033[0m'

LOG=/tmp/cineia_start_$(date +%Y%m%d_%H%M%S).log
echo -e "${BLUE}═══ cineia start v4.0 · $(date) ═══${NC}" | tee -a "$LOG"

check_user() { systemctl --user is-active --quiet "$1" && echo "active" || echo "inactive"; }
check_sys()  { systemctl is-active --quiet "$1" && echo "active" || echo "inactive"; }

start_user() {
  local svc="$1" label="$2" port="${3:-}" wait_max="${4:-30}"
  if systemctl --user is-active --quiet "$svc"; then
    echo -e "  ${GREEN}✓${NC} $label  ya activo"
    return 0
  fi
  systemctl --user reset-failed "$svc" 2>/dev/null
  systemctl --user start "$svc" 2>/dev/null
  if [ -n "$port" ]; then
    for i in $(seq 1 "$wait_max"); do
      sleep 1
      if ss -tln 2>/dev/null | grep -q ":$port "; then
        echo -e "  ${GREEN}✓${NC} $label  (puerto $port, ${i}s)"
        return 0
      fi
    done
    echo -e "  ${YELLOW}⚠${NC} $label  puerto $port no abrió en ${wait_max}s"
  else
    sleep 2
    systemctl --user is-active --quiet "$svc" \
      && echo -e "  ${GREEN}✓${NC} $label" \
      || echo -e "  ${YELLOW}⚠${NC} $label  no activo"
  fi
}

start_sys() {
  local svc="$1" label="$2" port="${3:-}" wait_max="${4:-30}"
  if systemctl is-active --quiet "$svc"; then
    echo -e "  ${GREEN}✓${NC} $label  ya activo"
    return 0
  fi
  systemctl reset-failed "$svc" 2>/dev/null
  systemctl start "$svc" 2>/dev/null
  if [ -n "$port" ]; then
    for i in $(seq 1 "$wait_max"); do
      sleep 1
      if ss -tln 2>/dev/null | grep -q ":$port "; then
        echo -e "  ${GREEN}✓${NC} $label  (puerto $port, ${i}s)"
        return 0
      fi
    done
    echo -e "  ${YELLOW}⚠${NC} $label  puerto $port no abrió en ${wait_max}s"
  else
    sleep 2
    systemctl is-active --quiet "$svc" \
      && echo -e "  ${GREEN}✓${NC} $label" \
      || echo -e "  ${YELLOW}⚠${NC} $label  no activo"
  fi
}

start_docker() {
  local container="$1" label="$2" port="${3:-}" wait_max="${4:-30}"
  if docker ps --format '{{.Names}}' 2>/dev/null | grep -q "^${container}$"; then
    echo -e "  ${GREEN}✓${NC} $label  container ya corriendo"
    return 0
  fi
  docker start "$container" >/dev/null 2>&1
  if [ -n "$port" ]; then
    for i in $(seq 1 "$wait_max"); do
      sleep 1
      if ss -tln 2>/dev/null | grep -q ":$port "; then
        echo -e "  ${GREEN}✓${NC} $label  (puerto $port, ${i}s)"
        return 0
      fi
    done
    echo -e "  ${YELLOW}⚠${NC} $label  container no levantó puerto $port en ${wait_max}s"
  else
    sleep 2
    echo -e "  ${GREEN}✓${NC} $label  container start enviado"
  fi
}

# ─── FASE 0: Red + Túnel (crítico - bloqueante) ─────────────────────────────
echo -e "\n${YELLOW}FASE 0 — Túnel Cloudflare${NC}" | tee -a "$LOG"
start_user tunnel-killer "Tunnel Killer" "" 5
# tunnel.service en /etc/systemd/system/ es alias de cloudflared.
# Lo levanta cloudflared directamente (no systemd).
if ! pgrep -f "cloudflared.*tunnel" > /dev/null; then
  echo -e "  ${YELLOW}⚠ cloudflared no está corriendo. Iniciar manualmente:${NC}"
  echo "    cloudflared --config /home/sam/.cloudflared/config_cineia.yml tunnel run"
else
  echo -e "  ${GREEN}✓${NC} cloudflared ya corriendo"
fi

# ─── FASE 1: Control + Monitoreo ────────────────────────────────────────────
echo -e "\n${YELLOW}FASE 1 — Control (Megapanel + Health)${NC}" | tee -a "$LOG"
start_user health-monitor "Health Monitor" 9000 15
start_user megapanel      "Megapanel"      9001 10

# ─── FASE 2: CPU/RAM — CineIA core (Studio, Movie, Post) ────────────────────
echo -e "\n${YELLOW}FASE 2 — CineIA core (CPU/RAM)${NC}" | tee -a "$LOG"
start_user cineia_studio_server "CineIA Studio"  8095 20
start_user movie_server          "Movie Server"   8090 25
start_user post_server           "Post-Production" 8014 15

# ─── FASE 3: CPU/RAM — Modelos auxiliares ────────────────────────────────────
echo -e "\n${YELLOW}FASE 3 — Modelos auxiliares (CPU/RAM)${NC}" | tee -a "$LOG"
start_user cineia-audioldm2 "AudioLDM2 SFX"  8013 20
start_user cineia-sadtalker "SadTalker"      8022 20
start_user cineia-musicgen  "MusicGen"       8023 20

# ─── FASE 4: GPU 0+1 — H3 ComfyUI workers ────────────────────────────────────
echo -e "\n${YELLOW}FASE 4 — GPU 0/1: H3 ComfyUI workers${NC}" | tee -a "$LOG"
start_user h3-gpu0 "H3 ComfyUI GPU 0" 8189 30
start_user h3-gpu1 "H3 ComfyUI GPU 1" 8190 30

# ─── FASE 5: GPU 2 — Flux (docker compartido) + RealESRGAN ──────────────────
echo -e "\n${YELLOW}FASE 5 — GPU 2: Flux + RealESRGAN${NC}" | tee -a "$LOG"
# cineia-flux es docker, compartido con otras instancias. NO destruir.
start_docker cineia-flux        "Flux (docker)" 8020 15
start_user   cineia-realesrgan  "RealESRGAN"   8021 15

# ─── FASE 6: Infraestructura ────────────────────────────────────────────────
echo -e "\n${YELLOW}FASE 6 — Infraestructura${NC}" | tee -a "$LOG"
start_sys redis-ojoia "Redis OjoIA" 6379 5

# ─── Resumen final ──────────────────────────────────────────────────────────
echo -e "\n${BLUE}═══ Resumen ═══${NC}" | tee -a "$LOG"
printf "  %-26s %-8s %s\n" "SERVICIO" "PUERTO" "ESTADO"
printf "  %-26s %-8s %s\n" "-------" "------" "------"

declare -A CHECK_USER=(
  ["Health Monitor"]="health-monitor:9000"
  ["Megapanel"]="megapanel:9001"
  ["CineIA Studio"]="cineia_studio_server:8095"
  ["Movie Server"]="movie_server:8090"
  ["Post-Production"]="post_server:8014"
  ["AudioLDM2"]="cineia-audioldm2:8013"
  ["SadTalker"]="cineia-sadtalker:8022"
  ["MusicGen"]="cineia-musicgen:8023"
  ["H3 GPU 0"]="h3-gpu0:8189"
  ["H3 GPU 1"]="h3-gpu1:8190"
  ["RealESRGAN"]="cineia-realesrgan:8021"
)
for name in "${!CHECK_USER[@]}"; do
  IFS=':' read -r svc port <<< "${CHECK_USER[$name]}"
  state=$(check_user "$svc")
  if [ "$state" = "active" ]; then
    printf "  ${GREEN}%-26s %-8s active${NC}\n" "$name" "$port" | tee -a "$LOG"
  else
    printf "  ${RED}%-26s %-8s %s${NC}\n" "$name" "$port" "$state" | tee -a "$LOG"
  fi
done

# System
for entry in "Redis OjoIA:redis-ojoia:6379" "Tunnel Killer:tunnel-killer:-"; do
  IFS=':' read -r name svc port <<< "$entry"
  if [ "$svc" = "tunnel-killer" ]; then
    state=$(check_user "$svc")
  else
    state=$(check_sys "$svc")
  fi
  if [ "$state" = "active" ]; then
    printf "  ${GREEN}%-26s %-8s active${NC}\n" "$name" "$port" | tee -a "$LOG"
  else
    printf "  ${RED}%-26s %-8s %s${NC}\n" "$name" "$port" "$state" | tee -a "$LOG"
  fi
done

# Docker
docker ps --format "  ${GREEN}%-26s${NC} 8020      active" --filter "name=cineia-flux" 2>/dev/null | tee -a "$LOG"

# Cloudflare
if pgrep -f "cloudflared.*tunnel" >/dev/null; then
  echo -e "  ${GREEN}Cloudflare Tunnel       -        active${NC}" | tee -a "$LOG"
else
  echo -e "  ${RED}Cloudflare Tunnel       -        inactive${NC}" | tee -a "$LOG"
fi

echo -e "\n${BLUE}GPUs:${NC}" | tee -a "$LOG"
nvidia-smi --query-gpu=index,memory.used,memory.total --format=csv,noheader | sed 's/^/  /' | tee -a "$LOG"

echo -e "\n${BLUE}Log: $LOG${NC}"
echo -e "${BLUE}═══ fin ═══${NC}"
