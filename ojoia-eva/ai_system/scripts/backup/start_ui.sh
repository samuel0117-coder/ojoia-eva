#!/bin/bash
cd /home/sam/ai_system
nohup /home/sam/ai_system/venv/bin/python -c "
import uvicorn
uvicorn.run('ui_server:APP', host='0.0.0.0', port=8443, 
            ssl_keyfile='certs/ui_server.key', ssl_certfile='certs/ui_server.crt',
            log_level='warning')
" > /home/sam/ai_system/logs/ui.log 2>&1 &
echo "UI server started on https://10.0.0.44:8443"
