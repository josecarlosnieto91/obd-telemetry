#!/usr/bin/env python3
"""Resumen de viaje vehicle tablet — cierra sesiones OBD2 terminadas y emite resumen.

Patrón cron no_agent:
  - stdout VACÍO  → silencio (no hay viaje terminado, no se entrega nada)
  - stdout TEXTO  → resumen entregado por Telegram

Además inserta el viaje en el sistema de contexto (context.db) para Janus:
  - evento type='vehiculo' value='viaje' (detail = JSON con métricas)
  - fila en tabla trips
"""
import sqlite3, os, sys, math, json, urllib.request, urllib.parse
from datetime import datetime

OBD_DB = os.path.expanduser("~/.hermes/data/obd_telemetry.db")
CTX_DB = os.path.expanduser("~/.hermes/data/context/context.db")
CONFIG_PATH = os.path.expanduser("~/.hermes/scripts/obd_vehicle_config.json")
IDLE_MINUTES = 5      # sin lecturas durante 5 min => bridge apagado, viaje terminado
STOPPED_MINUTES = 10  # coche parado (speed < MIN_MOVING_SPEED) durante 10 min => viaje terminado
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


DENSITY_FUEL = _fuel_density()  # g/L — usado en el cálculo de consumo MAF


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
    return conn


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


def generate_tips(conn, c, session_id, readings, dist, dur_min):
    """Genera consejos de conducción/mantenimiento/uso tras cerrar un viaje
    y los inserta en la tabla alerts (los consume la webapp /maintenance).
    Portado desde obd_collector.py (el recolector TCP live está pausado)."""
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
    if avg_rpm and avg_rpm > 3000:
        tips.append(("conduccion", "info",
            f"RPM medios altos ({avg_rpm:.0f} rpm). Para ahorrar combustible, "
            "cambia a una marcha superior cuando el motor supere las 2500 rpm."))
    if avg_rpm and avg_rpm < 1500 and avg_speed and avg_speed > 60:
        tips.append(("conduccion", "info",
            "Conducción eficiente: RPM bajos a velocidad de crucero. Buen estilo."))
    if avg_maf and avg_maf > 25.0:
        tips.append(("conduccion", "info",
            f"Carga media del motor alta (MAF {avg_maf:.1f} g/s). Revisar presión "
            "de neumáticos y exceso de peso en el vehículo."))
    if max_speed and max_speed > 120:
        tips.append(("conduccion", "warning",
            f"Velocidad máxima de {max_speed:.0f} km/h registrada. Circular a "
            "altas velocidades incrementa el consumo y el desgaste."))

    # Mantenimiento
    if max_temp and max_temp > 100:
        tips.append(("mantenimiento", "warning",
            f"Temperatura refrigerante alta ({max_temp:.0f}°C). Revisar nivel "
            "de refrigerante y funcionamiento del termostato/ventilador."))
    if max_rpm and max_rpm > 5000:
        tips.append(("mantenimiento", "info",
            f"RPM máximo de {max_rpm:.0f} rpm. Si es frecuente, revisar estado "
            "del aceite y niveles."))

    # Uso del vehículo
    if dur_min and dur_min < 10 and dist < 5:
        tips.append(("uso", "info",
            f"Trayecto corto ({dur_min} min). Los motores necesitan trayectos "
            "más largos para alcanzar temperatura óptima."))
    if dist and dist > 150:
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

        c.execute(
            "SELECT timestamp, lat, lon, gps_speed FROM positions WHERE session_id=? ORDER BY id",
            (sid,),
        )
        positions = [dict(r) for r in c.fetchall()]
        c.execute(
            "SELECT timestamp, rpm, speed, coolant_temp, maf FROM readings WHERE session_id=? ORDER BY id",
            (sid,),
        )
        readings = [dict(r) for r in c.fetchall()]

        # Solo lecturas "activas": motor en marcha (rpm>0) o coche en movimiento.
        # Las de motor apagado (rpm=0, maf~0.6) no cuentan para métricas ni consumo.
        active = [r for r in readings
                  if (r["rpm"] or 0) > 0 or (r["speed"] or 0) > MIN_MOVING_SPEED]

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

        # Inicio real: primera lectura activa (no el start_time de la sesión, que
        # puede arrastrar horas de motor apagado del importador).
        start = parse_ts(s["start_time"]) or last_ts
        if active:
            start = parse_ts(active[0]["timestamp"]) or start
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

        if not is_trip:
            c.execute(
                "UPDATE sessions SET end_time=?, status='completed', distance_km=0, "
                "max_speed=0, avg_speed=0, max_rpm=0, driving_minutes=0 WHERE id=?",
                (end_ts.isoformat(), sid),
            )
            conn.commit()
            continue  # silencio: no es un viaje, no hay resumen ni Janus

        # Consumo estimado: MAF (g/s) → l/100km instantáneo cuando speed > 3
        cons_inst = []
        litros = 0.0
        prev_r = None
        for r in active:
            if r["maf"] is not None:
                if prev_r is not None:
                    t1, t2 = parse_ts(prev_r["timestamp"]), parse_ts(r["timestamp"])
                    if t1 and t2:
                        dt_h = (t2 - t1).total_seconds() / 3600.0
                        litros += (r["maf"] / DENSITY_FUEL) * dt_h
                prev_r = r
                if r["speed"] and r["speed"] > 3:
                    l100 = (r["maf"] / DENSITY_FUEL) * 3600.0 / r["speed"] * 100.0
                    if 0 < l100 < 60:
                        cons_inst.append(l100)
        cons_medio = (sum(cons_inst) / len(cons_inst)) if cons_inst else None
        if litros <= 0 and cons_medio and dist > 0:
            litros = cons_medio * dist / 100.0

        # Actualizar sesión
        c.execute(
            """UPDATE sessions SET end_time=?, status='completed', distance_km=?,
               max_speed=?, avg_speed=?, max_rpm=?, driving_minutes=? WHERE id=?""",
            (end_ts.isoformat(), round(dist, 2), round(max_speed, 1),
             round(avg_speed, 1), round(max_rpm, 1), dur_min, sid),
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
        if cons_medio:
            lines.append(f"⛽ consumo estimado {cons_medio:.1f} l/100km (~{litros:.1f} L)")
        if positions:
            lastp = positions[-1]
            lugar = reverse_geocode(lastp["lat"], lastp["lon"])
            if lugar:
                lines.append(f"📍 fin: {lugar}")
            else:
                lines.append(f"📍 fin: {lastp['lat']:.4f},{lastp['lon']:.4f}")
            last_place = lugar
        else:
            last_place = None
        report = "\n".join(lines)

        # Janus: evento + trip en context.db
        try:
            ctx = connect_db(CTX_DB)
            cc = ctx.cursor()
            detail = json.dumps({
                "km": round(dist, 1), "min": dur_min,
                "avg": round(avg_speed, 0), "max_speed": round(max_speed, 0),
                "consumo": round(cons_medio, 1) if cons_medio else None,
                "fin": last_place,
            }, ensure_ascii=False)
            cc.execute(
                "INSERT INTO events (ts, ts_unix, type, value, detail) VALUES (?,?,?,?,?)",
                (end_ts.isoformat(), int(end_ts.timestamp()), "vehiculo", "viaje", detail),
            )
            cc.execute(
                """INSERT INTO trips (date, start_time, end_time, distance_km, duration_min)
                   VALUES (?,?,?,?,?)""",
                (start.strftime("%Y-%m-%d"), start.isoformat(), end_ts.isoformat(),
                 round(dist, 2), dur_min),
            )
            ctx.commit()
            ctx.close()
        except Exception as e:
            sys.stderr.write(f"ctx insert fail: {e}\n")

        reports.append(report)

    conn.close()
    if reports:
        print("\n\n".join(reports))


if __name__ == "__main__":
    main()
