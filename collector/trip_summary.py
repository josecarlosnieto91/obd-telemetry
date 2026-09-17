#!/usr/bin/env python3
"""Resumen de viaje Polar Star — cierra sesiones OBD2 terminadas y emite resumen.

Patrón cron no_agent:
  - stdout VACÍO  → silencio (no hay viaje terminado, no se entrega nada)
  - stdout TEXTO  → resumen entregado por Telegram

Además inserta el viaje en el sistema de contexto (context.db) para Janus:
  - evento type='vehiculo' value='viaje' (detail = JSON con métricas)
  - fila en tabla trips
"""
import sqlite3, os, sys, math, json, time, urllib.request, urllib.parse
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fuel_consumption as fc      # consumo real (depósito a depósito)

OBD_DB = os.path.expanduser("~/.hermes/data/obd_telemetry.db")
CTX_DB = os.path.expanduser("~/.hermes/data/context/context.db")
CONFIG_PATH = os.path.expanduser("~/.hermes/scripts/obd_vehicle_config.json")
GEOCODE_CACHE = os.path.expanduser("~/.hermes/data/geocode_cache.json")
GEOCODE_MIN_INTERVAL = 1.1   # segundos entre peticiones a Nominatim (su política: máx 1/s)
IDLE_MINUTES = 15     # sin lecturas NUEVAS durante 15 min => bridge apagado, viaje terminado
STOPPED_MINUTES = 15  # coche parado (speed < MIN_MOVING_SPEED) durante 15 min => viaje terminado
MIN_MOVING_SPEED = 1.0   # km/h: por debajo, el coche está parado
MIN_TRIP_KM = 0.5        # km mínimos para considerar viaje real (filtro deriva GPS)
MIN_TRIP_MAX_SPEED = 5.0 # km/h: si nunca supera, no es viaje real (arranque parado)
MIN_JUMP_M = 30.0        # metros mínimos entre posiciones consecutivas para sumar distancia


def _fuel_density():
    """Densidad del combustible (g/L) según vehicle.fuel_density_g_l del config.
    Portabilidad 2026-08-05: diésel ~832, gasolina ~740. Fallback 832 (C4 HDi).
    El consumo MAF→litros depende del combustible real."""
    try:
        with open(CONFIG_PATH) as fh:
            return float(json.load(fh).get("vehicle", {}).get("fuel_density_g_l", 832.0))
    except Exception:
        return 832.0


def _maf_cruise_warn():
    """Umbral MAF de carga media alta (g/s) según thresholds del config.

    ⚠️ FIX 2026-08-08: antes hardcodeado a 25.0 g/s — genérico y mal calibrado
    para un diésel 2.0 (crucero 120 km/h = 25-35 g/s, disparaba alerta falsa).
    El config del C4 define maf_cruise_warn_gs=40.0; fallback 40.0."""
    try:
        with open(CONFIG_PATH) as fh:
            return float(json.load(fh).get("thresholds", {}).get("maf_cruise_warn_gs", 40.0))
    except Exception:
        return 40.0


def _threshold(key, fallback):
    """Lee un umbral numérico del config (thresholds.<key>); fallback si falta.
    Parametrización 2026-08-08: los consejos de viaje ya no llevan valores
    hardcodeados (antes: 3000 rpm, 120 km/h, 100 °C, 5000 rpm)."""
    try:
        with open(CONFIG_PATH) as fh:
            return float(json.load(fh).get("thresholds", {}).get(key, fallback))
    except Exception:
        return fallback


DENSITY_FUEL = _fuel_density()  # g/L — usado en el cálculo de consumo MAF
DIESEL_AFR = 30.0               # relación aire-combustible diésel típica en crucero
                                # (fallback cuando no hay fuel_rate del PID 015E)


def connect_db(path, row_factory=True):
    """Conexión SQLite con protección de concurrencia.

    WAL permite 1 escritor + N lectores simultáneos (sin locks de lectura);
    busy_timeout hace esperar a los escritores en vez de fallar al instante.
    Varios procesos (import, trip_summary, car_status, refuel, webapp) escriben
    la misma BD — sin esto, 'database is locked' aleatorio entre crons.
    """
    conn = sqlite3.connect(path, timeout=10.0)
    conn.execute("PRAGMA busy_timeout=10000")
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.OperationalError:
        pass  # WAL no disponible en algún FS raro; timeout sigue protegiendo
    if row_factory:
        conn.row_factory = sqlite3.Row
    # Migración idempotente: consumo por viaje (fuel_liters, consumption_l100)
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(sessions)")]
        for col in ("fuel_liters", "consumption_l100", "real_l100"):
            if col not in cols:
                conn.execute(f"ALTER TABLE sessions ADD COLUMN {col} REAL")
    except sqlite3.OperationalError:
        pass
    return conn


def _cache_key(lat, lon):
    """Clave de caché a 3 decimales (~110 m).

    Lo que devuelve Nominatim para el zoom 12 es el municipio o la localidad, así
    que redondear 110 m no cambia la respuesta y convierte en UNA consulta las
    cientos de veces que el coche se para en el mismo sitio (casa, trabajo, la
    gasolinera de siempre).
    """
    return "%.3f,%.3f" % (round(lat, 3), round(lon, 3))


def _leer_cache(path):
    try:
        with open(path) as fh:
            datos = json.load(fh)
        return datos if isinstance(datos, dict) else {}
    except (OSError, ValueError):
        return {}


def reverse_geocode_cached(lat, lon, cache_path=None, esperar=True):
    """`reverse_geocode` con caché en disco y respetando 1 petición/segundo.

    Sin caché, cada cierre de viaje (y cada relleno del histórico) volvería a
    preguntar por los mismos sitios. Solo se guardan los aciertos: un fallo de
    red no se cachea, para poder reintentarlo.
    """
    if lat is None or lon is None:
        return None
    path = cache_path or GEOCODE_CACHE
    clave = _cache_key(lat, lon)
    cache = _leer_cache(path)
    if clave in cache:
        return cache[clave]
    if esperar:
        time.sleep(GEOCODE_MIN_INTERVAL)   # política de Nominatim: máx 1 req/s
    lugar = reverse_geocode(lat, lon)
    if not lugar:
        return None
    cache[clave] = lugar
    try:
        tmp = path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(cache, fh, ensure_ascii=False)
        os.replace(tmp, path)              # atómico
    except OSError:
        pass
    return lugar


def nombre_lugar(lat, lon):
    """Nombre del sitio; si no se puede resolver, las coordenadas.

    Unas coordenadas son un dato (dicen dónde fue); «sin determinar» no dice
    nada. Es el mismo criterio que usa la línea `📍 fin:` del resumen.
    """
    if lat is None or lon is None:
        return None
    return reverse_geocode_cached(lat, lon) or "%.4f,%.4f" % (lat, lon)


def reverse_geocode(lat, lon):
    """Reverse geocoding con Nominatim (OpenStreetMap).
    Devuelve el nombre del lugar o None si falla. 1 petición por viaje (política
    de Nominatim: máx 1 req/s con User-Agent identificable)."""
    try:
        url = "https://nominatim.openstreetmap.org/reverse?" + urllib.parse.urlencode({
            "format": "json", "lat": lat, "lon": lon,
            "zoom": 12, "accept-language": "es",
        })
        req = urllib.request.Request(url, headers={
            "User-Agent": "VehicleTelemetry/1.0 (personal vehicle tracker)",
        })
        with urllib.request.urlopen(req, timeout=6) as r:
            data = json.loads(r.read().decode())
        addr = data.get("address", {})
        place = (
            addr.get("city") or addr.get("town") or addr.get("village")
            or addr.get("municipality") or addr.get("county") or addr.get("state")
        )
        if place:
            return place
        dn = data.get("display_name", "")
        if dn:
            return dn.split(",")[0].strip()
    except Exception:
        pass
    return None


def haversine(lat1, lon1, lat2, lon2):
    R = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def parse_ts(s):
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return None


def real_consumption(c, end_ts, km_per_l, session_id, dist):
    """Consumo REAL (l/100km) y de dónde sale — no el del cuadro.

    1) **Depósito en curso**: litros por la caída de rango CAN desde el último
       repostaje (rango calibrado contra el surtidor) / km hechos desde entonces.
    2) Si aún no hay km suficientes (recién repostado), el **último depósito
       completo**: km entre los dos últimos repostajes / litros del último.

    El cuadro del coche marca ~la mitad (15/09: 4,2-5,7 frente a 8,29 reales).
    Comprobado con datos reales: sin el filtro `ts <= end_ts`, un viaje del 13/09
    usaba el repostaje del 15/09 (posterior a él) y daba 39 l/100km.
    Devuelve ``(valor, etiqueta)`` o ``(None, None)``.
    """
    fills = c.execute(
        "SELECT ts, liters, fuel_after FROM refuels "
        "WHERE full_tank=1 AND fuel_after IS NOT NULL AND fuel_after > 0 "
        "AND ts <= ? ORDER BY ts DESC LIMIT 2", (end_ts,)).fetchall()
    if not fills:
        return None, None
    rango = c.execute(
        "SELECT range_km FROM can_readings WHERE range_km IS NOT NULL AND ts <= ? "
        "ORDER BY ts DESC LIMIT 1", (end_ts,)).fetchone()
    if not rango or not rango["range_km"]:
        return None, None
    lleno = fills[0]
    # El viaje que se cierra ahora todavía no tiene distance_km en la tabla: se suma
    km_previos = c.execute(
        "SELECT COALESCE(SUM(distance_km),0) AS km FROM sessions "
        "WHERE id != ? AND start_time > ? AND start_time <= ?",
        (session_id, lleno["ts"], end_ts)).fetchone()["km"] or 0.0
    km_actual = km_previos + (dist or 0.0)
    actual = fc.consumption_from_range(lleno["fuel_after"] - rango["range_km"],
                                       km_actual, km_per_l)
    ultimo = None
    if len(fills) > 1:
        a, b = fills[1], fills[0]
        km_tanque = c.execute(
            "SELECT COALESCE(SUM(distance_km),0) AS km FROM sessions "
            "WHERE start_time > ? AND start_time < ?",
            (a["ts"], b["ts"])).fetchone()["km"] or 0.0
        ultimo = fc.l100(km_tanque, b["liters"])
    return fc.pick_current_or_last(km_actual, actual, ultimo)


TIP_CATEGORIES = ("conduccion", "mantenimiento", "uso")


def generate_tips(conn, c, session_id, readings, dist, dur_min, replace=False):
    """Genera consejos de conducción/mantenimiento/uso tras cerrar un viaje
    y los inserta en la tabla alerts (los consume la webapp /maintenance).
    Portado desde obd_collector.py (el recolector TCP live está pausado).

    `replace=True`: borra antes los avisos de esa sesión en las categorías que
    produce esta función, para que recalcular un viaje no acumule consejos
    viejos. Los avisos de otro origen (termostato, batería, calendario de
    mantenimiento) van con session_id NULL o fuera de TIP_CATEGORIES: no se
    tocan."""
    if replace:
        c.execute(
            "DELETE FROM alerts WHERE session_id=? AND category IN (?,?,?)",
            (session_id, *TIP_CATEGORIES))
        conn.commit()
    if not readings:
        return

    rpms = [r["rpm"] for r in readings if r.get("rpm") is not None]
    speeds = [r["speed"] for r in readings if r.get("speed") is not None]
    temps = [r["coolant_temp"] for r in readings if r.get("coolant_temp") is not None]
    mafs = [r["maf"] for r in readings if r.get("maf") is not None]
    volts = []  # voltage no se lee en el flujo local (solo OBD2 directo); vacío

    avg_rpm = sum(rpms) / len(rpms) if rpms else None
    max_rpm = max(rpms) if rpms else None
    avg_speed = sum(speeds) / len(speeds) if speeds else None
    max_speed = max(speeds) if speeds else None
    max_temp = max(temps) if temps else None
    avg_maf = sum(mafs) / len(mafs) if mafs else None

    tips = []

    # Conducción eficiente
    if avg_rpm and avg_rpm > _threshold("rpm_high_warn", 3000):
        tips.append(("conduccion", "info",
            f"RPM medios altos ({avg_rpm:.0f} rpm). Para ahorrar combustible, "
            "cambia a una marcha superior cuando el motor supere las 2500 rpm."))
    if avg_rpm and avg_rpm < _threshold("rpm_low_econ", 1500) and avg_speed and avg_speed > _threshold("speed_econ_kmh", 60):
        tips.append(("conduccion", "info",
            "Conducción eficiente: RPM bajos a velocidad de crucero. Buen estilo."))
    if avg_maf and avg_maf > _maf_cruise_warn():
        tips.append(("conduccion", "info",
            f"Carga media del motor alta (MAF {avg_maf:.1f} g/s). Revisar presión "
            "de neumáticos y exceso de peso en el vehículo."))
    if max_speed and max_speed > _threshold("speed_fast_kmh", 120):
        tips.append(("conduccion", "warning",
            f"Velocidad máxima de {max_speed:.0f} km/h registrada. Circular a "
            "altas velocidades incrementa el consumo y el desgaste."))

    # Mantenimiento
    if max_temp and max_temp > _threshold("coolant_trip_warn_c", 100):
        tips.append(("mantenimiento", "warning",
            f"Temperatura refrigerante alta ({max_temp:.0f}°C). Revisar nivel "
            "de refrigerante y funcionamiento del termostato/ventilador."))
    if max_rpm and max_rpm > _threshold("rpm_max_warn", 5000):
        tips.append(("mantenimiento", "info",
            f"RPM máximo de {max_rpm:.0f} rpm. Si es frecuente, revisar estado "
            "del aceite y niveles."))

    # Uso del vehículo
    if dur_min and dur_min < _threshold("trip_short_min", 10) and dist < _threshold("trip_short_km", 5):
        tips.append(("uso", "info",
            f"Trayecto corto ({dur_min} min). Los motores necesitan trayectos "
            "más largos para alcanzar temperatura óptima."))
    if dist and dist > _threshold("trip_long_km", 150):
        tips.append(("uso", "info",
            f"Viaje largo ({dist:.0f} km). Descansa cada 2 horas y revisa "
            "presión de neumáticos antes de salir."))

    ts = datetime.now().isoformat()
    for category, severity, message in tips:
        c.execute(
            "INSERT INTO alerts (session_id, timestamp, category, severity, message) "
            "VALUES (?, ?, ?, ?, ?)",
            (session_id, ts, category, severity, message),
        )
    if tips:
        conn.commit()


def session_metrics(conn, c, sid, end_ts=None, start_hint=None):
    """Métricas de un viaje, calculadas desde sus lecturas y posiciones.

    Fuente ÚNICA de cálculo: la usa `main()` al cerrar un viaje activo y
    `recalc_session()` al rehacer uno ya cerrado. Antes vivía dentro de
    `main()`, y por eso un viaje fusionado o con datos recuperados a posteriori
    se quedaba con métricas viejas: no había manera de recomputarlas.

    `end_ts`: fin efectivo (lo decide el cierre por inactividad). None → se
    deriva de la última lectura en movimiento. `start_hint`: start_time de la
    sesión, último recurso si no hay lecturas activas.
    Devuelve None si la sesión no tiene ninguna lectura.
    """
    if conn.row_factory is None:
        conn.row_factory = sqlite3.Row
    c.execute(
        # ⚠️ ORDER BY timestamp, NO por id (fix 2026-09-16): el id refleja el
        # ORDEN DE INSERCIÓN, no el cronológico. Con datos recuperados a
        # posteriori (filas antiguas añadidas después) el orden por id mete
        # saltos falsos en el cálculo de distancia y falsea el inicio del
        # viaje (caso real: 167 arrancaba a las 18:00 cuando empezó a 17:41).
        "SELECT timestamp, lat, lon, gps_speed FROM positions WHERE session_id=? ORDER BY timestamp",
        (sid,),
    )
    positions = [dict(r) for r in c.fetchall()]
    c.execute(
        "SELECT timestamp, rpm, speed, coolant_temp, maf, fuel_rate FROM readings WHERE session_id=? ORDER BY timestamp",
        (sid,),
    )
    readings = [dict(r) for r in c.fetchall()]
    if not readings:
        return None

    # Solo lecturas "activas": motor en marcha (rpm>0) o coche en movimiento.
    # Las de motor apagado (rpm=0, maf~0.6) no cuentan para métricas ni consumo.
    active = [r for r in readings
              if (r["rpm"] or 0) > 0 or (r["speed"] or 0) > MIN_MOVING_SPEED]

    if end_ts is None:
        moves = [r["timestamp"] for r in readings
                 if (r["speed"] or 0) > MIN_MOVING_SPEED]
        end_ts = parse_ts(moves[-1]) if moves else parse_ts(readings[-1]["timestamp"])
    if end_ts is None:
        return None

    # Inicio real: primera lectura activa (no el start_time de la sesión, que
    # puede arrastrar horas de motor apagado del importador).
    if active:
        start = parse_ts(active[0]["timestamp"])
    else:
        start = parse_ts(start_hint) if start_hint else None
        start = start or parse_ts(readings[0]["timestamp"])
    if start is None:
        return None

    # Distancia: sumar solo saltos >= MIN_JUMP_M entre posiciones consecutivas.
    # La deriva GPS de un coche parado genera saltos de 1-10 m que inflan los km.
    dist = 0.0
    prev = None
    for p in positions:
        if p["lat"] is None or p["lon"] is None:
            continue
        if prev:
            d_m = haversine(prev[0], prev[1], p["lat"], p["lon"]) * 1000.0
            if d_m >= MIN_JUMP_M:
                dist += d_m / 1000.0
        prev = (p["lat"], p["lon"])

    dur_min = max(1, int((end_ts - start).total_seconds() / 60))

    speeds = [r["speed"] for r in active if r["speed"] is not None]
    rpms = [r["rpm"] for r in active if r["rpm"] is not None]
    temps = [r["coolant_temp"] for r in active if r["coolant_temp"] is not None]
    max_speed = max(speeds) if speeds else 0.0
    avg_speed = (sum(speeds) / len(speeds)) if speeds else (
        dist / (dur_min / 60.0) if dur_min else 0.0)
    max_rpm = max(rpms) if rpms else 0.0
    max_temp = max(temps) if temps else 0.0

    # ¿Es un viaje real? Señal primaria: velocidad OBD (fiable, no deriva).
    # Si el OBD nunca registró velocidad (coche parado / arranque en frío),
    # no es viaje aunque la deriva GPS acumule km durante horas. Solo si no
    # hay lecturas de velocidad (OBD mudo) se usa la distancia GPS.
    has_speed_data = any(r["speed"] is not None for r in readings)
    if has_speed_data:
        is_trip = max_speed >= MIN_TRIP_MAX_SPEED
    else:
        is_trip = dist >= MIN_TRIP_KM

    # Consumo: CAN del decodificador Witson (L/100km directos, fuente
    # primaria) > fuel_rate OBD 015E (L/h) > MAF/AFR (estimación).
    # v4.8: can_readings trae consumption_l100 del CAN real.
    cons_inst = []
    litros = 0.0
    can_used = False
    try:
        can_rows = conn.execute(
            "SELECT consumption_l100 FROM can_readings "
            "WHERE ts >= ? AND ts <= ? AND consumption_l100 > 0 AND consumption_l100 < 60 "
            "ORDER BY ts",
            (start.isoformat(), end_ts.isoformat())).fetchall()
        if can_rows:
            cons_inst = [r[0] for r in can_rows]
            can_used = True
            litros = (sum(cons_inst) / len(cons_inst)) * dist / 100.0
    except Exception:
        pass

    if not can_used:
        prev_r = None
        for r in active:
            if r.get("fuel_rate") is not None and r["fuel_rate"] > 0:
                # Consumo real de la ECU: L/h → litros en el intervalo
                if prev_r is not None:
                    t1, t2 = parse_ts(prev_r["timestamp"]), parse_ts(r["timestamp"])
                    if t1 and t2:
                        dt_h = (t2 - t1).total_seconds() / 3600.0
                        litros += r["fuel_rate"] * dt_h
                prev_r = r
                if r["speed"] and r["speed"] > 3:
                    l100 = r["fuel_rate"] / r["speed"] * 100.0
                    if 0 < l100 < 60:
                        cons_inst.append(l100)
            elif r["maf"] is not None:
                # Fallback MAF: litros = aire / AFR / densidad
                if prev_r is not None:
                    t1, t2 = parse_ts(prev_r["timestamp"]), parse_ts(r["timestamp"])
                    if t1 and t2:
                        dt_h = (t2 - t1).total_seconds() / 3600.0
                        litros += (r["maf"] / DIESEL_AFR / DENSITY_FUEL) * dt_h
                prev_r = r
                if r["speed"] and r["speed"] > 3:
                    l100 = (r["maf"] / DIESEL_AFR / DENSITY_FUEL) * 3600.0 / r["speed"] * 100.0
                    if 0 < l100 < 60:
                        cons_inst.append(l100)
    cons_medio = (sum(cons_inst) / len(cons_inst)) if cons_inst else None
    if litros <= 0 and cons_medio and dist > 0:
        litros = cons_medio * dist / 100.0

    # Calibración del depósito (una sola lectura para todo el viaje)
    try:
        with open(CONFIG_PATH) as fh:
            vcfg = json.load(fh).get("vehicle", {}) or {}
        km_per_l = float(vcfg.get("range_km_per_l", 17.90))
        reserve_l = float(vcfg.get("reserve_liters", 5.6))
    except Exception:
        km_per_l, reserve_l = 17.90, 5.6

    # Consumo REAL (depósito a depósito). El del cuadro (cons_medio) se
    # guarda igual en consumption_l100, pero no es lo que se enseña: marca
    # aproximadamente la mitad de lo que el coche gasta de verdad.
    real_l100, real_tag = real_consumption(c, end_ts.isoformat(), km_per_l, sid, dist)

    return {
        "start": start, "end_ts": end_ts, "dur_min": dur_min, "dist": dist,
        "positions": positions, "readings": readings, "active": active,
        "max_speed": max_speed, "avg_speed": avg_speed, "max_rpm": max_rpm,
        "max_temp": max_temp, "litros": litros, "cons_medio": cons_medio,
        "can_used": can_used, "real_l100": real_l100, "real_tag": real_tag,
        "km_per_l": km_per_l, "reserve_l": reserve_l, "is_trip": is_trip,
    }


def recalc_session(conn, sid, keep_aggregates=False, c=None):
    """Recalcula métricas y avisos de un viaje YA CERRADO. Idempotente.

    Por qué existe: `merge_sessions.py` une tramos partidos de un mismo viaje y
    solo SUMA distancia/minutos. Sin recalcular, la sesión fusionada queda
    quimérica —velocidad máxima y avisos del primer tramo, con la distancia de
    los dos— y la única forma de rehacerla era devolverla a 'active' y esperar
    al cron, que reenviaba el resumen por Telegram y duplicaba el evento en
    context.db. Este camino recalcula en sitio: no notifica y no toca context.db.

    `keep_aggregates=True` (sesiones fusionadas): NO recomputa distancia ni
    minutos desde los datos. Ya vienen sumados de los tramos, y recomputarlos
    cruzaría el hueco sin lecturas (el salto recto entre el fin de un tramo y el
    inicio del siguiente no son km medidos).

    Los avisos del viaje se borran y se regeneran (replace) para no acumular
    consejos de una versión anterior de los datos.
    """
    if conn.row_factory is None:
        conn.row_factory = sqlite3.Row
    c = c or conn.cursor()
    row = c.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
    if row is None:
        raise ValueError(f"sesión {sid} no existe")
    antes = dict(row)
    m = session_metrics(conn, c, sid, start_hint=antes.get("start_time"))

    if m is None:
        # Sin lecturas: no hay nada que recalcular, pero que no queden avisos.
        c.execute("DELETE FROM alerts WHERE session_id=? AND category IN (?,?,?)",
                  (sid, *TIP_CATEGORIES))
        conn.commit()
        return {"id": sid, "sin_datos": True}

    dist = antes["distance_km"] if keep_aggregates else m["dist"]
    dur_min = antes["driving_minutes"] if keep_aggregates else m["dur_min"]
    dist = dist or 0.0
    dur_min = dur_min or 0

    if not m["is_trip"]:
        c.execute(
            "UPDATE sessions SET status='completed', distance_km=0, max_speed=0, "
            "avg_speed=0, max_rpm=0, driving_minutes=0 WHERE id=?", (sid,))
        c.execute("DELETE FROM alerts WHERE session_id=? AND category IN (?,?,?)",
                  (sid, *TIP_CATEGORIES))
        conn.commit()
        return {"id": sid, "no_viaje": True,
                "antes": {"dist": antes["distance_km"], "max_speed": antes["max_speed"]}}

    c.execute(
        """UPDATE sessions SET end_time=?, status='completed', distance_km=?,
           max_speed=?, avg_speed=?, max_rpm=?, driving_minutes=?,
           fuel_liters=?, consumption_l100=?, real_l100=? WHERE id=?""",
        (m["end_ts"].isoformat(), round(dist, 2), round(m["max_speed"], 1),
         round(m["avg_speed"], 1), round(m["max_rpm"], 1), dur_min,
         round(m["litros"], 2), round(m["cons_medio"], 1) if m["cons_medio"] else None,
         m["real_l100"], sid))
    conn.commit()

    n_avisos_antes = c.execute(
        "SELECT COUNT(*) FROM alerts WHERE session_id=? AND category IN (?,?,?)",
        (sid, *TIP_CATEGORIES)).fetchone()[0]
    generate_tips(conn, c, sid, m["active"], dist, dur_min, replace=True)
    n_avisos = c.execute(
        "SELECT COUNT(*) FROM alerts WHERE session_id=? AND category IN (?,?,?)",
        (sid, *TIP_CATEGORIES)).fetchone()[0]

    return {
        "id": sid, "dist": round(dist, 2), "dur_min": dur_min,
        "max_speed": round(m["max_speed"], 1), "avg_speed": round(m["avg_speed"], 1),
        "max_rpm": round(m["max_rpm"], 1), "real_l100": m["real_l100"],
        "keep_aggregates": keep_aggregates,
        "antes": {"dist": antes["distance_km"], "dur_min": antes["driving_minutes"],
                  "max_speed": antes["max_speed"]},
        "avisos": {"antes": n_avisos_antes, "despues": n_avisos},
    }


def main():
    conn = connect_db(OBD_DB)
    c = conn.cursor()

    c.execute("SELECT * FROM sessions WHERE status='active'")
    sessions = [dict(r) for r in c.fetchall()]
    if not sessions:
        conn.close()
        return

    now = datetime.now()
    reports = []

    for s in sessions:
        sid = s["id"]
        c.execute("SELECT MAX(timestamp) AS t FROM readings WHERE session_id=?", (sid,))
        row = c.fetchone()
        if not row or not row["t"]:
            # Sesión fantasma: activa pero sin ninguna lectura (creada por el
            # importador sin datos reales). Cerrarla limpiamente en vez de
            # dejarla 'active' para siempre — no tiene nada que resumir.
            c.execute(
                "UPDATE sessions SET end_time=COALESCE(start_time,?), status='completed', "
                "distance_km=0, max_speed=0, avg_speed=0, max_rpm=0, driving_minutes=0 "
                "WHERE id=? AND status='active'",
                (datetime.now().isoformat(), sid),
            )
            conn.commit()
            continue
        last_ts = parse_ts(row["t"])
        if not last_ts:
            continue

        # Última lectura con el coche en movimiento (speed > umbral)
        c.execute(
            "SELECT MAX(timestamp) AS t FROM readings WHERE session_id=? AND speed > ?",
            (sid, MIN_MOVING_SPEED),
        )
        mrow = c.fetchone()
        last_move_ts = parse_ts(mrow["t"]) if mrow and mrow["t"] else None

        idle = (now - last_ts).total_seconds() / 60.0
        stopped = (now - last_move_ts).total_seconds() / 60.0 if last_move_ts else idle
        if idle < IDLE_MINUTES and stopped < STOPPED_MINUTES:
            continue  # coche en marcha o parado brevemente

        # ---- cerrar viaje ----
        # fin efectivo: última lectura con movimiento real (si lo hubo), si no la última
        end_ts = last_move_ts or last_ts

        # Métricas del viaje: cálculo compartido con recalc_session() — una sola
        # fuente de verdad. `end_ts` lo fija arriba el cierre por inactividad.
        m = session_metrics(conn, c, sid, end_ts=end_ts, start_hint=s["start_time"])
        if m is None:
            continue
        positions, readings, active = m["positions"], m["readings"], m["active"]
        dist, dur_min, start = m["dist"], m["dur_min"], m["start"]
        max_speed, avg_speed = m["max_speed"], m["avg_speed"]
        max_rpm, max_temp = m["max_rpm"], m["max_temp"]
        is_trip = m["is_trip"]

        if not is_trip:
            c.execute(
                "UPDATE sessions SET end_time=?, status='completed', distance_km=0, "
                "max_speed=0, avg_speed=0, max_rpm=0, driving_minutes=0 WHERE id=?",
                (end_ts.isoformat(), sid),
            )
            conn.commit()
            continue  # silencio: no es un viaje, no hay resumen ni Janus

        litros, cons_medio, can_used = m["litros"], m["cons_medio"], m["can_used"]
        real_l100, real_tag = m["real_l100"], m["real_tag"]
        km_per_l, reserve_l = m["km_per_l"], m["reserve_l"]

        # Actualizar sesión (incluye consumo: litros + media l/100km)
        c.execute(
            """UPDATE sessions SET end_time=?, status='completed', distance_km=?,
               max_speed=?, avg_speed=?, max_rpm=?, driving_minutes=?,
               fuel_liters=?, consumption_l100=?, real_l100=? WHERE id=?""",
            (end_ts.isoformat(), round(dist, 2), round(max_speed, 1),
             round(avg_speed, 1), round(max_rpm, 1), dur_min,
             round(litros, 2), round(cons_medio, 1) if cons_medio else None,
             real_l100, sid),
        )
        conn.commit()

        # Consejos del viaje (conducción / mantenimiento / uso) → tabla alerts
        generate_tips(conn, c, sid, active, dist, dur_min)

        # Texto del resumen
        lines = [
            f"🏁 Viaje terminado — {start.strftime('%d/%m %H:%M')} → {end_ts.strftime('%H:%M')}",
            f"📏 {dist:.1f} km · ⏱️ {dur_min} min · 🚗 media {avg_speed:.0f} km/h · máx {max_speed:.0f} km/h",
        ]
        if max_rpm:
            lines.append(f"🔧 RPM máx {max_rpm:.0f} · temp máx {max_temp:.0f}°C")
        if real_l100:
            # La cifra que vale: consumo real depósito a depósito
            lines.append(f"⛽ real {real_l100:.1f} l/100km "
                         f"(~{real_l100 * dist / 100:.1f} L) — {real_tag}")
        elif cons_medio:
            # Sin datos de depósito: el del cuadro, pero dicho como tal
            tag = "consumo CAN" if can_used else "consumo est."
            lines.append(f"⛽ {tag} {cons_medio:.1f} l/100km (~{litros:.1f} L)")
        # Combustible restante estimado desde el rango CAN (si hay datos)
        try:
            last_range = conn.execute(
                "SELECT range_km FROM can_readings WHERE range_km IS NOT NULL "
                "AND ts <= ? ORDER BY ts DESC LIMIT 1",
                (end_ts.isoformat(),)).fetchone()
            if last_range and last_range["range_km"] is not None and last_range["range_km"] > 0:
                rango = last_range["range_km"]
                litros_rest = rango / km_per_l + reserve_l
                lines.append(f"🛢️ Restante: ~{litros_rest:.0f} L (rango {rango:.0f} km)")
        except Exception:
            pass
        # Origen y destino del viaje. Se guardan en la tabla `trips` de Janus, que
        # es de donde los lee el panel y la consola del salón: antes se escribían
        # a NULL y ahí salía «origen/destino sin determinar».
        first_place = last_place = None
        if positions:
            firstp, lastp = positions[0], positions[-1]
            first_place = nombre_lugar(firstp["lat"], firstp["lon"])
            last_place = nombre_lugar(lastp["lat"], lastp["lon"])
            if first_place:
                lines.append(f"📍 inicio: {first_place}")
            if last_place:
                lines.append(f"📍 fin: {last_place}")
        report = "\n".join(lines)

        # Janus: evento + trip en context.db
        try:
            ctx = connect_db(CTX_DB)
            cc = ctx.cursor()
            detail = json.dumps({
                "km": round(dist, 1), "min": dur_min,
                "avg": round(avg_speed, 0), "max_speed": round(max_speed, 0),
                "consumo": round(cons_medio, 1) if cons_medio else None,
                "inicio": first_place, "fin": last_place,
            }, ensure_ascii=False)
            cc.execute(
                "INSERT INTO events (ts, ts_unix, type, value, detail) VALUES (?,?,?,?,?)",
                (end_ts.isoformat(), int(end_ts.timestamp()), "vehiculo", "viaje", detail),
            )
            cc.execute(
                """INSERT INTO trips (date, start_time, end_time, distance_km, duration_min,
                                      start_place, end_place)
                   VALUES (?,?,?,?,?,?,?)""",
                (start.strftime("%Y-%m-%d"), start.isoformat(), end_ts.isoformat(),
                 round(dist, 2), dur_min, first_place, last_place),
            )
            ctx.commit()
            ctx.close()
        except Exception as e:
            sys.stderr.write(f"ctx insert fail: {e}\n")

        reports.append(report)

    conn.close()
    if reports:
        print("\n\n".join(reports))


def _cli_recalc(ids, keep_aggregates=False):
    """`--recalc <id> [<id>...]`: rehace métricas y avisos de viajes ya cerrados.

    Para cuando los datos llegan o cambian DESPUÉS del cierre (recuperación de
    un fichero entrante, fusión de tramos, corrección de un orden de lectura):
    el camino normal solo recalcula sesiones 'active'.
    """
    conn = connect_db(OBD_DB)
    for sid in ids:
        try:
            r = recalc_session(conn, sid, keep_aggregates=keep_aggregates)
        except ValueError as e:
            print(f"⚠️  {e}")
            continue
        if r.get("sin_datos"):
            print(f"♻️  sesión {sid}: sin lecturas (solo se han limpiado avisos)")
        elif r.get("no_viaje"):
            print(f"♻️  sesión {sid}: no es viaje real → métricas a cero")
        else:
            a = r["antes"]
            print(f"♻️  sesión {sid}: {r['dist']} km · {r['dur_min']} min · "
                  f"máx {r['max_speed']} km/h (antes: {a['dist']} km · {a['dur_min']} min · "
                  f"máx {a['max_speed']} km/h) · avisos {r['avisos']['antes']}→{r['avisos']['despues']}")
    conn.close()


def consolidar_janus(obd_db=None, ctx_db=None, dry_run=True):
    """Deja la tabla `trips` de Janus en espejo de las sesiones reales.

    Por qué hace falta: un viaje **fusionado** deja en `context.db` las filas de
    los tramos absorbidos —el panel y la consola los enseñan como viajes sueltos,
    y entre todas suman los km del fusionado (13/09: 0,86+49,18+78,37+5,06 =
    133,47 km = la sesión 177)—, y un recálculo que cambia el `start_time` deja
    la fila vieja huérfana. Resultado medido: 136 filas frente a 92 sesiones.

    Qué hace, por cada sesión real (`end_time` y `km > 0`):

    - su fila (la que casa por `start_time`) se **actualiza** con los valores de
      la sesión y los lugares;
    - si no tiene fila, se **inserta**;
    - se **borran** las filas contenidas en su intervalo (tramos absorbidos).

    Las filas que no encajan en ninguna sesión **no se borran**: se devuelven en
    `dudosas` para revisarlas (borrar a ciegas es cómo se pierde histórico).

    Con `dry_run=True` no toca nada: solo devuelve el plan.
    """
    con = connect_db(obd_db or OBD_DB)
    ctx = connect_db(ctx_db or CTX_DB)
    cc = ctx.cursor()
    sesiones = con.execute(
        "SELECT id, start_time, end_time, distance_km, driving_minutes FROM sessions "
        "WHERE end_time IS NOT NULL AND distance_km > 0 ORDER BY start_time").fetchall()
    filas = cc.execute("SELECT id, start_time, end_time FROM trips").fetchall()

    plan = {"actualizar": [], "insertar": [], "borrar": [], "dudosas": [], "anidadas": [],
            "eventos_actualizar": [], "eventos_borrar": []}
    usadas = set()
    for s in sesiones:
        # Una sesión DENTRO de otra es una secuela de fusión en la propia BD de
        # telemetría (medido: sesión 48 dentro de la 43). No se le inventa fila:
        # su tramo ya está contado en la de fuera.
        if any(o["id"] != s["id"] and o["start_time"] <= s["start_time"]
               and o["end_time"] >= s["end_time"] for o in sesiones):
            plan["anidadas"].append(s["id"])
            continue
        # La identidad de un viaje es su hora de INICIO: si el fin o los km de la
        # fila están viejos (un recálculo cambió el fin), esa fila SIGUE siendo la
        # del viaje y se actualiza. Exigir además que su fin cayera dentro del
        # intervalo creaba una fila nueva y dejaba la vieja como dudosa (medido:
        # sesión 43, fin recalculado a 12:46:34 con la fila diciendo 12:47:21).
        clave = next((f for f in filas if f["start_time"] == s["start_time"]), None)
        dentro = [f for f in filas
                  if (clave is None or f["id"] != clave["id"])
                  and f["start_time"] >= s["start_time"]
                  and (f["end_time"] or f["start_time"]) <= s["end_time"]]
        if clave:
            plan["actualizar"].append((clave["id"], s))
            usadas.add(clave["id"])
        else:
            plan["insertar"].append(s)
        for f in dentro:
            plan["borrar"].append(f["id"])
        usadas.update(f["id"] for f in dentro)
    plan["dudosas"] = [f["id"] for f in filas if f["id"] not in usadas]

    # Los EVENTOS son el otro espejo del viaje en Janus (los lee la analítica de
    # Janus). El que casa con el fin de una sesión se actualiza con los valores
    # definitivos —el de un tramo absorbido llevaba los del tramo, no los del
    # viaje— y el que no casa con ninguna sesión se borra: es la huella de un
    # tramo que ya no existe.
    fin_de_sesion = {}
    for s in sesiones:
        fin_de_sesion[(s["end_time"] or "")[:19]] = s
    tiene_events = cc.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                              "AND name='events'").fetchone() is not None
    if tiene_events:
        for e in cc.execute("SELECT id, ts, detail FROM events WHERE value='viaje'"):
            s = fin_de_sesion.get(e["ts"][:19])
            if s is None:
                plan["eventos_borrar"].append(e["id"])
            else:
                plan["eventos_actualizar"].append((e["id"], e["detail"], s))

    if not dry_run:
        for tid, s in plan["actualizar"]:
            inicio, fin = lugares_de_sesion(con, s)
            cc.execute("""UPDATE trips SET date=?, end_time=?, distance_km=?, duration_min=?,
                          start_place=?, end_place=? WHERE id=?""",
                       (s["start_time"][:10], s["end_time"], round(s["distance_km"], 2),
                        s["driving_minutes"], inicio, fin, tid))
        for s in plan["insertar"]:
            inicio, fin = lugares_de_sesion(con, s)
            cc.execute("""INSERT INTO trips (date, start_time, end_time, distance_km,
                          duration_min, start_place, end_place) VALUES (?,?,?,?,?,?,?)""",
                       (s["start_time"][:10], s["start_time"], s["end_time"],
                        round(s["distance_km"], 2), s["driving_minutes"], inicio, fin))
        for tid in plan["borrar"]:
            cc.execute("DELETE FROM trips WHERE id=?", (tid,))
        for eid, detail, s in plan["eventos_actualizar"]:
            try:
                d = json.loads(detail) if detail else {}
            except ValueError:
                d = {}
            d["km"] = round(s["distance_km"], 1)
            d["min"] = s["driving_minutes"]
            d["inicio"], d["fin"] = lugares_de_sesion(con, s)
            cc.execute("UPDATE events SET detail=? WHERE id=?",
                       (json.dumps(d, ensure_ascii=False), eid))
        for eid in plan["eventos_borrar"]:
            cc.execute("DELETE FROM events WHERE id=?", (eid,))
        ctx.commit()
    ctx.close()
    con.close()
    return plan


def lugares_de_sesion(con, s):
    """Origen y destino de una sesión: primer y último punto GPS (con caché)."""
    pos = con.execute("SELECT lat, lon FROM positions WHERE session_id=? ORDER BY timestamp",
                      (s["id"],)).fetchall()
    if not pos:
        return None, None
    return nombre_lugar(pos[0]["lat"], pos[0]["lon"]), nombre_lugar(pos[-1]["lat"], pos[-1]["lon"])


def _cli_lugares(limite=None, solo_vacios=True, obd_db=None, ctx_db=None):
    """`--lugares [n]`: rellena origen y destino de los viajes ya escritos en Janus.

    Para el histórico: los viajes cerrados antes de que esto existiera tienen la
    columna a NULL, y en el panel salían como «origen/destino sin determinar».
    Se resuelve el primer y el último punto GPS de cada viaje, con la caché de
    geocodificación delante (casa y trabajo se preguntan UNA vez, no cien).

    Los que no se pueden rellenar se cuentan por su causa, que NO son lo mismo:
    una fila **huérfana** es un fragmento de una fusión vieja (no hay sesión que
    le corresponda y sobra en `trips`); **sin GPS** es un viaje real cuyas
    posiciones no llegaron. Decir «sin GPS» de las huérfanas despista.

    Devuelve (rellenados, huerfanas, sin_gps).
    """
    con = connect_db(obd_db or OBD_DB)
    ctx = connect_db(ctx_db or CTX_DB)
    cc = ctx.cursor()
    sql = ("select id, start_time, start_place, end_place from trips"
           + (" where start_place is null or end_place is null" if solo_vacios else "")
           + " order by date desc, start_time desc")
    if limite:
        sql += " limit %d" % int(limite)
    filas = cc.execute(sql).fetchall()
    rellenos = huerfanas = sin_gps = 0
    for f in filas:
        sid = con.execute("SELECT id FROM sessions WHERE start_time=? LIMIT 1",
                          (f["start_time"],)).fetchone()
        pos = con.execute(
            "SELECT lat, lon FROM positions WHERE session_id=? ORDER BY timestamp",
            (sid["id"],)).fetchall() if sid else []
        if not pos:
            if sid:
                sin_gps += 1
            else:
                huerfanas += 1        # fragmento de fusión vieja: no es un viaje
            continue
        inicio = nombre_lugar(pos[0]["lat"], pos[0]["lon"]) if f["start_place"] is None else f["start_place"]
        fin = nombre_lugar(pos[-1]["lat"], pos[-1]["lon"]) if f["end_place"] is None else f["end_place"]
        cc.execute("UPDATE trips SET start_place=?, end_place=? WHERE id=?",
                   (inicio, fin, f["id"]))
        rellenos += 1
        if rellenos % 10 == 0:
            ctx.commit()
    ctx.commit()
    ctx.close()
    con.close()
    return rellenos, huerfanas, sin_gps


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--recalc":
        args = [a for a in sys.argv[2:] if a != "--keep-aggregates"]
        keep = "--keep-aggregates" in sys.argv[2:]
        ids = [int(x) for x in args]
        if not ids:
            sys.exit("uso: trip_summary.py --recalc <session_id> [<session_id>...] "
                     "[--keep-aggregates]")
        _cli_recalc(ids, keep_aggregates=keep)
    elif len(sys.argv) > 1 and sys.argv[1] == "--consolidar":
        aplicar = "--aplicar" in sys.argv[2:]
        plan = consolidar_janus(dry_run=not aplicar)
        print(f"📋 viajes: {len(plan['actualizar'])} a actualizar · "
              f"{len(plan['insertar'])} a insertar · {len(plan['borrar'])} a borrar · "
              f"{len(plan['dudosas'])} dudosas")
        print(f"📋 eventos: {len(plan['eventos_actualizar'])} a actualizar · "
              f"{len(plan['eventos_borrar'])} a borrar")
        if plan["dudosas"]:
            print(f"⚠️  dudosas (NO se tocan): {plan['dudosas']}")
        if plan["anidadas"]:
            print(f"⚠️  sesiones anidadas (sin fila propia): {plan['anidadas']}")
        print("   (dry-run: no se ha escrito nada; añade --aplicar)" if not aplicar
              else "   ✅ aplicado")
    elif len(sys.argv) > 1 and sys.argv[1] == "--lugares":
        limite = int(sys.argv[2]) if len(sys.argv) > 2 and sys.argv[2].isdigit() else None
        n, huerfanas, sin_gps = _cli_lugares(limite)
        print(f"📍 {n} viajes con origen/destino")
        if huerfanas:
            print(f"⚠️  {huerfanas} filas huérfanas en context.trips (sin sesión: fragmentos de "
                  f"fusiones viejas) — no se tocan, hay que consolidarlas a mano")
        if sin_gps:
            print(f"❓ {sin_gps} viajes reales sin posiciones GPS — no se inventa nada")
    else:
        main()
