#!/bin/bash
cd /home/sam/ai_system

# Source virtual environment
source venv/bin/activate

# Start API Eva
python api_eva.py &
API_PID=$!

# Wait for API to be ready
for i in {1..30}; do
    if curl -s http://localhost:8005/admin/server/status > /dev/null 2>&1; then
        break
    fi
    sleep 1
done

# Start cloudflared tunnel
cloudflared tunnel --config /home/sam/.cloudflared/config.yml run ojoia-prod-v2 &

# Wait for both processes
wait $API_PID