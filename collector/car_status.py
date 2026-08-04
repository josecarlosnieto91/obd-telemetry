#!/usr/bin/env python3
"""Estado del coche — odómetro virtual, mantenimiento programado y diagnósticos.

Patrón cron no_agent (cada 15 min):
  - stdout VACÍO  → silencio (no hay cambios nuevos que notificar)
  - stdout TEXTO  → resumen de alertas nuevas a Telegram (solo cambios)

Funciones exportadas para la webapp (import car_status):
  - get_odometer(conn) → km totales estimados
  - get_maintenance(conn) → lista de ítems con km desde último servicio y %
  - get_diagnostics(conn) → batería, termostato, turbo, ralentí

Odómetro virtual: reference_km (lectura del cuadro cuando se activó) + suma de
distancia de viajes completados desde entonces. La referencia se guarda en
obd_vehicle_config.json → vehicle.odometer_reference_km. Servicios registrados
en tabla services (maintenance_id, ts, odometer_km).
"""
import sqlite3, os, json, sys
from datetime import datetime

OBD_DB = os.path.expanduser("~/.hermes/data/obd_telemetry.db")
CONFIG_PATH = os.path.expanduser("~/.hermes/scripts/obd_vehicle_config.json")


def load_config():
    try:
        with open(CONFIG_PATH) as fh:
            return json.load(fh)
    except Exception:
        return {}


def _vehicle_flag(key, default=False):
    """Flag de portabilidad del vehículo (vehicle.<key> en config).
    P.ej. has_turbo, has_dpf — activan/desactivan diagnósticos según el coche."""
    try:
        return bool(load_config().get("vehicle", {}).get(key, default))
    except Exception:
        return default


def get_db():
    conn = sqlite3.connect(OBD_DB, timeout=10.0)
    conn.execute("PRAGMA busy_timeout=10000")
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.OperationalError:
        pass
    conn.row_factory = sqlite3.Row
    _ensure_tables(conn.cursor())
    return conn


def _ensure_tables(c):
    c.execute("""CREATE TABLE IF NOT EXISTS services (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        maintenance_id TEXT NOT NULL,
        ts TEXT NOT NULL,
        odometer_km REAL
    )""")


def get_odometer(conn):
    """Km totales: referencia del cuadro + distancia de viajes completados
    DESPUÉS de la referencia (odometer_reference_ts). La lectura del cuadro
    ya incluye los km previos, así que solo se suman sesiones posteriores
    para no contarlos dos veces."""
    cfg = load_config()
    vehicle = cfg.get("vehicle", {})
    ref = float(vehicle.get("odometer_reference_km", 0) or 0)
    ref_ts = vehicle.get("odometer_reference_ts")
    c = conn.cursor()
    if ref_ts:
        c.execute(
            "SELECT COALESCE(SUM(distance_km), 0) AS s FROM sessions "
            "WHERE status='completed' AND distance_km IS NOT NULL "
            "AND end_time >= ?",
            (ref_ts,),
        )
    else:
        c.execute(
            "SELECT COALESCE(SUM(distance_km), 0) AS s FROM sessions "
            "WHERE status='completed' AND distance_km IS NOT NULL"
        )
    row = c.fetchone()
    return ref + (row["s"] if row else 0.0)


def get_itv():
    """Información ITV: última inspección, validez, días restantes.
    Devuelve dict o None si no está configurada."""
    cfg = load_config()
    itv = cfg.get("vehicle", {}).get("itv")
    if not itv:
        return None
    try:
        valid = datetime.fromisoformat(itv["valid_until"])
        days = (valid - datetime.now()).days
    except Exception:
        days = None
    return {
        "last_date": itv.get("last_date"),
        "valid_until": itv.get("valid_until"),
        "last_km": itv.get("last_km"),
        "status": itv.get("status", "favorable"),
        "days_left": days,
    }


def ignored_dtcs():
    """Conjunto de códigos DTC ignorados (config → vehicle.ignored_dtcs)."""
    cfg = load_config()
    d = cfg.get("vehicle", {}).get("ignored_dtcs", {}) or {}
    return set(d.keys())


def get_maintenance(conn):
    """Ítems de mantenimiento con km desde último servicio y % de intervalo usado.

    Devuelve lista de dicts: {id, name, icon, interval_km, interval_days,
    last_km, last_ts, km_done, pct_km, pct_days, due_km, due_days, overdue}
    """
    cfg = load_config()
    items = cfg.get("maintenance", [])
    odometer = get_odometer(conn)
    c = conn.cursor()
    out = []
    for it in items:
        c.execute(
            "SELECT ts, odometer_km FROM services WHERE maintenance_id=? "
            "ORDER BY ts DESC LIMIT 1",
            (it["id"],),
        )
        row = c.fetchone()
        last_km = row["odometer_km"] if row and row["odometer_km"] is not None else None
        last_ts = row["ts"] if row else None

        km_done = None
        pct_km = None
        if it.get("interval_km") and last_km is not None:
            km_done = max(0.0, odometer - last_km)
            pct_km = min(100.0, km_done / it["interval_km"] * 100.0)

        pct_days = None
        if it.get("interval_days") and last_ts:
            try:
                days = (datetime.now() - datetime.fromisoformat(last_ts)).total_seconds() / 86400.0
                pct_days = min(100.0, max(0.0, days / it["interval_days"] * 100.0))
            except Exception:
                pass

        # % global = el peor de los dos (km o días)
        pct = max([p for p in (pct_km, pct_days) if p is not None] or [0.0])
        overdue = pct >= 100.0
        out.append({
            **it,
            "last_km": last_km, "last_ts": last_ts,
            "km_done": round(km_done, 0) if km_done is not None else None,
            "pct_km": round(pct_km, 1) if pct_km is not None else None,
            "pct_days": round(pct_days, 1) if pct_days is not None else None,
            "pct": round(pct, 1),
            "due_km": round(it["interval_km"] - km_done, 0) if it.get("interval_km") and km_done is not None else None,
            "overdue": overdue,
        })
    return out


def get_diagnostics(conn):
    """Diagnósticos del motor a partir de la calibración aprendida.

    Devuelve dict con estado de: battery, thermostat, turbo, idle.
    Cada uno: {status: 'ok'|'warn'|'critical'|'nodata', label, detail, value}
    """
    cfg = load_config()
    thr = cfg.get("thresholds", {})
    c = conn.cursor()

    # ── Batería: voltaje en parado (calibración voltage_stop) + lecturas recientes
    battery = {"status": "nodata", "label": "—", "detail": "Sin datos suficientes", "value": None}
    try:
        c.execute("SELECT value FROM calibration WHERE key='voltage_stop'")
        r = c.fetchone()
        c.execute(
            "SELECT voltage FROM readings WHERE rpm < 100 AND voltage IS NOT NULL "
            "ORDER BY timestamp DESC LIMIT 5"
        )
        recent = [row["voltage"] for row in c.fetchall()]
        ref = r["value"] if r else None
        if recent:
            v = sum(recent) / len(recent)
            if v >= thr.get("battery_ok_v", 12.4):
                battery = {"status": "ok", "label": "Saludable", "detail": f"Voltaje parado {v:.2f} V", "value": round(v, 2)}
            elif v >= thr.get("battery_warn_v", 12.0):
                battery = {"status": "warn", "label": "Precaución", "detail": f"Voltaje parado {v:.2f} V (batería baja)", "value": round(v, 2)}
            else:
                battery = {"status": "critical", "label": "Crítica", "detail": f"Voltaje parado {v:.2f} V — revisar batería", "value": round(v, 2)}
        elif ref is not None:
            if ref >= thr.get("battery_ok_v", 12.4):
                battery = {"status": "ok", "label": "Saludable", "detail": f"Calibrado {ref:.2f} V", "value": round(ref, 2)}
            else:
                battery = {"status": "warn", "label": "Precaución", "detail": f"Calibrado {ref:.2f} V", "value": round(ref, 2)}
    except Exception as e:
        sys.stderr.write(f"battery diag fail: {e}\n")

    # ── Termostato: en viajes >20 km, la temp debe superar el mínimo
    thermostat = {"status": "nodata", "label": "—", "detail": "Necesita un viaje de >20 km", "value": None}
    try:
        c.execute("""
            SELECT s.id, s.distance_km,
                   MAX(r.coolant_temp) AS max_temp, MAX(r.speed) AS max_speed
            FROM sessions s JOIN readings r ON r.session_id = s.id
            WHERE s.status='completed' AND s.distance_km >= 20
            GROUP BY s.id ORDER BY s.end_time DESC LIMIT 5
        """)
        trips = [dict(row) for row in c.fetchall()]
        if trips:
            # Usar el viaje más reciente con max_speed > 60 (crucero real)
            real = next((t for t in trips if (t["max_speed"] or 0) > 60), trips[0])
            mt = real["max_temp"]
            if mt is not None and mt < thr.get("thermostat_min_c", 75.0):
                thermostat = {"status": "warn", "label": "Sospecha", "detail": f"Temp máx {mt:.0f}°C en viaje de {real['distance_km']:.0f} km (termostato puede estar abierto)", "value": round(mt, 1)}
            elif mt is not None:
                thermostat = {"status": "ok", "label": "OK", "detail": f"Temp máx {mt:.0f}°C en viaje de {real['distance_km']:.0f} km", "value": round(mt, 1)}
    except Exception as e:
        sys.stderr.write(f"thermostat diag fail: {e}\n")

    # ── Turbo: MAP en crucero (speed > 80) — tendencia a la baja = VGT débil
    # Portabilidad 2026-08-05: solo si vehicle.has_turbo (un atmosférico no
    # tiene MAP de sobrealimentación → el check sería ruido).
    turbo = {"status": "nodata", "label": "—", "detail": "Sin datos MAP suficientes", "value": None}
    if _vehicle_flag("has_turbo", True):
        try:
            c.execute("""
                SELECT AVG(map) AS avg_map, COUNT(*) AS n
                FROM readings WHERE map IS NOT NULL AND speed > 80
            """)
            r = c.fetchone()
            if r and r["n"] and r["n"] >= 5 and r["avg_map"]:
                avg = r["avg_map"]
                if avg < 90:
                    turbo = {"status": "warn", "label": "Bajo", "detail": f"MAP medio en crucero {avg:.0f} kPa (posible VGT/turbo débil)", "value": round(avg, 1)}
                else:
                    turbo = {"status": "ok", "label": "OK", "detail": f"MAP medio en crucero {avg:.0f} kPa", "value": round(avg, 1)}
        except Exception as e:
            sys.stderr.write(f"turbo diag fail: {e}\n")

    # ── Ralentí: desviación estándar de RPM en idle (motor caliente)
    idle = {"status": "nodata", "label": "—", "detail": "Sin datos de ralentí suficientes", "value": None}
    try:
        c.execute("""
            SELECT rpm FROM readings
            WHERE speed < 2 AND coolant_temp >= 70 AND rpm IS NOT NULL
              AND rpm BETWEEN 500 AND 2000
            ORDER BY timestamp DESC LIMIT 30
        """)
        rpms = [row["rpm"] for row in c.fetchall()]
        if len(rpms) >= 5:
            mean = sum(rpms) / len(rpms)
            var = sum((x - mean) ** 2 for x in rpms) / len(rpms)
            std = var ** 0.5
            if std > thr.get("idle_rpm_stddev_max", 100.0):
                idle = {"status": "warn", "label": "Inestable", "detail": f"Desviación ralentí {std:.0f} rpm (media {mean:.0f}) — posible inyectores/EGR", "value": round(std, 1)}
            else:
                idle = {"status": "ok", "label": "Estable", "detail": f"Desviación ralentí {std:.0f} rpm (media {mean:.0f})", "value": round(std, 1)}
    except Exception as e:
        sys.stderr.write(f"idle diag fail: {e}\n")

    return {"battery": battery, "thermostat": thermostat, "turbo": turbo, "idle": idle}


# ── Alertas ────────────────────────────────────────────────

def _insert_alert(c, conn, alert_type, severity, message):
    """Inserta alerta solo si no existe una del mismo tipo sin acknowledge."""
    c.execute(
        "SELECT id FROM alerts WHERE category='diagnostico' AND message=? "
        "AND acknowledged=0 ORDER BY id DESC LIMIT 1",
        (message,),
    )
    if c.fetchone():
        return False
    ts = datetime.now().isoformat()
    c.execute(
        "INSERT INTO alerts (session_id, timestamp, category, severity, message) "
        "VALUES (NULL, ?, 'diagnostico', ?, ?)",
        (ts, severity, message),
    )
    conn.commit()
    return True


def check_and_alert():
    """Genera alertas para diagnósticos en estado warn/critical. Solo nuevas."""
    conn = get_db()
    c = conn.cursor()
    _ensure_tables(c)
    diag = get_diagnostics(conn)
    new_alerts = []

    if diag["battery"]["status"] in ("warn", "critical"):
        sev = "critical" if diag["battery"]["status"] == "critical" else "warning"
        if _insert_alert(c, conn, "battery", sev, f"🔋 Batería: {diag['battery']['detail']}"):
            new_alerts.append(f"🔋 {diag['battery']['detail']}")

    if diag["thermostat"]["status"] == "warn":
        if _insert_alert(c, conn, "thermostat", "warning", f"🌡️ Termostato: {diag['thermostat']['detail']}"):
            new_alerts.append(f"🌡️ {diag['thermostat']['detail']}")

    if diag["turbo"]["status"] == "warn":
        if _insert_alert(c, conn, "turbo", "warning", f"🌀 Turbo: {diag['turbo']['detail']}"):
            new_alerts.append(f"🌀 {diag['turbo']['detail']}")

    if diag["idle"]["status"] == "warn":
        if _insert_alert(c, conn, "idle", "warning", f"🔧 Ralentí: {diag['idle']['detail']}"):
            new_alerts.append(f"🔧 {diag['idle']['detail']}")

    # Mantenimiento vencido (overdue) — aviso único por ítem
    for it in get_maintenance(conn):
        if it["overdue"]:
            if _insert_alert(c, conn, f"maint_{it['id']}", "warning",
                             f"📅 {it['icon']} {it['name']}: intervalo superado ({it['pct']:.0f}%)"):
                new_alerts.append(f"📅 {it['icon']} {it['name']}: toca revisión")

    # ITV próxima a vencer (< 60 días) — aviso único
    itv = get_itv()
    if itv and itv["days_left"] is not None and itv["days_left"] < 60:
        if _insert_alert(c, conn, "itv", "warning",
                         f"🛂 ITV vence en {itv['days_left']} días ({itv['valid_until']}). "
                         f"Última: {itv['last_date']} a los {itv['last_km']} km — favorable."):
            new_alerts.append(f"🛂 ITV vence en {itv['days_left']} días")

    conn.close()
    if new_alerts:
        print("🔧 Diagnósticos del coche:\n" + "\n".join(f"• {a}" for a in new_alerts))


if __name__ == "__main__":
    check_and_alert()
