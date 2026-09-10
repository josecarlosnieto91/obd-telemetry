#!/usr/bin/env bash
# deploy_obd_v3.sh — despliega el recolector OBD v3 (DTCs + PIDs diésel +
# regeneración FAP + calibración) en Polar Star.
# Ejecutar cuando la tablet esté online (coche arrancado).
# Uso: bash ~/repos/obd-telemetry/collector/deploy_obd_v3.sh

set -e
SRC=~/.hermes/scripts
DST=polar-star

echo "=== 1. Config del vehículo ==="
scp -o ConnectTimeout=10 "$SRC/obd_vehicle_config.json" "$DST:~/obd_vehicle_config.json"
echo "   OK"

echo "=== 2. Recolector local v3 (DTC + diésel + FAP + calibración) ==="
scp -o ConnectTimeout=10 "$SRC/obd_local_collector.py" "$DST:~/obd_local_collector.py"
echo "   OK"

echo "=== 3. Importador (Cassiopeia ya lo tiene) ==="
echo "   OK"

echo "=== 4. Reiniciar recolector en la tablet (crond lo relanza) ==="
ssh -o ConnectTimeout=10 "$DST" "pkill -f obd_local_collector.py; sleep 2; nohup python3 ~/obd_local_collector.py >/dev/null 2>&1 & sleep 3; pgrep -f obd_local_collector.py >/dev/null && echo '   recolector VIVO' || echo '   recolector MUERTO'"

echo "=== 5. Verificación ==="
ssh -o ConnectTimeout=10 "$DST" "grep -c 'def scan_supported_pids\|def detect_fap_regen\|def update_calibration' ~/obd_local_collector.py | xargs echo '   funciones v3 presentes:'; ls -la ~/obd_vehicle_config.json"

echo ""
echo "✅ Despliegue OBD v3 completado."
echo "   El recolector ahora: lee DTCs cada ~5min, escanea PIDs diésel al arrancar,"
echo "   detecta regeneración FAP (ralentí elevado + MAF alto sostenido) y"
echo "   aprende calibración (ralentí, crucero, voltaje) con datos reales."
