#!/bin/bash
# /home/sam/startup_system.sh
# Encendido manual del sistema - ejecuta el boot principal

G='\033[0;32m'; NC='\033[0m'
echo -e "${G}[INICIO] Ejecutando boot_system.sh...${NC}"
bash /home/sam/ai_system/scripts/boot_system.sh
