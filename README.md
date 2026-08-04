# OBD Telemetry — Vehicle Telemetry System

Sistema telemático DIY para vehículos: captura OBD2 + GPS, telemetría local,
dashboard web y análisis de viajes. Diseñado originalmente para un Citroën C4
Grand Picasso I (2006, DW10BTED4 2.0 HDi) pero **portable a cualquier coche con
conector OBD-II y a cualquier adaptador ELM327 Bluetooth**.

```
┌───────────────────────┐        ┌─────────────────────────────┐
│  VEHICLE (tablet)     │   SCP  │  CASSIOPEIA (server)        │
│  vehicle tablet           │ ─────▶ │  obd_local_import (cron)    │
│  ┌─────────────────┐  │  c/5m  │  → obd_telemetry.db         │
│  │ VgateBridge APK │  │        │  → trip_summary (viajes)    │
│  │ BT SPP → TCP    │  │        │  → car_status (diagnóstico) │
│  │ :22000          │  │        │  → refuel_detector          │
│  ├─────────────────┤  │        │  → WebApp Flask :8765       │
│  │ obd_local_collector│    │   │  → Janus (contexto)         │
│  │ GPS logger       │  │        └─────────────────────────────┘
│  └─────────────────┘  │
└───────────────────────┘
```

## Funcionalidades

### Recolección (autónoma, sin Internet)
- **`obd_local_collector.py`** (en la tablet): lee PIDs OBD2 del bridge TCP local
  `127.0.0.1:22000` + GPS (`termux-location`), guarda en SQLite local
  `~/obd_data/obd_local.db`. Cada ~5 ciclos sube el fichero a server por SCP.
  No depende de red: si no hay Internet, la captura sigue.
- **Escaneo de PIDs soportados**: bitmap SAE J1979 (`0100/0120/...`) → solo lee
  los PIDs que el motor expone. PIDs diésel (MAP/ambient/presión raíl) opcionales.
- **DTCs**: Mode 03 (almacenados) + Mode 07 (pendientes), descripciones del config.
- **Regeneración FAP** (si `has_dpf`): heurística por comportamiento (ralentí
  elevado + MAF alto en vacío) sin PIDs de fabricante.
- **Calibración automática**: aprende valores normales del vehículo (rpm_idle,
  maf_idle, coolant_cruise, voltajes) con media acumulada.

### Viajes y consumo
- **`trip_summary.py`** (cron c/5 min): cierra sesiones inactivas y genera resumen
  con distancia (haversine), duración, velocidades, RPM, consumo estimado desde
  MAF (densidad del combustible del config). Nombres de lugar con Nominatim.
- **Cierre por parada real**: el viaje se cierra cuando el coche lleva 10 min sin
  moverse, aunque el bridge siga emitiendo. Arranques sin movimiento = no-viaje
  silencioso (0 km).
- **Consejos de conducción/mantenimiento** post-viaje (tabla `alerts`).

### Estado del coche
- **Odómetro virtual**: referencia del cuadro + km de sesiones posteriores.
- **ITV**: días restantes con alerta automática (< 60 días).
- **Mantenimiento programado**: aceite, correa, filtros... con % de intervalo
  consumido y registro de servicios.
- **Diagnósticos**: batería (voltaje en reposo), termostato (temp en viajes),
  turbo (MAP en crucero, si `has_turbo`), ralentí (desviación RPM).

### Repostajes
- **`refuel_gps_detector.py`** (cron c/10 min): al cerrar un viaje, si la última
  posición está a <200 m de una gasolinera (OSM) → posible repostaje.
- **Precio automático**: API oficial del Ministerio (España), estación más
  cercana por GPS, producto del config (`fuel_price_product`).
- **Litros**: confirmación manual en la webapp (botón "Litros").

### Webapp (Flask :8765)
- Dashboard: lecturas en vivo, gauges, gráfico del último trayecto.
- Historial de viajes con consumo, detalle por viaje, DTCs.
- Mapa Leaflet con selector de calendario y tracks GPX.
- Alertas y consejos descartables, mantenimiento, repostajes.
- API REST completa (`/api/status`, `/api/trips`, `/api/map`, `/api/refuels`...).

## Portabilidad

### Cambiar de coche
Todo lo específico del vehículo vive en `obd_vehicle_config.json`
(copia en `docs/obd_vehicle_config.example.json`):

| Campo | Efecto |
|-------|--------|
| `fuel_type` | diesel / gasoline (semántico) |
| `fuel_density_g_l` | densidad para consumo MAF (diésel ~832, gasolina ~740) |
| `fuel_price_product` | producto del Ministerio ("Gasoleo A", "Gasolina 95 E5") |
| `has_turbo` | activa/desactiva diagnóstico de turbo (MAP) |
| `has_dpf` | activa/desactiva heurística FAP |
| `dtc_descriptions` | diccionario de códigos del fabricante |
| `thresholds`, `maintenance`, `itv` | umbrales y plan de mantenimiento |

Los PIDs se escanean automáticamente; la calibración aprende sola. Cambiar de
coche = nuevo config + seleccionar el adaptador en el bridge.

### Cambiar de adaptador OBD
- **ELM327 Bluetooth clásico (SPP)**: compatible directamente (Vgate vLinker MC,
  OBDLink LX, etc.). Solo hay que emparejar y seleccionar la MAC en la app.
- **vLinker MC+ (BLE + SPP)**: funciona por SPP.
- **WiFi / BLE-only**: requiere cambios (apuntar el recolector al IP:35000 o
  implementar BLE en el bridge).

## Instalación

### Requisitos y dependencias

**Qué es obligatorio vs opcional** — el sistema funciona en dos capas:

| Capa | Componente | ¿Obligatorio? | Para qué |
|------|-----------|:---:|----------|
| **Tablet** (en el coche) | Termux | ✅ | Ejecuta el recolector Python, GPS logger, crond y SSH |
| | Termux:API | ✅ | GPS (`termux-location`) y wake-lock |
| | Termux:Boot | ✅ | Auto-arranque al encender la tablet |
| | Python 3 + cronie | ✅ | Runtime del recolector y auto-reparación (crond) |
| | **Tailscale** | ⚠️ Opcional | Solo para acceso remoto al dashboard y sync cuando el coche no está en tu red WiFi |
| | VgateBridge APK | ✅ | Puente Bluetooth SPP → TCP local :22000 |
| **Servidor** (en casa) | Python 3.11+ | ✅ | Scripts + webapp Flask |
| | Tailscale | ⚠️ Opcional | Solo para acceder al dashboard de forma remota |
| | systemd user | ✅ | Webapp como servicio |

**Sin Tailscale** el sistema sigue funcionando: la tablet sube datos por SCP solo cuando
hay red (WiFi del coche/hotspot). Tailscale solo añade conectividad permanente y acceso
remoto al dashboard.

**Arquitectura de red — quién habla con quién:**

```
Adaptador ELM327 ──BT──▶ VgateBridge (APK, SERVIDOR TCP :22000 en la tablet)
                                  ▲
                                  │ 127.0.0.1:22000
                                  │
                        obd_local_collector.py (CLIENTE, en la tablet)
                                  │
                                  │ SCP (SSH)
                                  ▼
                        Servidor (Cassiopeia) → obd_telemetry.db → webapp :8765
```

El APK **no necesita servidor configurable**: es el servidor local. El destino
remoto se configura en `obd_local_collector.py` (constantes `CASSIOPEIA` /
`INCOMING_PATH` al inicio del fichero).

```bash
git clone git@github-obd-telemetry:josecarlosnieto91/obd-telemetry.git ~/repos/obd-telemetry
mkdir -p ~/.hermes/scripts ~/.hermes/data/incoming/processed ~/.hermes/data/tracks
cp -r collector/*.py collector/*.sh ~/.hermes/scripts/
cp -r webapp ~/.hermes/obd_webapp
cp docs/obd_vehicle_config.example.json ~/.hermes/scripts/obd_vehicle_config.json
# editar el config con los datos del vehículo real

# Webapp como servicio systemd user
cat > ~/.config/systemd/user/obd-webapp.service <<'EOF'
[Unit]
Description=OBD Telemetry WebApp
After=network-online.target

[Service]
Type=simple
WorkingDirectory=%h/.hermes/obd_webapp
ExecStart=/usr/bin/python3 %h/.hermes/obd_webapp/app.py
Restart=on-failure

[Install]
WantedBy=default.target
EOF
systemctl --user daemon-reload
systemctl --user enable --now obd-webapp
```

### 2. Crons (Hermes o cron del sistema)
```
obd_local_import.py    cada 5 min   (importa el fichero de la tablet)
trip_summary.py        cada 5 min   (cierra viajes, resumen a Telegram)
refuel_gps_detector.py cada 10 min  (detecta repostajes)
car_status.py          cada 15 min  (diagnósticos + alertas)
```

### 3. Tablet (vehicle tablet)

**Instalar desde F-Droid** (no Play Store):
1. **Termux** — runtime del recolector, GPS logger, SSH y crond.
2. **Termux:API** — necesario para GPS (`termux-location`) y wake-lock.
3. **Termux:Boot** — ejecuta los scripts de auto-arranque al encender la tablet.

**Configurar Termux:**
```bash
pkg update && pkg install -y openssh python cronie termux-api
sshd                                    # SSH en :8022 para el servidor
mkdir -p ~/.termux/boot ~/.termux
# Conceder a Termux:API → Permisos → Ubicación → Permitir siempre
# Ajustes → Apps → Termux → Batería → Sin restricciones (evita que Android lo mate)
```

**Auto-arranque** — copiar los scripts del repo:
```bash
# Desde el servidor:
scp collector/polar_boot_extra.sh tablet:~/
scp collector/polar_watchdog.sh tablet:~/
scp collector/obd_local_collector.py tablet:~/
scp collector/polar_gps_logger.py tablet:~/
# En la tablet:
mkdir -p ~/.termux/boot
cp ~/polar_boot_extra.sh ~/.termux/boot/start-services   # boot completo
chmod +x ~/.termux/boot/start-services
# El crond (cada minuto) ejecuta polar_boot_extra.sh como red de seguridad
# para ciclos de corriente sin reinicio completo.
```

**App VgateBridge** — instalar el APK desde el release de
[vgate-bridge](https://github.com/josecarlosnieto91/vgate-bridge/releases),
abrirla una vez, emparejar el adaptador ELM327 por Bluetooth y seleccionarlo en
la app (guardar la MAC). Con el modo coche activado, arranca solo al dar
corriente.

**Destino del sync** — en `obd_local_collector.py` de la tablet, editar las
constantes `CASSIOPEIA` (usuario@host del servidor) e `INCOMING_PATH` si tu
layout difiere. Requiere clave SSH reversa (tablet → servidor) configurada.

### 4. Base de datos
La webapp y los scripts crean el esquema automáticamente
(`sessions`, `readings`, `positions`, `dtc`, `alerts`, `refuels`, `services`,
`fap_events`, `calibration`). SQLite en modo WAL (concurrencia entre crons).

## Scripts

| Script | Dónde corre | Propósito |
|--------|------------|-----------|
| `obd_local_collector.py` | Tablet | Recolección OBD2+GPS local, autónoma |
| `obd_local_import.py` | Servidor | Mergea el fichero local en `obd_telemetry.db` |
| `trip_summary.py` | Servidor | Cierra viajes, resumen, consejos, Janus |
| `car_status.py` | Servidor | Odómetro, ITV, mantenimiento, diagnósticos |
| `refuel_gps_detector.py` | Servidor | Detecta repostajes por GPS+OSM |
| `fuel_price.py` | Servidor | Precios del Ministerio (módulo) |
| `obd_scan.py` | Servidor | Escaneo exhaustivo de PIDs (diagnóstico) |
| `polar_gps_logger.py` | Tablet | Logger GPS continuo → tracks GPX |
| `polar_watchdog.sh` | Tablet | Mantiene sshd + logger vivos |
| `polar_boot_extra.sh` | Tablet | Auto-arranque de servicios (crond) |

## Repos
- [obd-telemetry](https://github.com/josecarlosnieto91/obd-telemetry) — este repo
- [vgate-bridge](https://github.com/josecarlosnieto91/vgate-bridge) — app Android BT→TCP

## Notas de diseño
- SQLite en **WAL** con `busy_timeout=10000`: 5 procesos escriben la misma BD
  (import, trip_summary, car_status, refuel, webapp) sin `database is locked`.
- La tablet sube una **copia consistente** del fichero local (snapshot con la
  API de backup de SQLite), no el fichero vivo.
- El recolector local tiene **guard de instancia única** (flock) — boot + crond
  pueden lanzarlo dos veces casi a la vez.
