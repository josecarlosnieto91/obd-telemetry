#!/usr/bin/env python3
"""
OBD Telemetry WebApp — personal vehicle dashboard accessible via Tailscale.
Displays real-time and historical OBD2 data, alerts, and maintenance tips.
"""
import sqlite3
import json
import os
import sys
from datetime import datetime, timedelta
from flask import Flask, render_template, jsonify, request

app = Flask(__name__)
DB_PATH = os.path.expanduser("~/.hermes/data/obd_telemetry.db")

# car_status.py vive en ~/.hermes/scripts — reutilizamos sus funciones
sys.path.insert(0, os.path.expanduser("~/.hermes/scripts"))
import car_status  # noqa: E402

def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=10.0)
    # Concurrencia: WAL permite 1 escritor + N lectores; busy_timeout hace
    # esperar a escritores concurrentes (crons + webapp threaded) en vez de
    # fallar con 'database is locked'. Sin esto, un POST (ack, litros, servicio)
    # podía chocar con el import/trip_summary y devolver 500.
    conn.execute("PRAGMA busy_timeout=10000")
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.OperationalError:
        pass
    conn.row_factory = sqlite3.Row
    return conn

# ── API Routes ─────────────────────────────────────────────────

@app.route("/api/status")
def api_status():
    """Current vehicle status"""
    conn = get_db()
    c = conn.cursor()
    
    # Latest reading
    c.execute("SELECT * FROM readings ORDER BY id DESC LIMIT 1")
    latest = c.fetchone()
    
    # Active session
    c.execute("SELECT * FROM sessions WHERE status = 'active' LIMIT 1")
    session = c.fetchone()
    
    # Unacknowledged alerts
    c.execute("SELECT COUNT(*) FROM alerts WHERE acknowledged = 0 AND severity IN ('critical', 'warning')")
    alert_count = c.fetchone()[0]

    # DTCs activos (no borrados, recientes) — excluyendo los ignorados en config
    c.execute("""
        SELECT code, description FROM dtc
        WHERE cleared = 0
        ORDER BY timestamp DESC LIMIT 10
    """)
    dtcs_active = [dict(r) for r in c.fetchall()]
    ignored = car_status.ignored_dtcs()
    if ignored:
        dtcs_active = [d for d in dtcs_active if d["code"] not in ignored]

    conn.close()
    
    if latest:
        return jsonify({
            "connected": True,
            "timestamp": latest["timestamp"],
            "rpm": latest["rpm"],
            "speed": latest["speed"],
            "coolant_temp": latest["coolant_temp"],
            "engine_load": latest["engine_load"],
            "intake_temp": latest["intake_temp"],
            "throttle_pos": latest["throttle_pos"],
            "fuel_level": latest["fuel_level"],
            "voltage": latest["voltage"],
            "maf": latest["maf"],
            "map": latest["map"],
            "ambient": latest["ambient"],
            "fuel_pressure": latest["fuel_pressure"],
            "dtc_count": latest["dtc_count"],
            "session_active": session is not None,
            "alert_count": alert_count,
            "dtcs": dtcs_active,
        })
    else:
        return jsonify({"connected": False, "message": "No hay datos aún", "alert_count": 0, "dtcs": []})


@app.route("/api/fap")
def api_fap():
    """Eventos de regeneración FAP detectados (heurística local de la tablet)."""
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM fap_events ORDER BY start_ts DESC LIMIT 50")
    events = [dict(r) for r in c.fetchall()]
    conn.close()
    return jsonify(events)


@app.route("/api/calibration")
def api_calibration():
    """Valores normales aprendidos del vehículo (calibración automática)."""
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM calibration ORDER BY key")
    cal = [dict(r) for r in c.fetchall()]
    conn.close()
    return jsonify(cal)


@app.route("/api/trips")
def api_trips():
    """Trip history + consumo estimado (MAF)."""
    conn = get_db()
    c = conn.cursor()
    c.execute("""
        SELECT id, start_time, end_time, distance_km, max_speed, avg_speed,
               max_rpm, driving_minutes, status
        FROM sessions ORDER BY start_time DESC LIMIT 50
    """)
    trips = [dict(row) for row in c.fetchall()]
    for t in trips:
        # Consumo estimado: l/100km instantáneo con MAF y speed > 3 km/h
        c.execute(
            "SELECT maf, speed FROM readings WHERE session_id=? AND maf IS NOT NULL",
            (t["id"],),
        )
        inst = []
        for r in c.fetchall():
            if r["speed"] and r["speed"] > 3:
                l100 = (r["maf"] / 740.0) * 3600.0 / r["speed"] * 100.0
                if 0 < l100 < 60:
                    inst.append(l100)
        t["consumo_l100"] = round(sum(inst) / len(inst), 1) if inst else None
    conn.close()
    return jsonify(trips)


@app.route("/api/trip/<int:trip_id>")
def api_trip_detail(trip_id):
    """Detailed data for a specific trip"""
    conn = get_db()
    c = conn.cursor()
    
    c.execute("SELECT * FROM sessions WHERE id = ?", (trip_id,))
    trip = dict(c.fetchone())
    
    c.execute("""
        SELECT timestamp, rpm, speed, coolant_temp, engine_load, 
               throttle_pos, fuel_level, voltage
        FROM readings WHERE session_id = ? ORDER BY timestamp
    """, (trip_id,))
    readings = [dict(row) for row in c.fetchall()]
    
    c.execute("SELECT * FROM dtc WHERE session_id = ?", (trip_id,))
    dtcs = [dict(row) for row in c.fetchall()]
    
    c.execute("""
        SELECT * FROM alerts WHERE session_id = ? 
        AND category IN ('conduccion', 'mantenimiento', 'uso')
        ORDER BY id
    """, (trip_id,))
    tips = [dict(row) for row in c.fetchall()]
    
    conn.close()
    return jsonify({"trip": trip, "readings": readings, "dtcs": dtcs, "tips": tips})


@app.route("/api/alerts")
def api_alerts():
    """All alerts"""
    conn = get_db()
    c = conn.cursor()
    severity = request.args.get("severity")
    if severity:
        c.execute("""
            SELECT a.*, s.start_time as trip_start
            FROM alerts a LEFT JOIN sessions s ON a.session_id = s.id
            WHERE a.severity = ? ORDER BY a.timestamp DESC LIMIT 100
        """, (severity,))
    else:
        c.execute("""
            SELECT a.*, s.start_time as trip_start
            FROM alerts a LEFT JOIN sessions s ON a.session_id = s.id
            ORDER BY a.timestamp DESC LIMIT 100
        """)
    alerts = [dict(row) for row in c.fetchall()]
    conn.close()
    return jsonify(alerts)


@app.route("/api/alerts/acknowledge", methods=["POST"])
def api_acknowledge():
    data = request.get_json()
    alert_id = data.get("id")
    conn = get_db()
    conn.execute("UPDATE alerts SET acknowledged = 1 WHERE id = ?", (alert_id,))
    conn.commit()
    conn.close()
    return jsonify({"success": True})


@app.route("/api/stats")
def api_stats():
    """Overall statistics"""
    conn = get_db()
    c = conn.cursor()
    
    c.execute("SELECT COUNT(*), COALESCE(SUM(distance_km), 0), COALESCE(SUM(driving_minutes), 0) FROM sessions WHERE status = 'completed'")
    row = c.fetchone()
    total_trips = row[0] or 0
    total_km = round(row[1] or 0, 1)
    total_minutes = row[2] or 0
    
    # Trips this week
    week_ago = (datetime.now() - timedelta(days=7)).isoformat()
    c.execute("SELECT COUNT(*), COALESCE(SUM(distance_km), 0) FROM sessions WHERE start_time >= ? AND status = 'completed'", (week_ago,))
    row = c.fetchone()
    week_trips = row[0] or 0
    week_km = round(row[1] or 0, 1)
    
    # Max speed ever
    c.execute("SELECT MAX(max_speed) FROM sessions")
    max_speed = c.fetchone()[0] or 0
    
    # Alerts this week
    c.execute("SELECT COUNT(*) FROM alerts WHERE timestamp >= ?", (week_ago,))
    week_alerts = c.fetchone()[0] or 0
    
    conn.close()
    return jsonify({
        "total_trips": total_trips,
        "total_km": total_km,
        "total_minutes": total_minutes,
        "total_hours": round(total_minutes / 60, 1),
        "week_trips": week_trips,
        "week_km": week_km,
        "max_speed": round(max_speed, 1),
        "week_alerts": week_alerts,
    })


@app.route("/api/latest_readings/<int:count>")
def api_latest_readings(count=60):
    """Lecturas para el gráfico del dashboard.

    ?minutes=N → solo lecturas de los últimos N minutos (filtro temporal).
    ?last_trip=1 → lecturas del ÚLTIMO TRAYECTO: sesión activa si existe,
    si no la última sesión cerrada por start_time. (Preferido: el dashboard
    muestra el trayecto real, no un trozo de tiempo arbitrario.)
    """
    minutes = request.args.get("minutes", type=int)
    last_trip = request.args.get("last_trip", type=int)
    conn = get_db()
    c = conn.cursor()

    if last_trip:
        # Última sesión con datos: activa primero, luego la más reciente
        c.execute(
            "SELECT id FROM sessions WHERE status='active' ORDER BY id DESC LIMIT 1")
        row = c.fetchone()
        if not row:
            c.execute(
                "SELECT id FROM sessions WHERE status='completed' "
                "AND EXISTS (SELECT 1 FROM readings r WHERE r.session_id = sessions.id) "
                "ORDER BY start_time DESC LIMIT 1")
            row = c.fetchone()
        if row:
            c.execute(
                "SELECT timestamp, rpm, speed, coolant_temp FROM readings "
                "WHERE session_id=? ORDER BY timestamp", (row["id"],))
            readings = [dict(r) for r in c.fetchall()]
            conn.close()
            return jsonify(readings)
        conn.close()
        return jsonify([])

    if minutes:
        cutoff = (datetime.now() - timedelta(minutes=minutes)).isoformat()
        c.execute(
            "SELECT timestamp, rpm, speed, coolant_temp FROM readings WHERE timestamp >= ? ORDER BY id",
            (cutoff,),
        )
        readings = [dict(row) for row in c.fetchall()]
    else:
        c.execute(
            "SELECT timestamp, rpm, speed, coolant_temp FROM readings ORDER BY id DESC LIMIT ?",
            (count,),
        )
        readings = [dict(row) for row in c.fetchall()]
        readings.reverse()
    conn.close()
    return jsonify(readings)


# ── Page Routes ────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/trips")
def trips():
    return render_template("trips.html")


@app.route("/alerts")
def alerts():
    return render_template("alerts.html")


@app.route("/maintenance")
def maintenance():
    return render_template("maintenance.html")


@app.route("/refuels")
def refuels():
    return render_template("refuels.html")


@app.route("/api/refuels")
def api_refuels():
    """Historial de repostajes detectados (GPS+OSM o salto de nivel)."""
    conn = get_db()
    c = conn.cursor()
    c.execute("""
        SELECT r.id, r.ts, r.prev_ts, r.fuel_before, r.fuel_after, r.jump_pct,
               r.liters, r.full_tank, r.session_id, r.price_per_l, r.cost, r.station, r.source
        FROM refuels r ORDER BY r.ts DESC LIMIT 100
    """)
    rows = [dict(row) for row in c.fetchall()]
    # Total de litros y coste (solo confirmados)
    c.execute("SELECT COALESCE(SUM(liters),0) AS total_l, COALESCE(SUM(cost),0) AS total_cost FROM refuels")
    s = c.fetchone()
    conn.close()
    return jsonify({"refuels": rows, "total_liters": round(s["total_l"], 1), "total_cost": round(s["total_cost"], 2)})


@app.route("/api/refuels/<int:rid>/liters", methods=["POST"])
def api_refuel_liters(rid):
    """Confirma litros de un repostaje detectado por GPS. Recalcula coste con
    el precio de la estación ya guardado (precio del día de la detección)."""
    data = request.get_json(silent=True) or {}
    litros = data.get("liters")
    if not litros or litros <= 0:
        return jsonify({"error": "liters > 0 required"}), 400
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT price_per_l FROM refuels WHERE id=?", (rid,))
    row = c.fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "refuel not found"}), 404
    price = row["price_per_l"]
    cost = round(litros * price, 2) if price else None
    c.execute(
        "UPDATE refuels SET liters=?, cost=? WHERE id=?",
        (round(litros, 1), cost, rid),
    )
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "liters": round(litros, 1), "cost": cost, "price_per_l": price})


@app.route("/api/car_status")
def api_car_status():
    """Estado del coche: odómetro, ITV, mantenimiento programado y diagnósticos."""
    conn = get_db()
    status = {
        "odometer_km": round(car_status.get_odometer(conn), 1),
        "itv": car_status.get_itv(),
        "maintenance": car_status.get_maintenance(conn),
        "diagnostics": car_status.get_diagnostics(conn),
    }
    conn.close()
    return jsonify(status)


@app.route("/api/services", methods=["POST"])
def api_add_service():
    """Registra un servicio de mantenimiento (cambio de aceite, correa, ...).
    Body: {"maintenance_id": "oil", "odometer_km": 210000 (opcional)}.
    Si no se pasa odometer_km, usa el odómetro actual. El campo permite
    anclar servicios hechos en el pasado (p.ej. aceite cambiado hace 5.000 km)
    para que el contador de intervalos sea real desde el km correcto."""
    data = request.get_json(silent=True) or {}
    mid = data.get("maintenance_id")
    if not mid:
        return jsonify({"error": "maintenance_id required"}), 400
    cfg = car_status.load_config()
    valid = {it["id"] for it in cfg.get("maintenance", [])}
    if mid not in valid:
        return jsonify({"error": f"maintenance_id must be one of {sorted(valid)}"}), 400
    conn = get_db()
    c = conn.cursor()
    km = data.get("odometer_km")
    if km is None or km == "":
        km = car_status.get_odometer(conn)
    try:
        km = round(float(km), 1)
    except (TypeError, ValueError):
        conn.close()
        return jsonify({"error": "odometer_km must be a number"}), 400
    ts = datetime.now().isoformat()
    c.execute(
        "INSERT INTO services (maintenance_id, ts, odometer_km) VALUES (?,?,?)",
        (mid, ts, km),
    )
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "maintenance_id": mid, "ts": ts, "odometer_km": km})


# ── Map Routes ────────────────────────────────────────────────

import xml.etree.ElementTree as ET

def parse_gpx(path):
    """Extrae (lat, lon) de un GPX. Devuelve lista de [lat, lon].

    Seguridad: los GPX los genera nuestro propio logger en vehicle tablet
    (input confiable), pero se deshabilitan entidades externas para
    eliminar el riesgo XXE por si algún día llega un archivo externo.
    """
    pts = []
    try:
        parser = ET.XMLParser()
        # Desactivar entidades externas (mitigación XXE)
        try:
            parser.entity = {}
        except Exception:
            pass
        tree = ET.parse(path, parser=parser)
        for trkpt in tree.iter("{http://www.topografix.com/GPX/1/1}trkpt"):
            lat = float(trkpt.get("lat"))
            lon = float(trkpt.get("lon"))
            pts.append([lat, lon])
    except Exception:
        return []
    return pts


@app.route("/map")
def map_view():
    return render_template("map.html")


@app.route("/api/map")
def api_map():
    """Posiciones GPS (+ tracks GPX) para pintar en el mapa.
    ?session_id=N → solo las posiciones de esa sesión (historial por viaje).
    ?date=YYYY-MM-DD → solo posiciones de ese día (selector de calendario)."""
    session_id = request.args.get("session_id", type=int)
    date = request.args.get("date")
    conn = get_db()
    c = conn.cursor()
    if session_id:
        c.execute(
            "SELECT timestamp, lat, lon, gps_speed FROM positions WHERE session_id=? ORDER BY id",
            (session_id,),
        )
    elif date:
        c.execute(
            "SELECT timestamp, lat, lon, gps_speed FROM positions WHERE timestamp LIKE ? ORDER BY id",
            (date + "%",),
        )
    else:
        c.execute(
            "SELECT timestamp, lat, lon, gps_speed FROM positions ORDER BY id"
        )
    positions = [dict(r) for r in c.fetchall()]
    conn.close()

    tracks = []
    tracks_dir = os.path.expanduser("~/.hermes/data/tracks")
    if os.path.isdir(tracks_dir):
        for f in sorted(os.listdir(tracks_dir)):
            if f.endswith(".gpx"):
                pts = parse_gpx(os.path.join(tracks_dir, f))
                if pts:
                    tracks.append({"name": f, "points": pts})

    return jsonify({"positions": positions, "tracks": tracks})


if __name__ == "__main__":
    import socket
    # Only listen on Tailscale interface by default
    host = "0.0.0.0"
    port = int(os.environ.get("OBD_WEB_PORT", 8765))
    print(f"🚗 OBD WebApp arrancando en http://{host}:{port}")
    print(f"📡 Accesible via Tailscale: http://100.XX.XX.XX:{port}")
    app.run(host=host, port=port, debug=False)
