#!/usr/bin/env python3
"""
OBD Telemetry WebApp — personal vehicle dashboard accessible via Tailscale.
Displays real-time and historical OBD2 data, alerts, and maintenance tips.
"""
import sqlite3
import json
import os
import sys
import gzip
import hashlib
from datetime import datetime, timedelta
from flask import Flask, render_template, jsonify, request, Response

app = Flask(__name__)


@app.context_processor
def _static_version():
    """Sella los estáticos con la fecha del más reciente de la carpeta.

    Sin esto el navegador sirve la copia cacheada y un cambio de CSS «no se
    aplica»: se diagnostica como si la regla no existiera cuando lo que pasa es
    que no ha llegado. Con ?v=<mtime> cada edición invalida la caché sola.
    Cubre style.css, nav.js y chart.umd.min.js (Chart.js local, ya sin CDN).
    """
    carpeta = os.path.join(app.root_path, "static")
    try:
        v = max(int(os.path.getmtime(os.path.join(carpeta, f)))
                for f in ("style.css", "nav.js", "chart.umd.min.js")
                if os.path.exists(os.path.join(carpeta, f)))
    except (OSError, ValueError):
        v = 0
    return {"static_v": v, "api_token": _api_token()}
DB_PATH = os.path.expanduser("~/.hermes/data/obd_telemetry.db")
CONFIG_PATH = os.path.expanduser("~/.hermes/scripts/obd_vehicle_config.json")


def _config():
    """Config del vehículo (la operativa, no la copia del repo)."""
    try:
        with open(CONFIG_PATH) as fh:
            return json.load(fh)
    except Exception:
        return {}


def _api_token():
    """Token exigido a los POST (`webapp.api_token`). Vacío = sin exigencia."""
    return ((_config().get("webapp") or {}).get("api_token") or "").strip()


def _autorizado():
    """True si el POST puede ejecutarse.

    Los POST cambian datos del coche (marcar DTCs, reconocer alertas, apuntar
    litros) y la app escucha en la tailnet sin más protección: un token
    compartido en cabecera es el mínimo que evita que cualquier dispositivo de
    la red marque como borrado un fallo real. Sin token configurado no se exige
    (compatibilidad), pero se avisa una vez en el log.
    """
    esperado = _api_token()
    if not esperado:
        return True
    recibido = request.headers.get("X-OBD-Token", "")
    return recibido == esperado


def _ensure_auditoria_dtc():
    """Columnas de auditoría en `dtc`: quién y cuándo marcó un código como borrado.

    Sin esto no hay forma de saber por qué P0113 apareció marcado `cleared=1`
    el 2026-10-28 ni de distinguir "lo borró el usuario" de "lo ocultó una
    herramienta". Idempotente y aditivo.
    """
    try:
        conn = sqlite3.connect(DB_PATH, timeout=10.0)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(dtc)")}
        for col, tipo in (("cleared_ts", "TEXT"), ("cleared_by", "TEXT")):
            if col not in cols:
                conn.execute(f"ALTER TABLE dtc ADD COLUMN {col} {tipo}")
        conn.commit()
        conn.close()
    except sqlite3.Error as e:
        sys.stderr.write(f"[webapp] auditoría dtc: {e}\n")


_ensure_auditoria_dtc()


# car_status.py vive en ../collector (una sola copia en el repo). Antes apuntaba
# a ~/.hermes/scripts, donde solo queda el wrapper .sh del cron → el servicio
# arrancaba roto con ModuleNotFoundError (latente desde el refactor del 10/09).
sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "collector"))
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


# ── Comodidades de servicio ────────────────────────────────────

FAVICON_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
    '<rect width="64" height="64" rx="12" fill="#111"/>'
    '<text x="32" y="45" font-size="38" text-anchor="middle">'
    '\U0001F697</text></svg>'
).encode("utf-8")


@app.route("/favicon.ico")
def favicon():
    """Sin esto el navegador pedía /favicon.ico en cada carga y recibía 404."""
    r = Response(FAVICON_SVG, mimetype="image/svg+xml")
    r.headers["Cache-Control"] = "public, max-age=604800"
    return r


@app.route("/health")
def health():
    """Sonda barata para el launcher y para el despliegue: no toca los GPX."""
    try:
        conn = get_db()
        row = conn.execute("SELECT MAX(timestamp) FROM readings").fetchone()
        conn.close()
        ultima = row[0] if row else None
        return jsonify({"ok": True, "db": DB_PATH, "ultima_lectura": ultima})
    except Exception as e:  # BD ausente o ilegible: se dice, no se finge
        return jsonify({"ok": False, "error": str(e)}), 503


@app.after_request
def _comprimir(resp):
    """gzip en las respuestas grandes y ETag para no repetir lo que no cambió.

    /api/map devolvía 1 MB por la tailnet sin comprimir (medido 2026-10-28) y
    el mapa se abre desde el móvil. `send_file` (los estáticos) va en modo
    direct_passthrough y no se toca.
    """
    if resp.direct_passthrough or resp.status_code >= 300:
        return resp
    if resp.mimetype not in ("application/json", "text/html", "text/css",
                             "application/javascript", "image/svg+xml"):
        return resp
    if request.accept_encodings.find("gzip") >= 0 and not (
            resp.content_length is not None and resp.content_length < 800):
        datos = resp.get_data()
        comprimido = gzip.compress(datos, 6)
        if len(comprimido) < len(datos):
            resp.set_data(comprimido)
            resp.headers["Content-Encoding"] = "gzip"
            resp.headers["Content-Length"] = str(len(comprimido))
            resp.headers.add("Vary", "Accept-Encoding")
    # ETag del cuerpo ya servido: el dashboard y las APIs se sondean cada 5 s y
    # la mayoría de las veces el contenido no ha cambiado (304 = 0 bytes).
    cuerpo = resp.get_data()
    if cuerpo:
        etag = '"' + hashlib.md5(cuerpo).hexdigest() + '"'
        resp.headers.setdefault("ETag", etag)
        if request.headers.get("If-None-Match") == resp.headers.get("ETag"):
            resp.status_code = 304
            resp.set_data(b"")
    return resp

# ── API Routes ─────────────────────────────────────────────────

@app.route("/api/status")
def api_status():
    """Current vehicle status

    Incluye `frescura` (cuánto hace de la última lectura, con su estado) y
    `pids_no_soportados`: antes el dashboard pintaba valores de hace semanas
    como si fueran de ahora y devolvía `null` para PIDs que este coche no
    expone nunca, indistinguible de un fallo de lectura.
    """
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
    for d in dtcs_active:
        d["sistema"] = _dtc_sistema(d.get("code"))
    ignored = car_status.ignored_dtcs()
    if ignored:
        dtcs_active = [d for d in dtcs_active if d["code"] not in ignored]

    # Combustible restante estimado desde el rango CAN (el C4 no expone
    # fuel_level por OBD; la señal fiable es el rango del decodificador)
    fuel_remaining = None
    fuel_range = None
    if latest:
        try:
            cfg = car_status.load_config()
            vcfg = cfg.get("vehicle", {}) or {}
            km_per_l = float(vcfg.get("range_km_per_l", 17.90))
            reserve_l = float(vcfg.get("reserve_liters", 5.6))
            row = c.execute(
                "SELECT range_km FROM can_readings WHERE range_km IS NOT NULL "
                "ORDER BY ts DESC LIMIT 1").fetchone()
            if row and row["range_km"] is not None and row["range_km"] > 0:
                fuel_range = row["range_km"]
                fuel_remaining = round(fuel_range / km_per_l + reserve_l, 1)
        except Exception as e:
            import sys
            print(f"[webapp] fuel_remaining error: {e}", file=sys.stderr)

    # Frescura del dato: sin esto el dashboard pinta valores de hace semanas
    # con el mismo aspecto que los de ahora mismo.
    frescura = None
    if latest:
        try:
            t = datetime.fromisoformat(str(latest["timestamp"]))
            minutos = (datetime.now() - t).total_seconds() / 60.0
            if minutos < 5:
                estado, etiqueta = "fresco", "ahora mismo"
            elif minutos < 60:
                estado, etiqueta = "fresco", f"hace {minutos:.0f} min"
            elif minutos < 1440:
                estado, etiqueta = "reciente", f"hace {minutos/60:.0f} h"
            elif minutos < 1440 * 7:
                estado, etiqueta = "viejo", f"hace {minutos/1440:.0f} días"
            else:
                estado, etiqueta = "obsoleto", f"hace {minutos/1440:.0f} días"
            frescura = {"minutos": round(minutos, 1), "estado": estado,
                        "etiqueta": etiqueta, "ts": latest["timestamp"]}
        except Exception:
            frescura = {"minutos": None, "estado": "desconocido", "etiqueta": "sin fecha", "ts": None}

    cfg = _config()
    vcfg = cfg.get("vehicle", {}) or {}
    no_soportados = list(vcfg.get("unsupported_pids") or [])

    conn.close()

    if latest:
        datos = {
            "connected": True,
            "timestamp": latest["timestamp"],
            "rpm": latest["rpm"],
            "speed": latest["speed"],
            "coolant_temp": latest["coolant_temp"],
            "engine_load": latest["engine_load"],
            "intake_temp": latest["intake_temp"],
            "throttle_pos": latest["throttle_pos"],
            "fuel_level": latest["fuel_level"],
            "fuel_remaining_l": fuel_remaining,
            "fuel_range_km": fuel_range,
            "voltage": latest["voltage"],
            "maf": latest["maf"],
            "map": latest["map"],
            "ambient": latest["ambient"],
            "fuel_pressure": latest["fuel_pressure"],
            "dtc_count": latest["dtc_count"],
            "session_active": session is not None,
            "alert_count": alert_count,
            "dtcs": dtcs_active,
            "frescura": frescura,
            # PIDs que ESTE coche no expone: el cliente no debe pintarlos como
            # «—» (que se confunde con un fallo de lectura).
            "pids_no_soportados": no_soportados,
        }
        return jsonify(datos)
    else:
        return jsonify({"connected": False, "message": "No hay datos aún", "alert_count": 0,
                        "dtcs": [], "frescura": None, "pids_no_soportados": no_soportados})


def _dtc_sistema(code):
    """Sistema del código según SAE J2012 (primera letra).

    P = motor/transmisión, C = chasis, B = carrocería, U = red. Es información
    real y no interpretada: el dashboard no debe insinuar gravedad que no sabe.
    """
    letra = (str(code or "?")[:1]).upper()
    return {"P": "Motor", "C": "Chasis", "B": "Carrocería", "U": "Red"}.get(letra, "Desconocido")


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


def _maf_factor():
    """Factor de corrección del consumo estimado por MAF (mismo criterio que
    `trip_summary._maf_factor`: medido ×1,92 contra el surtidor el 2026-10-28)."""
    try:
        return float((_config().get("vehicle") or {}).get("maf_consumption_factor", 1.0))
    except Exception:
        return 1.0


@app.route("/api/trips")
def api_trips():
    """Trip history + consumo (REAL de surtidor / estimado por MAF / sin datos).

    Cada viaje lleva `consumo_origen` para que la interfaz pueda decir de dónde
    sale la cifra: antes se mezclaban en el mismo campo el real, el del cuadro y
    la estimación MAF sin decirlo.
    """
    conn = get_db()
    c = conn.cursor()
    desde = request.args.get("from")
    hasta = request.args.get("to")
    limit = min(request.args.get("limit", 50, type=int) or 50, 500)
    offset = max(request.args.get("offset", 0, type=int) or 0, 0)
    # La webapp y el colector van por separado: si la BD aún no tiene
    # real_l100 (migración del colector pendiente), no se puede tumbar la
    # página entera — se degrada a NULL y se usa la estimación con MAF.
    cols = {r[1] for r in c.execute("PRAGMA table_info(sessions)")}
    real_col = "real_l100" if "real_l100" in cols else "NULL AS real_l100"
    filtros, params = [], []
    if desde:
        filtros.append("start_time >= ?")
        params.append(desde)
    if hasta:
        filtros.append("start_time <= ?")
        params.append(hasta + "T23:59:59")
    where = ("WHERE " + " AND ".join(filtros) + " ") if filtros else ""
    c.execute(f"SELECT COUNT(*) FROM sessions {where}", tuple(params))
    total = c.fetchone()[0]
    c.execute(f"""
        SELECT id, start_time, end_time, distance_km, max_speed, avg_speed,
               max_rpm, driving_minutes, status, consumption_l100, {real_col}
        FROM sessions {where} ORDER BY start_time DESC LIMIT ? OFFSET ?
    """, tuple(params) + (limit, offset))
    trips = [dict(row) for row in c.fetchall()]
    # Una sola consulta para todos los viajes sin consumo real (antes: un
    # SELECT por viaje, hasta 50 seguidos). El consumo estimado por MAF se
    # calcula solo para los que no tienen real_l100.
    factor = _maf_factor()
    sin_real = [t["id"] for t in trips if t.get("real_l100") is None]
    estimado = {sid: [] for sid in sin_real}
    if sin_real:
        marcas = ",".join("?" * len(sin_real))
        c.execute(
            "SELECT session_id, maf, speed FROM readings "
            f"WHERE maf IS NOT NULL AND speed > 3 AND session_id IN ({marcas})",
            sin_real,
        )
        for r in c.fetchall():
            l100 = (r["maf"] / 740.0) * 3600.0 / r["speed"] * 100.0 * factor
            if 0 < l100 < 60:
                estimado[r["session_id"]].append(l100)
    for t in trips:
        t["consumo_cuadro_l100"] = t.get("consumption_l100")
        if t.get("real_l100") is not None:
            # El consumo que vale: real, depósito a depósito, contra el surtidor.
            t["consumo_l100"] = t["real_l100"]
            t["consumo_origen"] = "real"
            continue
        inst = estimado.get(t["id"]) or []
        if inst:
            t["consumo_l100"] = round(sum(inst) / len(inst), 1)
            t["consumo_origen"] = "estimado_maf"
        else:
            # Ni real ni estimación: se dice, no se inventa una cifra.
            t["consumo_l100"] = None
            t["consumo_origen"] = None
    conn.close()
    resp = jsonify(trips)
    resp.headers["X-Total-Count"] = str(total)
    return resp


@app.route("/api/trip/<int:trip_id>")
def api_trip_detail(trip_id):
    """Detailed data for a specific trip"""
    conn = get_db()
    c = conn.cursor()

    c.execute("SELECT * FROM sessions WHERE id = ?", (trip_id,))
    fila = c.fetchone()
    if fila is None:
        # Antes: dict(None) → TypeError → HTTP 500. Un viaje que no existe es
        # un 404, y el enlace ?trip=<id> de los resúmenes puede llegar viejo.
        conn.close()
        return jsonify({"error": "trip not found", "id": trip_id}), 404
    trip = dict(fila)

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
    """All alerts

    Se devuelven como mucho 100 (respuesta pequeña para el móvil) y el TOTAL va
    en la cabecera `X-Total-Count`: antes la página decía «195 alertas» y
    enseñaba 100 sin avisar de que faltaban 95.
    """
    severity = request.args.get("severity")
    limit = min(request.args.get("limit", 100, type=int) or 100, 500)
    offset = max(request.args.get("offset", 0, type=int) or 0, 0)
    conn = get_db()
    c = conn.cursor()
    if severity:
        c.execute("SELECT COUNT(*) FROM alerts WHERE severity = ?", (severity,))
        total = c.fetchone()[0]
        c.execute("""
            SELECT a.*, s.start_time as trip_start
            FROM alerts a LEFT JOIN sessions s ON a.session_id = s.id
            WHERE a.severity = ? ORDER BY a.timestamp DESC LIMIT ? OFFSET ?
        """, (severity, limit, offset))
    else:
        c.execute("SELECT COUNT(*) FROM alerts")
        total = c.fetchone()[0]
        c.execute("""
            SELECT a.*, s.start_time as trip_start
            FROM alerts a LEFT JOIN sessions s ON a.session_id = s.id
            ORDER BY a.timestamp DESC LIMIT ? OFFSET ?
        """, (limit, offset))
    alerts = [dict(row) for row in c.fetchall()]
    conn.close()
    resp = jsonify(alerts)
    resp.headers["X-Total-Count"] = str(total)
    resp.headers["X-Limit"] = str(limit)
    resp.headers["X-Offset"] = str(offset)
    return resp


@app.route("/api/alerts/acknowledge", methods=["POST"])
def api_acknowledge():
    if not _autorizado():
        return jsonify({"error": "token requerido (cabecera X-OBD-Token)"}), 401
    data = request.get_json(silent=True) or {}
    alert_id = data.get("id")
    if alert_id is None:
        return jsonify({"error": "id required"}), 400
    conn = get_db()
    conn.execute("UPDATE alerts SET acknowledged = 1 WHERE id = ?", (alert_id,))
    conn.commit()
    conn.close()
    return jsonify({"success": True})


@app.route("/api/dtcs/clear", methods=["POST"])
def api_dtcs_clear():
    """Marca los DTCs como borrados (cleared=1) para que no aparezcan como
    activos. No los elimina: quedan en el histórico. Útil para verificar que
    un mantenimiento resolvió el fallo (si el código reaparece, sigue ahí).

    EXIGE la lista de códigos: antes, sin cuerpo, marcaba como borrado el
    histórico ENTERO (`UPDATE dtc SET cleared=1`), y el dashboard tenía un botón
    que lo hacía con una confirmación. Así se ocultó P0113 el 2026-10-28.
    """
    if not _autorizado():
        return jsonify({"error": "token requerido (cabecera X-OBD-Token)"}), 401
    data = request.get_json(silent=True) or {}
    codes = data.get("codes")
    if not isinstance(codes, list) or not codes:
        return jsonify({
            "error": "codes requerido: lista no vacía de códigos concretos",
            "ejemplo": {"codes": ["P0113"]},
        }), 400
    motivo = (data.get("motivo") or "panel")[:200]
    ahora = datetime.now().isoformat(timespec="seconds")
    conn = get_db()
    marcados = {}
    for code in codes:
        cur = conn.execute(
            "UPDATE dtc SET cleared=1, cleared_ts=?, cleared_by=? "
            "WHERE code=? AND (cleared IS NULL OR cleared=0)",
            (ahora, motivo, str(code)),
        )
        marcados[str(code)] = cur.rowcount
    conn.commit()
    conn.close()
    return jsonify({"success": True, "cleared": marcados, "ts": ahora, "motivo": motivo})


@app.route("/api/dtcs")
def api_dtcs():
    """Histórico completo de DTCs: código, descripción, estado (activo/borrado),
    primera/última vez visto, viajes asociados y quién lo marcó como borrado."""
    conn = get_db()
    c = conn.cursor()
    cols = {r[1] for r in c.execute("PRAGMA table_info(dtc)")}
    audit = ", MAX(cleared_ts) AS cleared_ts, MAX(cleared_by) AS cleared_by" if "cleared_ts" in cols else ""
    c.execute(f"""
        SELECT code, description, MAX(cleared) AS cleared,
               MIN(timestamp) AS first_seen, MAX(timestamp) AS last_seen,
               COUNT(*) AS sightings,
               GROUP_CONCAT(DISTINCT session_id) AS session_ids {audit}
        FROM dtc
        GROUP BY code
        ORDER BY last_seen DESC
    """)
    rows = [dict(r) for r in c.fetchall()]
    for r in rows:
        r["sistema"] = _dtc_sistema(r.get("code"))
    conn.close()
    return jsonify(rows)


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


@app.route("/dtcs")
def dtcs():
    return render_template("dtcs.html")


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
               r.liters, r.full_tank, r.session_id, r.price_per_l, r.cost, r.station, r.source,
               r.station_addr, r.station_dist_m, r.car_lat, r.car_lon
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
    if not _autorizado():
        return jsonify({"error": "token requerido (cabecera X-OBD-Token)"}), 401
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
    # Dos decimales, no uno: el surtidor da 49,16 L y redondear a 49,2 falsea
    # el coste y los litros del depósito. Mismo defecto que se arregló en
    # /api/refuels/manual el 2026-08-22 y que aquí había quedado sin tocar.
    c.execute(
        "UPDATE refuels SET liters=?, cost=? WHERE id=?",
        (round(litros, 2), cost, rid),
    )
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "liters": round(litros, 2), "cost": cost, "price_per_l": price})


@app.route("/api/refuels/manual", methods=["POST"])
def api_refuel_manual():
    """Crea un repostaje manual (el usuario lo introduce en la web).

    Cuerpo JSON: {ts?, liters, price_per_l?, station?, full_tank?}
    - ts: fecha ISO opcional (por defecto ahora)
    - liters: obligatorio (> 0)
    - price_per_l: opcional (€/L); si no, cost queda null
    - cost: se calcula si hay price_per_l
    """
    if not _autorizado():
        return jsonify({"error": "token requerido (cabecera X-OBD-Token)"}), 401
    data = request.get_json(silent=True) or {}
    litros = data.get("liters")
    if not litros or litros <= 0:
        return jsonify({"error": "liters > 0 required"}), 400
    ts = data.get("ts") or datetime.now().isoformat()
    price = data.get("price_per_l")
    try:
        price = float(price) if price is not None else None
    except (TypeError, ValueError):
        price = None
    cost = round(float(litros) * price, 2) if price else None
    station = (data.get("station") or "").strip() or None
    full_tank = 1 if data.get("full_tank") else 0
    conn = get_db()
    c = conn.cursor()
    # ts único para el repostaje manual (no choca con detecciones GPS)
    # FIX 2026-08-22: round(...,1) convertía 9.98 L en 10.0 L → redondear a 2
    c.execute(
        "INSERT INTO refuels (ts, liters, price_per_l, cost, station, full_tank, source) "
        "VALUES (?, ?, ?, ?, ?, ?, 'manual')",
        (ts, round(float(litros), 2), price, cost, station, full_tank),
    )
    conn.commit()
    rid = c.lastrowid
    conn.close()
    return jsonify({"ok": True, "id": rid, "ts": ts, "liters": round(float(litros), 2),
                    "cost": cost, "price_per_l": price, "station": station})


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
    if not _autorizado():
        return jsonify({"error": "token requerido (cabecera X-OBD-Token)"}), 401
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

    Seguridad: los GPX los genera nuestro propio logger en Polar Star
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


# Caché de GPX ya parseados: clave = ruta, valor = ((mtime_ns, tamaño), puntos).
# Sin esto cada visita al mapa releía y parseaba los 180 GPX (1,9 MB) aunque el
# cliente solo pidiera un viaje; medido 2026-10-28: 69 ms y creciendo.
_gpx_cache = {}

# Tope de posiciones que se devuelven al mapa sin filtro (el resto se decima).
MAX_POSICIONES_MAPA = 2000


def parse_gpx_cached(path):
    """parse_gpx con caché invalidada por mtime+tamaño del fichero."""
    try:
        st = os.stat(path)
    except OSError:
        return []
    clave = (st.st_mtime_ns, st.st_size)
    guardado = _gpx_cache.get(path)
    if guardado and guardado[0] == clave:
        return guardado[1]
    pts = parse_gpx(path)
    _gpx_cache[path] = (clave, pts)
    return pts


@app.route("/map")
def map_view():
    return render_template("map.html")


@app.route("/api/map")
def api_map():
    """Posiciones GPS (+ tracks GPX) para pintar en el mapa.

    ?session_id=N → solo las posiciones de esa sesión (historial por viaje).
    ?date=YYYY-MM-DD → solo posiciones de ese día (selector de calendario).
    ?tracks=0 → no devolver los tracks GPX (el cliente no los va a pintar).
    ?limit=N → como mucho N posiciones (las más recientes).
    """
    session_id = request.args.get("session_id", type=int)
    date = request.args.get("date")
    limit = request.args.get("limit", type=int)
    con_tracks = request.args.get("tracks", "1") != "0"
    conn = get_db()
    c = conn.cursor()
    params = ()
    if session_id:
        sql = ("SELECT timestamp, lat, lon, gps_speed FROM positions "
               "WHERE session_id=? ORDER BY id")
        params = (session_id,)
        if limit:
            sql = ("SELECT timestamp, lat, lon, gps_speed FROM positions "
                   "WHERE session_id=? ORDER BY id DESC LIMIT ?")
            params = (session_id, limit)
    elif date:
        sql = ("SELECT timestamp, lat, lon, gps_speed FROM positions "
               "WHERE timestamp LIKE ? ORDER BY id")
        params = (date + "%",)
        if limit:
            sql = ("SELECT timestamp, lat, lon, gps_speed FROM positions "
                   "WHERE timestamp LIKE ? ORDER BY id DESC LIMIT ?")
            params = (date + "%", limit)
    else:
        sql = ("SELECT timestamp, lat, lon, gps_speed FROM positions "
               "ORDER BY id")
        if limit:
            sql = ("SELECT timestamp, lat, lon, gps_speed FROM positions "
                   "ORDER BY id DESC LIMIT ?")
            params = (limit,)
    c.execute(sql, params)
    positions = [dict(r) for r in c.fetchall()]
    if limit:
        positions.reverse()   # devolver siempre en orden temporal
    conn.close()

    # Decimación: sin filtro salían 7.677 posiciones (819 KB comprimidos a 180).
    # Para pintar un trazado no hacen falta todas; se conservan primero y último
    # de cada tramo para que el recorrido no cambie de forma.
    total_pos = len(positions)
    decimado = False
    if not limit and total_pos > MAX_POSICIONES_MAPA:
        paso = total_pos / float(MAX_POSICIONES_MAPA)
        muestra = [positions[int(i * paso)] for i in range(MAX_POSICIONES_MAPA)]
        if positions[-1] not in muestra:
            muestra.append(positions[-1])
        positions = muestra
        decimado = True

    tracks = []
    tracks_dir = os.path.expanduser("~/.hermes/data/tracks")
    if con_tracks and os.path.isdir(tracks_dir):
        for f in sorted(os.listdir(tracks_dir)):
            if f.endswith(".gpx"):
                pts = parse_gpx_cached(os.path.join(tracks_dir, f))
                if pts:
                    tracks.append({"name": f, "points": pts})

    resp = jsonify({"positions": positions, "tracks": tracks,
                    "total_positions": total_pos, "decimado": decimado})
    return resp


# ── Consumo (datos CAN que estaban enterrados) ────────────────

@app.route("/api/consumo")
def api_consumo():
    """Consumo por mes y por día, con las tres fuentes separadas y etiquetadas.

    Fuentes:
      · `can_readings.consumption_l100` — lo que marca el cuadro por CAN (~86.700
        lecturas cada ~30 s desde el 23/08, guardadas y sin usar en la web).
      · `sessions.real_l100` — consumo real depósito a depósito (contra surtidor).
      · `refuels` — litros y coste para calcular €/100 km reales.

    `?dias=N` (por defecto 90) acota la serie diaria.
    """
    dias = min(request.args.get("dias", 90, type=int) or 90, 730)
    conn = get_db()
    c = conn.cursor()

    mensual = []
    try:
        c.execute("""
            SELECT substr(ts,1,7) AS mes,
                   COUNT(*) AS muestras,
                   AVG(consumption_l100) AS cuadro_medio,
                   MIN(consumption_l100) AS cuadro_min,
                   MAX(consumption_l100) AS cuadro_max,
                   AVG(range_km) AS rango_medio
            FROM can_readings
            WHERE consumption_l100 IS NOT NULL AND consumption_l100 > 0
            GROUP BY mes ORDER BY mes
        """)
        mensual = [dict(r) for r in c.fetchall()]
    except sqlite3.OperationalError as e:
        sys.stderr.write(f"[webapp] consumo CAN: {e}\n")

    real_mes = {}
    c.execute("""
        SELECT substr(start_time,1,7) AS mes, COUNT(*) AS viajes,
               AVG(real_l100) AS real_medio, SUM(distance_km) AS km
        FROM sessions WHERE real_l100 IS NOT NULL GROUP BY mes
    """)
    for r in c.fetchall():
        real_mes[r["mes"]] = dict(r)

    coste_mes = {}
    c.execute("""
        SELECT substr(ts,1,7) AS mes, SUM(liters) AS litros, SUM(cost) AS coste,
               AVG(price_per_l) AS precio_medio
        FROM refuels WHERE liters IS NOT NULL GROUP BY mes
    """)
    for r in c.fetchall():
        coste_mes[r["mes"]] = dict(r)

    for m in mensual:
        real = real_mes.get(m["mes"], {})
        coste = coste_mes.get(m["mes"], {})
        m["real_medio"] = round(real["real_medio"], 2) if real.get("real_medio") else None
        m["viajes_con_real"] = real.get("viajes")
        m["km"] = round(real["km"], 1) if real.get("km") else None
        m["litros"] = round(coste["litros"], 2) if coste.get("litros") else None
        m["coste_eur"] = round(coste["coste"], 2) if coste.get("coste") else None
        m["precio_medio_l"] = round(coste["precio_medio"], 3) if coste.get("precio_medio") else None
        if m.get("cuadro_medio") is not None:
            m["cuadro_medio"] = round(m["cuadro_medio"], 2)
        # €/100 km real: con precio medio del mes y consumo real
        real_medio = m.get("real_medio")
        precio_medio = m.get("precio_medio_l")
        m["eur_100km"] = round(real_medio * precio_medio, 2) if real_medio and precio_medio else None

    desde = (datetime.now() - timedelta(days=dias)).strftime("%Y-%m-%d")
    serie = []
    try:
        c.execute("""
            SELECT substr(ts,1,10) AS dia, AVG(consumption_l100) AS cuadro,
                   AVG(range_km) AS rango, COUNT(*) AS n
            FROM can_readings
            WHERE ts >= ? AND consumption_l100 IS NOT NULL AND consumption_l100 > 0
            GROUP BY dia ORDER BY dia
        """, (desde,))
        serie = [{"dia": r["dia"], "cuadro": round(r["cuadro"], 2),
                  "rango": round(r["rango"], 1) if r["rango"] else None,
                  "muestras": r["n"]} for r in c.fetchall()]
    except sqlite3.OperationalError:
        pass

    # Aviso honesto: el odómetro del CAN no es de fiar (ver auditoría). No basta
    # con mirar la variación de todo el histórico (arrancó en 105 y saltó a
    # 6.553): lo que delata el fallo es que lleve semanas CONGELADO.
    aviso = None
    try:
        desde30 = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
        c.execute("SELECT MAX(odometer_km) - MIN(odometer_km) AS var, COUNT(*) AS n "
                  "FROM can_readings WHERE odometer_km IS NOT NULL AND ts >= ?",
                  (desde30,))
        r = c.fetchone()
        if r and r["n"] and r["n"] > 100 and (r["var"] is None or r["var"] < 1):
            aviso = ("El odómetro que publica el CAN no se ha movido en los últimos 30 días "
                     f"({r['n']} lecturas): no se usa para calcular nada.")
    except sqlite3.OperationalError:
        pass

    conn.close()
    return jsonify({"mensual": mensual, "serie_diaria": serie, "dias": dias, "aviso": aviso})


# ── Descargas por viaje ───────────────────────────────────────

@app.route("/api/trip/<int:trip_id>/gpx")
def api_trip_gpx(trip_id):
    """GPX del trazado de un viaje (los ficheros existen en disco, pero la web
    no los ofrecía como descarga)."""
    conn = get_db()
    c = conn.cursor()
    fila = c.execute("SELECT start_time, end_time FROM sessions WHERE id=?",
                     (trip_id,)).fetchone()
    if fila is None:
        conn.close()
        return jsonify({"error": "trip not found", "id": trip_id}), 404
    puntos = c.execute(
        "SELECT timestamp, lat, lon FROM positions WHERE session_id=? "
        "AND lat IS NOT NULL AND lon IS NOT NULL ORDER BY id", (trip_id,)).fetchall()
    conn.close()
    if not puntos:
        return jsonify({"error": "trip sin posiciones GPS", "id": trip_id}), 404

    lineas = ['<?xml version="1.0" encoding="UTF-8"?>',
              '<gpx version="1.1" creator="obd-telemetry" '
              'xmlns="http://www.topografix.com/GPX/1/1">',
              f'  <trk><name>Viaje {trip_id} — {fila["start_time"]}</name><trkseg>']
    for p in puntos:
        lineas.append(f'    <trkpt lat="{p["lat"]}" lon="{p["lon"]}">'
                      f'<time>{p["timestamp"]}</time></trkpt>')
    lineas += ['  </trkseg></trk>', '</gpx>']
    cuerpo = "\n".join(lineas)
    resp = Response(cuerpo, mimetype="application/gpx+xml")
    resp.headers["Content-Disposition"] = f'attachment; filename="viaje-{trip_id}.gpx"'
    return resp


@app.route("/api/trip/<int:trip_id>/csv")
def api_trip_csv(trip_id):
    """CSV de las lecturas OBD de un viaje (para hoja de cálculo)."""
    conn = get_db()
    c = conn.cursor()
    if c.execute("SELECT 1 FROM sessions WHERE id=?", (trip_id,)).fetchone() is None:
        conn.close()
        return jsonify({"error": "trip not found", "id": trip_id}), 404
    filas = c.execute(
        "SELECT timestamp, rpm, speed, coolant_temp, engine_load, intake_temp, "
        "maf, map, voltage FROM readings WHERE session_id=? ORDER BY timestamp",
        (trip_id,)).fetchall()
    conn.close()
    if not filas:
        return jsonify({"error": "trip sin lecturas", "id": trip_id}), 404
    cabecera = ["timestamp", "rpm", "speed", "coolant_temp", "engine_load",
                "intake_temp", "maf", "map", "voltage"]
    lineas = [",".join(cabecera)]
    for f in filas:
        lineas.append(",".join("" if f[k] is None else str(f[k]) for k in cabecera))
    resp = Response("\n".join(lineas), mimetype="text/csv")
    resp.headers["Content-Disposition"] = f'attachment; filename="viaje-{trip_id}.csv"'
    return resp


@app.route("/manifest.webmanifest")
def manifest():
    """Manifest de la PWA: la tablet la instala como app en el salpicadero."""
    datos = {
        "name": "OBD Telemetry — Polar Star",
        "short_name": "OBD",
        "start_url": "/",
        "display": "standalone",
        "background_color": "#0d1117",
        "theme_color": "#0d1117",
        "lang": "es",
        "icons": [
            {"src": "/favicon.ico", "sizes": "any", "type": "image/svg+xml",
             "purpose": "any"},
        ],
    }
    return jsonify(datos)


@app.route("/sw.js")
def service_worker():
    """Service worker mínimo: sirve del cache lo ya visto y, si no hay red,
    devuelve la última copia del dashboard en lugar de la pantalla de error.

    No intercepta las APIs con datos en vivo (`/api/`): un dato viejo servido
    como actual es peor que un error visible.
    """
    js = '''
const CACHE = 'obd-v1';
const ESTATICOS = ['/static/style.css', '/static/nav.js', '/static/chart.umd.min.js'];
self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(ESTATICOS)).catch(() => {}));
  self.skipWaiting();
});
self.addEventListener('activate', (e) => {
  e.waitUntil(caches.keys().then(ks => Promise.all(
    ks.filter(k => k !== CACHE).map(k => caches.delete(k)))));
  self.clients.claim();
});
self.addEventListener('fetch', (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== 'GET' || url.pathname.startsWith('/api/')) return;
  if (url.pathname.startsWith('/static/')) {
    e.respondWith(caches.match(e.request).then(r => r || fetch(e.request)));
    return;
  }
  // Páginas: red primero, y si falla, la última copia buena.
  e.respondWith(fetch(e.request).then(r => {
    const copia = r.clone();
    caches.open(CACHE).then(c => c.put(e.request, copia)).catch(() => {});
    return r;
  }).catch(() => caches.match(e.request)));
});
'''
    return Response(js, mimetype="application/javascript")


@app.route("/consumo")
def consumo_view():
    return render_template("consumo.html")


@app.route("/trip/<int:trip_id>")
def trip_view(trip_id):
    """Detalle de un viaje con URL propia (además de /trips?trip=<id>)."""
    return render_template("trip.html", trip_id=trip_id)


if __name__ == "__main__":
    import socket
    # Only listen on Tailscale interface by default
    host = "0.0.0.0"
    port = int(os.environ.get("OBD_WEB_PORT", 8765))
    print(f"🚗 OBD WebApp arrancando en http://{host}:{port}")
    print(f"📡 Accesible via Tailscale: http://100.XX.XX.XX:{port}")
    app.run(host=host, port=port, debug=False)
