#!/usr/bin/env python3
import subprocess
import sys

# Activar entorno virtual y ejecutar wrapper
result = subprocess.run(
    ['/bin/bash', '-c', 'source /home/sam/ai_system/venv/bin/activate && python3 /home/sam/ai_system/api_eva_chat_wrapper.py'],
    capture_output=False
)
