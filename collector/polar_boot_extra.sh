#!/data/data/com.termux/files/usr/bin/sh
# vehicle tablet — arranque de servicios Termux (idempotente, robusto a timing)
# Invocado por: termux-boot.sh (boot completo), VgateBridge (POWER_CONNECTED/USER_PRESENT)
# y CROND (cada minuto, mecanismo principal de auto-arranque en ciclos de corriente).
# NO toca VgateBridge (la app se gestiona sola via BootReceiver).

# 1. Cron de auto-mantenimiento: ejecuta este script cada minuto.
#    Es la capa fiable: en ciclos de corriente la tablet NO reinicia y Android
#    bloquea startService() desde la app en background (el RUN_COMMAND de la app
#    falla en silencio en Android 8+). El cron no depende de nada: si crond
#    está vivo, cada minuto levanta lo que falte.
if ! pgrep -x crond > /dev/null 2>&1; then
    crond
fi
crontab -l 2>/dev/null | grep -q polar_boot_extra || \
    echo '* * * * * sh ~/polar_boot_extra.sh >/dev/null 2>&1' | crontab -

# 2. Esperar a que Termux esté listo: en ciclos de corriente Termux acaba de
#    despertar y python3 puede tardar en estar disponible. Sin esta espera,
#    el nohup python3 del logger falla en silencio.
i=0
while [ $i -lt 30 ]; do
    command -v python3 >/dev/null 2>&1 && break
    sleep 1
    i=$((i+1))
done

# 3. SSH
if ! pgrep -x sshd > /dev/null 2>&1; then
    sshd
fi

# 4. GPS logger — doble intento: el primero puede fallar si el entorno aún no está listo
start_logger() {
    if ! pgrep -f "polar_gps_logger.py" > /dev/null 2>&1; then
        if [ -f ~/polar_gps_logger.py ]; then
            nohup python3 ~/polar_gps_logger.py >/dev/null 2>&1 &
        fi
    fi
}
start_logger
sleep 2
start_logger

# 5. Watchdog — igual
start_watchdog() {
    if ! pgrep -f "polar_watchdog.sh" > /dev/null 2>&1; then
        if [ -f ~/polar_watchdog.sh ]; then
            nohup sh ~/polar_watchdog.sh >/dev/null 2>&1 &
        fi
    fi
}
start_watchdog
sleep 2
start_watchdog

# 5b. Recolector OBD local — fuente primaria de datos SIN Internet.
#     (Añadido 2026-08-03: se moría tras cada arranque y NADIE lo relanzaba
#     → la webapp se quedaba sin datos. El crond lo mantiene vivo ahora.)
start_local_collector() {
    if ! pgrep -f "obd_local_collector.py" > /dev/null 2>&1; then
        if [ -f ~/obd_local_collector.py ]; then
            nohup python3 ~/obd_local_collector.py >/dev/null 2>&1 &
        fi
    fi
}
start_local_collector
sleep 2
start_local_collector

# 6. Tailscale — refuerzo del WakeService.
#    El WakeService (VgateBridge v9.2) espera red y abre Tailscale cuando hay
#    conectividad. Este refuerzo cubre el caso de que la ROM mate el proceso
#    de la app durante la espera: si hay red pero la VPN no responde, relanza
#    Tailscale (con anti-spam de 3 min para no abrir la UI cada minuto).
TS_FLAG=~/.tailscale_retry_ts
TS_PEER=100.64.0.1   # server — si responde, la VPN Tailscale está up
TS_INTERNET=1.1.1.1      # referencia de conectividad general

tailscale_connected() {
    ping -c 1 -W 2 $TS_PEER >/dev/null 2>&1
}

has_internet() {
    ping -c 1 -W 3 $TS_INTERNET >/dev/null 2>&1
}

if ! tailscale_connected; then
    if has_internet; then
        # Hay red pero Tailscale no conecta: relanzar (máx 1 vez cada 3 min)
        now=$(date +%s)
        last=0
        [ -f "$TS_FLAG" ] && last=$(cat "$TS_FLAG" 2>/dev/null || echo 0)
        if [ $((now - last)) -ge 180 ]; then
            echo "$now" > "$TS_FLAG"
            am start -n com.tailscale.ipn/.MainActivity >/dev/null 2>&1
            sleep 4
            # Volver al launcher del coche (no robar foco)
            am start -a android.intent.action.MAIN -c android.intent.category.HOME >/dev/null 2>&1
        fi
    fi
else
    # VPN OK: limpiar flag
    rm -f "$TS_FLAG"
fi
