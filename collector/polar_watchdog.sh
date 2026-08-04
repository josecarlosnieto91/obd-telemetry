#!/data/data/com.termux/files/usr/bin/sh
# vehicle tablet Watchdog — asegura que todos los servicios críticos
# estén siempre funcionando. Corre como loop infinito.
# Se lanza desde Termux:Boot y se recupera de suspensiones.

SLEEP_INTERVAL=30

# Helper: check if process is running by name
is_running() {
    ps aux 2>/dev/null | grep -v grep | grep -q "$1"
    return $?
}

log() {
    echo "[$(date '+%H:%M:%S')] watchdog: $*"
}

log "Watchdog iniciado (cada ${SLEEP_INTERVAL}s)"

while true; do
    # 1. SSH daemon
    if ! is_running "sshd"; then
        log "sshd caido — reiniciando"
        sshd
    fi

    # 2. GPS logger
    if ! is_running "polar_gps_logger.py"; then
        log "GPS Logger caido — reiniciando"
        if [ -f ~/polar_gps_logger.py ]; then
            nohup python3 ~/polar_gps_logger.py >/dev/null 2>&1 &
        fi
    fi

    sleep "${SLEEP_INTERVAL}"
done
