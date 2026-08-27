#!/bin/bash
# OjoIA System Watchdog — runs from cron every minute
LOG="/home/sam/watchdog.log"

now() { date -Iseconds; }

restart_backend() {
    if systemctl --user restart api_eva.service >/dev/null 2>&1; then
        echo "[$(now)] Backend restarted via user service" >> "$LOG"
    else
        echo "[$(now)] Backend restart failed" >> "$LOG"
    fi
}

restart_tunnel() {
    if sudo -n systemctl restart tunnel.service >/dev/null 2>&1; then
        echo "[$(now)] Tunnel restarted via sudo/system service" >> "$LOG"
    elif systemctl restart tunnel.service >/dev/null 2>&1; then
        echo "[$(now)] Tunnel restarted via system service" >> "$LOG"
    elif systemctl --user restart tunnel.service >/dev/null 2>&1; then
        echo "[$(now)] Tunnel restarted via user service" >> "$LOG"
    else
        echo "[$(now)] Tunnel restart failed: sudo/root required or tunnel.service unavailable" >> "$LOG"
    fi
}

# 1) health check
if ! curl -sf --max-time 3 http://localhost:8005/health >/dev/null 2>&1; then
    echo "[$(now)] Backend DOWN — restarting..." >> "$LOG"
    restart_backend
    sleep 2
fi

# 2) Cloudflare Edge check
if ! curl -sf --max-time 5 https://api.ojoia.com.do/health >/dev/null 2>&1; then
    echo "[$(now)] Tunnel OFFLINE — restarting..." >> "$LOG"
    restart_tunnel
    sleep 2
fi
