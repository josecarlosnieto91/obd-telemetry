#!/usr/bin/env python3
"""Une sesiones consecutivas que son tramos del mismo viaje.

PROBLEMA: el importador crea una sesión nueva cuando el sync llega con un
hueco (la tablet pierde red, el recolector se reinicia, fichero corrupto).
El gap REAL de lecturas puede ser de 30 s, pero la sesión se parte.

CRITERIO: dos sesiones consecutivas (por start_time) se fusionan si el hueco
entre end_time de la primera y start_time de la segunda es < MERGE_GAP_MINUTES
(15 min, coherente con IDLE_MINUTES) y la distancia entre el fin de la primera
y el inicio de la segunda es < MAX_GEO_KM (2 km) cuando hay posiciones; sin
posiciones, el gap corto es señal suficiente.

La fusión: readings/positions/alerts/dtc apuntan a la sesión mayor (la más
antigua), se suman distancia/minutos, y se borra la sesión absorbida.
Idempotente: tras fusionar, ya no hay sesiones con gap corto.
"""
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta

# `trip_summary` es la fuente única del cálculo de métricas: la fusión mueve
# datos entre sesiones y necesita recalcular con las mismas reglas que el cierre
# de un viaje (no puede haber dos fórmulas).
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from trip_summary import recalc_session, consolidar_janus  # noqa: E402

OBD_DB = os.path.expanduser("~/.hermes/data/obd_telemetry.db")
CONFIG_PATH = os.path.expanduser("~/.hermes/scripts/obd_vehicle_config.json")

MERGE_GAP_MINUTES = 15.0
MAX_GEO_KM = 2.0

# Tabla de deshacer. La fusión BORRA la sesión absorbida y no hay vuelta atrás, pero
# las heurísticas (hueco + continuidad GPS + geografía) pueden equivocarse — este mismo
# fichero documenta el caso real de "22 km a 120 km/h en 11 min" y los pares fantasma
# 152/160. Un falso positivo se llevaba por delante un viaje real sin dejar rastro y el
# job corre cada 5 minutos, así que la fila se guarda ENTERA aquí antes de borrarla:
# reconstruirla es un INSERT del payload.
UNDO_TABLE = "sessions_merged_undo"
UNDO_RETENTION_DAYS = 90
MAX_MERGE_PASSES = 50  # tope del bucle de fusiones (no puede girar sin fin)


def load_config():
    try:
        with open(CONFIG_PATH) as fh:
            return json.load(fh)
    except Exception:
        return {}


def haversine_km(lat1, lon1, lat2, lon2):
    from math import radians, sin, cos, sqrt, atan2
    R = 6371.0
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = (sin(dlat / 2) ** 2
         + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2)
    return R * 2 * atan2(sqrt(a), sqrt(1 - a))


def position_near(conn, ts, window_min=5):
    """Posición GPS más cercana en tiempo a ts (ventana ± window_min).
    FIX 2026-08-26: antes usaba BETWEEN (ts, ts) = rango CERO → solo
    encontraba timestamps exactos; además una posición GPS espuria del
    arranque en frío (22 km en 11 min, imposible) bloqueaba fusiones
    legítimas. Ahora: ventana real ±window_min; si no hay posiciones
    fiables devuelve None (la fusión no se bloquea: el gap corto es
    señal suficiente)."""
    try:
        from datetime import timedelta
        t = datetime.fromisoformat(ts)
        lo = (t - timedelta(minutes=window_min)).isoformat()
        hi = (t + timedelta(minutes=window_min)).isoformat()
        row = conn.execute(
            "SELECT lat, lon FROM positions "
            "WHERE timestamp BETWEEN ? AND ? "
            "ORDER BY ABS(julianday(timestamp) - julianday(?)) LIMIT 1",
            (lo, hi, ts)).fetchone()
    except Exception:
        return None
    if not row:
        return None
    try:
        return (row[0], row[1])
    except Exception:
        return None


def main():
    cfg = load_config()
    thr = cfg.get("thresholds", {}) or {}
    gap_min = float(thr.get("trip_merge_gap_min", MERGE_GAP_MINUTES))
    geo_km = float(thr.get("trip_merge_geo_km", MAX_GEO_KM))

    # Las otras piezas del collector (trip_summary, obd_local_import, car_status) abren la
    # BD con busy_timeout + WAL. Sin esto, con 5-6 jobs escribiendo cada 5 minutos este
    # podía saltar con "database is locked" a mitad del bucle de fusiones.
    conn = sqlite3.connect(OBD_DB, timeout=10.0)
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    # Tabla de deshacer + su retención (ver UNDO_TABLE arriba)
    c.execute(f"""CREATE TABLE IF NOT EXISTS {UNDO_TABLE} (
        ts TEXT NOT NULL,
        dropped_id INTEGER NOT NULL,
        kept_id INTEGER NOT NULL,
        gap_min REAL,
        n_readings INTEGER,
        payload TEXT NOT NULL)""")
    c.execute(f"DELETE FROM {UNDO_TABLE} WHERE ts < ?",
              ((datetime.now() - timedelta(days=UNDO_RETENTION_DAYS)).isoformat(timespec="seconds"),))
    conn.commit()

    merged = 0
    changed = True
    passes = 0
    while changed and passes < MAX_MERGE_PASSES:
        changed = False
        passes += 1
        # Sesiones completadas ordenadas por start_time
        rows = c.execute(
            "SELECT id, start_time, end_time, distance_km, driving_minutes "
            "FROM sessions WHERE status='completed' AND end_time IS NOT NULL "
            "ORDER BY start_time").fetchall()
        for a, b in zip(rows, rows[1:]):
            try:
                gap = (datetime.fromisoformat(b["start_time"])
                       - datetime.fromisoformat(a["end_time"])).total_seconds() / 60.0
            except Exception:
                continue
            if gap < 0 or gap > gap_min:
                continue
            # Señal de continuidad: si el GPS no tiene hueco entre el fin de A y
            # el inicio de B (intervalo < 3 min entre posiciones), el coche NO
            # se detuvo → mismo viaje (el OBD se interrumpió, no el coche).
            # FIX 2026-08-26: la comparación geográfica fin/inicio fallaba
            # cuando el coche avanzaba (posición del fin lejos del inicio
            # aunque el GPS fuera continuo: 22 km a 120 km/h en 11 min).
            gps_cont = False
            try:
                row = conn.execute(
                    "SELECT MAX(ROUND((julianday((SELECT MIN(timestamp) FROM positions p2 "
                    "WHERE p2.timestamp > p.timestamp))-julianday(p.timestamp))*86400,0)) "
                    "FROM positions p WHERE p.timestamp BETWEEN ? AND ?",
                    (a["end_time"], b["start_time"])).fetchone()
                if row and row[0] is not None and float(row[0]) < 180:
                    gps_cont = True
            except Exception:
                gps_cont = False
            if gps_cont:
                pass  # mismo viaje: el coche no se detuvo
            else:
                # Sin GPS continuo: comprobar geografía fin/inicio
                pa = position_near(conn, a["end_time"])
                pb = position_near(conn, b["start_time"])
                if pa and pb:
                    d = haversine_km(pa[0], pa[1], pb[0], pb[1])
                    if d > geo_km:
                        continue  # lejos: viajes distintos (p.ej. vuelta a casa)
            # Fusión: conservar la sesión más antigua (a), absorber b
            keep, drop = a, b
            n_read = c.execute(
                "UPDATE readings SET session_id=? WHERE session_id=?",
                (keep["id"], drop["id"])).rowcount
            c.execute(
                "UPDATE positions SET session_id=? WHERE session_id=?",
                (keep["id"], drop["id"]))
            c.execute(
                "UPDATE alerts SET session_id=? WHERE session_id=?",
                (keep["id"], drop["id"]))
            c.execute(
                "UPDATE dtc SET session_id=? WHERE session_id=?",
                (keep["id"], drop["id"]))
            c.execute(
                "UPDATE can_readings SET session_id=? WHERE session_id=?",
                (keep["id"], drop["id"]))
            # Sumar métricas
            dist = (keep["distance_km"] or 0) + (drop["distance_km"] or 0)
            mins = (keep["driving_minutes"] or 0) + (drop["driving_minutes"] or 0)
            c.execute(
                "UPDATE sessions SET end_time=?, distance_km=?, driving_minutes=? "
                "WHERE id=?",
                (drop["end_time"], round(dist, 2), mins, keep["id"]))
            # Copia de deshacer ANTES del borrado (ver UNDO_TABLE): si esta fusión es
            # un falso positivo, la fila original queda recuperable.
            fila = c.execute("SELECT * FROM sessions WHERE id=?",
                             (drop["id"],)).fetchone()
            if fila is not None:
                c.execute(
                    f"INSERT INTO {UNDO_TABLE} "
                    "(ts, dropped_id, kept_id, gap_min, n_readings, payload) "
                    "VALUES (?,?,?,?,?,?)",
                    (datetime.now().isoformat(timespec="seconds"), drop["id"],
                     keep["id"], gap, n_read,
                     json.dumps(dict(fila), ensure_ascii=False, default=str)))
            c.execute("DELETE FROM sessions WHERE id=?", (drop["id"],))
            conn.commit()
            # La fusión mueve los datos, pero NO recalcula: sin esto la sesión
            # fusionada se quedaba con la velocidad máxima, la media y los avisos
            # del primer tramo (caso real 12-sep: máx 42 km/h con lecturas de 127
            # y el «Trayecto corto (1 min)» del tramo viejo).
            # keep_aggregates: distancia y minutos ya vienen sumados arriba y
            # recomputarlos cruzaría el hueco sin lecturas.
            info = recalc_session(conn, keep["id"], keep_aggregates=True)
            print(f"🔄 Fusionadas {keep['id']} ← {drop['id']} "
                  f"(gap {gap:.1f} min, {n_read} lecturas movidas) → "
                  f"{info.get('dist')} km · {info.get('dur_min')} min · "
                  f"máx {info.get('max_speed')} km/h")
            merged += 1
            changed = True
            break  # reiniciar: los ids pueden haber cambiado

    conn.close()
    print(f"✅ Unión de viajes: {merged} fusiones" if merged else "✅ Sin sesiones que unir")

    # La fusión deja en Janus las filas de los tramos absorbidos, y el panel y la
    # consola los enseñaban como viajes sueltos (13/09: cuatro filas de 0,86 /
    # 49,18 / 78,37 / 5,06 km en vez de una de 133,5). Se consolidan aquí, que es
    # justo cuando aparecen. Un fallo al consolidar NO debe tumbar la fusión.
    if merged:
        try:
            plan = consolidar_janus()
            print(f"🧹 Janus consolidado: {len(plan['actualizar'])} filas al día · "
                  f"{len(plan['borrar'])} tramos absorbidos fuera · "
                  f"{len(plan['insertar'])} filas nuevas")
            if plan["dudosas"]:
                print(f"⚠️  filas dudosas en context.trips (no se tocan): {plan['dudosas']}")
            if plan["anidadas"]:
                print(f"⚠️  sesiones anidadas en telemetría (no se les inventa fila): "
                      f"{plan['anidadas']}")
        except Exception as e:
            print(f"⚠️  no se pudo consolidar Janus: {e}")


if __name__ == "__main__":
    main()
