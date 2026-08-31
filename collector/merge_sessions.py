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

OBD_DB = os.path.expanduser("~/.hermes/data/obd_telemetry.db")
CONFIG_PATH = os.path.expanduser("~/.hermes/scripts/obd_vehicle_config.json")

MERGE_GAP_MINUTES = 15.0
MAX_GEO_KM = 2.0


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
    """Posición GPS más cercana en tiempo a ts (ventana ± window_min)."""
    try:
        row = conn.execute(
            "SELECT lat, lon FROM positions "
            "WHERE timestamp BETWEEN ? AND ? "
            "ORDER BY ABS(julianday(timestamp) - julianday(?)) LIMIT 1",
            (ts, ts, ts)).fetchone()
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

    conn = sqlite3.connect(OBD_DB)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    merged = 0
    changed = True
    while changed:
        changed = False
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
            # Geografía: fin de A y inicio de B cerca (si hay posiciones)
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
            c.execute("DELETE FROM sessions WHERE id=?", (drop["id"],))
            conn.commit()
            print(f"🔄 Fusionadas {keep['id']} ← {drop['id']} "
                  f"(gap {gap:.1f} min, {n_read} lecturas movidas)")
            merged += 1
            changed = True
            break  # reiniciar: los ids pueden haber cambiado

    conn.close()
    print(f"✅ Unión de viajes: {merged} fusiones" if merged else "✅ Sin sesiones que unir")


if __name__ == "__main__":
    main()
