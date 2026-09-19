#!/usr/bin/env bash
# redeploy_obd_collector.sh — mantiene el recolector de Polar Star igual al repo.
#
# El repo es la fuente de verdad: si el md5 del fichero en la tablet no coincide
# con el de aquí, lo sube, comprueba el md5 EN DESTINO y reinicia el proceso
# (crond lo relanza en ≤1 min). Si ya coinciden, silencio.
#
# ⚠️ Antes este script buscaba marcadores de una versión antigua (`sleep(6)` +
# `timeout=12`) y salía en silencio siempre: parecía un watchdog y no desplegaba
# nunca. Ahora compara contenido (md5), que es lo que se quiere de verdad.
#
# Cron: no_agent cada 5 min (stdout vacío = silencio; solo habla si despliega).
#
# Gate tailscale (2026-09-14): comprobar `tailscale status` (~0.01s) antes de
# SSH evita ~25-30s de timeouts cada 5 min contra una tablet apagada (lo normal
# con el coche parado).
#
# Las variables OBD_SRC/OBD_HOST/OBD_SSH/OBD_SCP/OBD_TAILSCALE/OBD_LOG existen
# para poder probar el script contra una tablet simulada (ver tests).
set -u

SCRIPT_SRC="${OBD_SRC:-$HOME/repos/obd-telemetry/collector/obd_local_collector.py}"
DEST="${OBD_DEST:-obd_local_collector.py}"     # relativo a ~ en la tablet
HOST="${OBD_HOST:-polar-star}"
SSH="${OBD_SSH:-ssh}"
SCP="${OBD_SCP:-scp}"
TS="${OBD_TAILSCALE:-tailscale}"
LOG="${OBD_LOG:-$HOME/.hermes/logs/redeploy_obd.log}"
OPTS=(-o ConnectTimeout=10 -o BatchMode=yes)
# `timeout` en TODA llamada remota: una tablet que responde al ssh y luego se cuelga a
# mitad de la transferencia dejaba el job colgado hasta que el scheduler lo matara (y con
# él el resto de la cadena de los 5 min). Con timeout, el ciclo siguiente lo reintenta.
# OBD_TIMEOUT permite ajustarlo; el fichero es pequeño, 90 s es de sobra.
TO=(timeout "${OBD_TIMEOUT:-90}")

# ¿Tablet online en tailscale? Si no aparece o está "offline", no intentar SSH.
ts_line="$("$TS" status 2>/dev/null | grep "$HOST" || true)"
if [ -z "$ts_line" ] || echo "$ts_line" | grep -q offline; then
    exit 0
fi

md5_local="$(md5sum "$SCRIPT_SRC" | awk '{print $1}')"
# Una sola ida y vuelta que distingue los tres casos: hash / AUSENTE / sin
# respuesta (sshd en pausa). Sin esto, un fichero que no existe en la tablet se
# confundía con "sshd mudo" y no se instalaba nunca.
remoto="$("${TO[@]}" "$SSH" "${OPTS[@]}" "$HOST" \
          "test -f $DEST && md5sum $DEST || echo AUSENTE" 2>/dev/null | awk '{print $1}')"

if [ -z "$remoto" ]; then
    echo "$(date '+%F %T') sin respuesta de sshd (¿Termux en pausa?)" >> "$LOG"
    exit 0
fi
if [ "$remoto" != "AUSENTE" ] && [ "$md5_local" = "$remoto" ]; then
    exit 0   # ya está al día — silencio
fi

if ! "${TO[@]}" "$SCP" "${OPTS[@]}" "$SCRIPT_SRC" "$HOST:$DEST" 2>/dev/null; then
    echo "$(date '+%F %T') scp falló; se reintenta en el próximo ciclo" >> "$LOG"
    exit 0
fi

# Verificación en destino: subir no es desplegar.
md5_destino="$("${TO[@]}" "$SSH" "${OPTS[@]}" "$HOST" "md5sum $DEST" 2>/dev/null | awk '{print $1}')"
if [ "$md5_destino" != "$md5_local" ]; then
    echo "$(date '+%F %T') md5 en destino NO coincide tras subir ($md5_destino != $md5_local)" >> "$LOG"
    exit 0
fi

# El proceso viejo sigue en memoria: matarlo para que crond lance el nuevo.
"${TO[@]}" "$SSH" "${OPTS[@]}" "$HOST" "pkill -f '[o]bd_local_collector.py'" 2>/dev/null

echo "🔧 Recolector OBD actualizado en la tablet (md5 ${md5_local:0:8})"
echo "$(date '+%F %T') recolector desplegado (md5 $md5_local)" >> "$LOG"
exit 0
