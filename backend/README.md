# Backend Multi-Nodo — OjoIA + CineIA

Arquitectura compartida entre ambos servidores.

## Estructura

```
backend/
├── megapanel/          # Dashboard maestro (corre en ojoia:9001)
├── cineia-agent/       # Agente worker (corre en cineia:8300)
├── health_monitor/     # Watchdog (corre en ojoia)
├── service_bus/        # Proxy + billing (corre en ojoia)
├── shared/             # Config central
├── firebase/           # Hosting config
└── deploy.sh           # Deploy en ambos nodos
```

## Deploy

```bash
# En ojoia:
git pull origin main && ./backend/deploy.sh ojoia

# En cineia:
git pull origin main && ./backend/deploy.sh cineia
```

## Variables de entorno

| Variable | Default | Uso |
|----------|---------|-----|
| `NODE_ID` | ojoia | Identificador del nodo |
| `CINEIA_AGENT_URL` | http://10.0.0.44:8300 | URL del agente cineia |
| `MEGAPANEL_TOKEN` | — | Token auth Bearer |
| `CINEIA_HOST` | 10.0.0.44 | IP del nodo cineia |
