#!/bin/bash

LOG_FILE="/home/sam/tunnel.log"
PID_FILE="/home/sam/tunnel.pid"

start_tunnel() {
    echo "$(date) Starting cloudflared tunnel..." >> $LOG_FILE
    cloudflared tunnel --config /home/sam/.cloudflared/config.yml run ojoia-prod-v2 >> $LOG_FILE 2>&1 &
    echo $! > $PID_FILE
}

# Kill any existing tunnel
if [ -f $PID_FILE ]; then
    kill $(cat $PID_FILE) 2>/dev/null
    rm -f $PID_FILE
fi

# Wait for API to be ready
while ! curl -s http://localhost:8005/admin/server/status > /dev/null 2>&1; do
    echo "Waiting for API..." >> $LOG_FILE
    sleep 2
done

# Start tunnel
start_tunnel

# Monitor and restart if needed
while true; do
    sleep 30
    if ! pgrep -f "cloudflared tunnel" > /dev/null; then
        echo "$(date) Tunnel died, restarting..." >> $LOG_FILE
        start_tunnel
    fi
done