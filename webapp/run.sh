#!/usr/bin/env bash
# OBD Telemetry WebApp — start/stop/status
cd "$(dirname "$0")"
case "${1:-start}" in
    start)
        echo "🚗 Arrancando OBD WebApp..."
        exec python3 app.py
        ;;
    stop)
        pkill -f "python3 app.py" 2>/dev/null && echo "🛑 Detenido" || echo "⚠ No estaba corriendo"
        ;;
    restart)
        $0 stop; sleep 1; $0 start
        ;;
    status)
        if pgrep -f "python3 app.py" >/dev/null; then
            echo "🟢 Corriendo en http://$(hostname -I | awk '{print $1}'):8765"
        else
            echo "🔴 Detenido"
        fi
        ;;
    *)
        echo "Uso: $0 {start|stop|restart|status}"
        ;;
esac
