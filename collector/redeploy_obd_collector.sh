#!/usr/bin/env bash
# redeploy_obd_collector.sh — despliega la versión definitiva del recolector
# OBD local en Polar Star cuando la tablet está online.
#
# Versión definitiva = tiene `sleep(6)` (ventana init ELM327) y `timeout=12`
# (warm-up). Si la tablet no está alcanzable o la versión ya es la definitiva,
# no hace nada (silencio). Tras desplegar, mata el proceso: crond lo relanza
# en ≤1 min con la versión nueva.
#
# Cron: no_agent cada 5 min (stdout vacío = silencio).
#
# Gate rápido (fix 2026-09-14): antes de SSH, comprobar tailscale status
# (~0.01s). Evita ~25-30s de timeouts SSH cada 5 min contra una tablet apagada
# (estado normal del coche parado) y el spam del log "red caída".

SCRIPT_SRC="$HOME/repos/obd-telemetry/collector/obd_local_collector.py"
LOG="/home/josecnr91/.hermes/logs/redeploy_obd.log"

# ¿Tablet online en tailscale? Si no aparece o está "offline", no intentar SSH.
ts_line="$(tailscale status 2>/dev/null | grep 'polar-star' || true)"
if [ -z "$ts_line" ] || echo "$ts_line" | grep -q 'offline'; then
    exit 0  # tablet apagada/no registrada — silencio, sin ensuciar el log
fi

# ¿Versión definitiva ya desplegada?
if timeout 15 ssh -o ConnectTimeout=10 -o BatchMode=yes polar-star \
    "grep -q 'sleep(6)' ~/obd_local_collector.py && grep -q 'timeout=12' ~/obd_local_collector.py" 2>/dev/null; then
    exit 0  # ya está — silencio
fi

# Intentar desplegar
if scp -o ConnectTimeout=10 -o BatchMode=yes "$SCRIPT_SRC" polar-star:~/ 2>/dev/null; then
    timeout 15 ssh -o ConnectTimeout=10 polar-star \
        'pkill -f "[o]bd_local_collector.py" 2>/dev/null; echo ok' 2>/dev/null
    echo "$(date '+%Y-%m-%d %H:%M:%S') recolector OBD desplegado (definitivo)" >> "$LOG"
else
    echo "$(date '+%Y-%m-%d %H:%M:%S') red caída, reintento pendiente" >> "$LOG"
fi
exit 0
