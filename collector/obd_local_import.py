#!/usr/bin/env python3
"""obd_local_import.py — importa el SQLite local de Polar Star en obd_telemetry.db.

La tablet (Polar Star) recopila OBD2+GPS localmente (~/obd_data/obd_local.db)
sin depender de Internet. Cuando hay red, sube el fichero completo por SCP a
~/.hermes/data/incoming/polar_obd_local.db. Este script mergea esas filas en
obd_telemetry.db reutilizando la sesión activa actual, con dedup por timestamp.

Sin fichero entrante → sale en silencio (exit 0). Después de importar, mueve
el fichero a processed/ para no reimportar.
"""
import datetime
import json
import os
import shutil
import sqlite3
import sys

HOME = os.path.expanduser("~")
INCOMING = os.path.join(HOME, ".hermes/data/incoming/polar_obd_local.db")
PROCESSED_DIR = os.path.join(HOME, ".hermes/data/incoming/processed")
TARGET_DB = os.path.join(HOME, ".hermes/data/obd_telemetry.db")
CONFIG_PATH = os.path.join(HOME, ".hermes/scripts/obd_vehicle_config.json")

# Si el primer dato nuevo llega más de SESSION_GAP_MINUTES después de la última
# lectura de la sesión activa, es un viaje NUEVO (el coche estuvo apagado):
# cerrar la sesión vieja y abrir una limpia. Evita sesiones que mezclan días
# (p.ej. la sesión 14 que acumuló 03-08 tarde + 04-08 mañana con 1392 min).
SESSION_GAP_MINUTES = 60


def load_config():
    try:
        with open(CONFIG_PATH) as fh:
            return json.load(fh)
    except Exception:
        return {}

FIELD_MAP = {
    "rpm": "rpm",
    "speed": "speed",
    "coolant": "coolant_temp",
    "throttle": "throttle_pos",
    "intake": "intake_temp",
    "fuel": "fuel_level",
    "maf": "maf",
    "voltage": "voltage",
}


def connect_db(path, wal=True):
    """Conexión SQLite con protección de concurrencia (ver trip_summary.connect_db)."""
    conn = sqlite3.connect(path, timeout=10.0)
    conn.execute("PRAGMA busy_timeout=10000")
    if wal:
        try:
            conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.OperationalError:
            pass
    # Migración idempotente: fuel_rate (PID 015E) puede no existir en BDs viejas
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(readings)")]
        if "fuel_rate" not in cols:
            conn.execute("ALTER TABLE readings ADD COLUMN fuel_rate REAL")
        if "engine_load" not in cols:
            conn.execute("ALTER TABLE readings ADD COLUMN engine_load REAL")
    except sqlite3.OperationalError:
        pass
    # v4.8: tabla can_readings (consumo CAN del decodificador Witson)
    try:
        conn.execute("""CREATE TABLE IF NOT EXISTS can_readings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER,
            ts TEXT NOT NULL,
            consumption_l100 REAL,
            range_km REAL,
            odometer_km REAL)""")
    except sqlite3.OperationalError:
        pass
    return conn


def get_or_create_active_session(conn, start_ts, first_new_ts=None):
    """Devuelve la sesión activa si los datos nuevos continúan la anterior;
    si no, cierra la vieja y crea una sesión limpia.

    first_new_ts: primer timestamp REALMENTE NUEVO que se va a importar (el más
    antiguo del fichero local que no está en destino). Si ese dato llega más de
    SESSION_GAP_MINUTES después de la última lectura de la sesión activa, el
    coche estuvo apagado entre medias → es un viaje nuevo, no una continuación.
    """
    row = conn.execute(
        "SELECT id FROM sessions WHERE status='active' ORDER BY id DESC LIMIT 1").fetchone()
    if row:
        sid = row[0]
        if first_new_ts:
            last = conn.execute(
                "SELECT MAX(timestamp) AS t FROM readings WHERE session_id=?",
                (sid,)).fetchone()
            if last and last[0]:
                try:
                    gap_min = (datetime.datetime.fromisoformat(first_new_ts)
                               - datetime.datetime.fromisoformat(last[0])).total_seconds() / 60.0
                except Exception:
                    gap_min = 0.0
                if gap_min > SESSION_GAP_MINUTES:
                    # Cerrar la sesión vieja (la cierra trip_summary con métricas
                    # reales en la siguiente pasada) y abrir una limpia.
                    conn.execute(
                        "UPDATE sessions SET status='completed', end_time=? "
                        "WHERE id=? AND status='active'",
                        (last[0], sid))
                    cur = conn.execute(
                        "INSERT INTO sessions (start_time, status) VALUES (?, 'active')",
                        (start_ts,))
                    return cur.lastrowid
            else:
                # Sesión activa sin ninguna lectura (fantasma creada por un sync
                # sin datos): los datos nuevos van a una sesión limpia.
                conn.execute(
                    "UPDATE sessions SET status='completed', end_time=? "
                    "WHERE id=? AND status='active'",
                    (start_ts, sid))
                cur = conn.execute(
                    "INSERT INTO sessions (start_time, status) VALUES (?, 'active')",
                    (start_ts,))
                return cur.lastrowid
        return sid
    cur = conn.execute(
        "INSERT INTO sessions (start_time, status) VALUES (?, 'active')", (start_ts,))
    return cur.lastrowid


def import_readings(conn, local_conn, session_id, ts_min=None, ts_max=None):
    try:
        # Columnas diésel/dinámicas (map/ambient/fuel_pressure/fuel_rate/
        # engine_load) pueden no existir en ficheros antiguos → consulta
        # dinámica según PRAGMA del fichero local
        local_cols = [r[1] for r in local_conn.execute("PRAGMA table_info(readings)")]
        diesel = [c for c in ("map", "ambient", "fuel_pressure", "fuel_rate", "engine_load")
                  if c in local_cols]
        sel = "timestamp, rpm, speed, coolant, throttle, intake, fuel, maf, voltage"
        if diesel:
            sel += ", " + ", ".join(diesel)
        rows = local_conn.execute(f"SELECT {sel} FROM readings").fetchall()
    except sqlite3.OperationalError:
        return 0  # tabla readings no existe en el fichero antiguo
    if not rows:
        return 0
    # Filtrar por bloque temporal (si se pasa: ts_min/ts_max de este viaje)
    if ts_min is not None or ts_max is not None:
        lo = ts_min or ""
        hi = ts_max or "~~~~~"  # > cualquier ISO timestamp
        rows = [r for r in rows if lo <= r[0] <= hi]
        if not rows:
            return 0
    # Timestamps ya presentes en destino (rango aproximado del fichero local)
    min_ts = rows[0][0]
    max_ts = rows[-1][0]
    existing = set(r[0] for r in conn.execute(
        "SELECT timestamp FROM readings WHERE timestamp BETWEEN ? AND ?",
        (min_ts, max_ts)).fetchall())
    inserted = 0
    for row in rows:
        ts, rpm, speed, coolant, throttle, intake, fuel, maf, voltage = row[:9]
        dmap = dict(zip(diesel, row[9:])) if diesel else {}
        if ts in existing:
            continue
        conn.execute(
            "INSERT INTO readings (session_id, timestamp, rpm, speed, coolant_temp, "
            "throttle_pos, intake_temp, fuel_level, maf, voltage, map, ambient, "
            "fuel_pressure, fuel_rate, engine_load) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (session_id, ts, rpm, speed, coolant, throttle, intake, fuel, maf, voltage,
             dmap.get("map"), dmap.get("ambient"), dmap.get("fuel_pressure"),
             dmap.get("fuel_rate"), dmap.get("engine_load")))
        existing.add(ts)
        inserted += 1
    return inserted


def import_positions(conn, local_conn, session_id, ts_min=None, ts_max=None):
    try:
        rows = local_conn.execute(
            "SELECT timestamp, lat, lon, speed, bearing, accuracy, alt, provider "
            "FROM positions").fetchall()
    except sqlite3.OperationalError:
        return 0  # tabla positions no existe en el fichero antiguo
    if not rows:
        return 0
    if ts_min is not None or ts_max is not None:
        lo = ts_min or ""
        hi = ts_max or "~~~~~"
        rows = [r for r in rows if lo <= r[0] <= hi]
        if not rows:
            return 0
    min_ts = rows[0][0]
    max_ts = rows[-1][0]
    existing = set(r[0] for r in conn.execute(
        "SELECT timestamp FROM positions WHERE timestamp BETWEEN ? AND ?",
        (min_ts, max_ts)).fetchall())
    inserted = 0
    for ts, lat, lon, speed, bearing, accuracy, altitude, provider in rows:
        if ts in existing:
            continue
        conn.execute(
            "INSERT INTO positions (session_id, timestamp, lat, lon, gps_speed, "
            "bearing, accuracy, altitude, provider) VALUES (?,?,?,?,?,?,?,?,?)",
            (session_id, ts, lat, lon, speed, bearing, accuracy, altitude, provider))
        existing.add(ts)
        inserted += 1
    return inserted


def split_into_blocks(local_conn, after_ts, gap_minutes=SESSION_GAP_MINUTES):
    """Divide las lecturas NUEVAS del fichero local en bloques de viaje.

    El fichero local acumula todo el historial de la tablet. Si entre dos
    lecturas consecutivas hay un hueco > gap_minutes (coche apagado), es un
    viaje DISTINTO → bloque nuevo. Cada bloque generará su propia sesión.

    Devuelve lista de (ts_min, ts_max). Vacía si no hay lecturas nuevas.
    """
    try:
        if after_ts:
            rows = local_conn.execute(
                "SELECT timestamp FROM readings WHERE timestamp > ? ORDER BY timestamp",
                (after_ts,)).fetchall()
        else:
            rows = local_conn.execute(
                "SELECT timestamp FROM readings ORDER BY timestamp").fetchall()
    except sqlite3.OperationalError:
        return []
    if not rows:
        return []

    blocks = []
    cur_min = cur_max = None
    prev = None
    for (ts,) in rows:
        if prev is not None:
            try:
                gap = (datetime.datetime.fromisoformat(ts)
                       - datetime.datetime.fromisoformat(prev)).total_seconds() / 60.0
            except Exception:
                gap = 0.0
            if gap > gap_minutes:
                blocks.append((cur_min, cur_max))
                cur_min = cur_max = None
        if cur_min is None:
            cur_min = ts
        cur_max = ts
        prev = ts
    if cur_min is not None:
        blocks.append((cur_min, cur_max))
    return blocks


def import_dtcs(conn, local_conn, session_id):
    """Importa DTCs locales (tabla dtc del fichero de la tablet).
    Dedup por code en destino: si el código ya existe, actualiza timestamp
    (el recolector local hace upsert por code+kind, así que cada sync trae
    el último avistamiento de cada código).

    Los códigos en ignored_dtcs del config se marcan cleared=1 en destino:
    quedan en el histórico pero nunca aparecen como DTC activo."""
    try:
        ignored = set((load_config().get("vehicle", {}) or {}).get("ignored_dtcs", {}).keys())
        descs = (load_config().get("dtc_descriptions", {}) or {})
    except Exception:
        ignored = set()
        descs = {}
    try:
        rows = local_conn.execute(
            "SELECT timestamp, code, description, kind FROM dtc ORDER BY timestamp").fetchall()
    except sqlite3.OperationalError:
        return 0  # tabla dtc no existe en el fichero antiguo
    if not rows:
        return 0
    inserted = 0
    updated = 0
    for ts, code, desc, kind in rows:
        cleared = 1 if code in ignored else 0
        # La tablet manda descripción vacía; usar la del config como fallback
        # (FIX 2026-08-21: sin esto, los DTCs sin descripción local pisaban la BD
        # y la webapp mostraba "Sin descripción" aunque el config las tuviera).
        if not desc:
            desc = descs.get(code, "")
        exists = conn.execute(
            "SELECT id FROM dtc WHERE code=? LIMIT 1", (code,)).fetchone()
        if exists:
            conn.execute(
                "UPDATE dtc SET timestamp=?, description=?, cleared=? WHERE id=?",
                (ts, desc or "", cleared, exists[0]))
            updated += 1
        else:
            conn.execute(
                "INSERT INTO dtc (session_id, timestamp, code, description, cleared) "
                "VALUES (?,?,?,?,?)",
                (session_id, ts, code, desc or "", cleared))
            inserted += 1
    return inserted + updated


def import_can_readings(conn, local_conn):
    """Importa lecturas CAN del decodificador (tabla can_readings de la tablet,
    poblada por el recolector desde el CSV del CanSnifferService)."""
    try:
        local_cols = [r[1] for r in local_conn.execute("PRAGMA table_info(can_readings)")]
        if "consumption_l100" not in local_cols:
            return 0
        rows = local_conn.execute(
            "SELECT ts, consumption_l100, range_km, odometer_km FROM can_readings "
            "ORDER BY ts").fetchall()
    except sqlite3.OperationalError:
        return 0
    if not rows:
        return 0
    existing = set(r[0] for r in conn.execute(
        "SELECT ts FROM can_readings WHERE ts BETWEEN ? AND ?",
        (rows[0][0], rows[-1][0])).fetchall())
    inserted = 0
    for ts, cons, rng, odom in rows:
        if ts in existing:
            continue
        conn.execute(
            "INSERT INTO can_readings (session_id, ts, consumption_l100, range_km, odometer_km) "
            "VALUES (?,?,?,?,?)",
            (None, ts, cons, rng, odom))
        existing.add(ts)
        inserted += 1
    return inserted


def import_fap_events(conn, local_conn):
    """Importa eventos de regeneración FAP detectados en la tablet."""
    try:
        rows = local_conn.execute(
            "SELECT start_ts, end_ts, duration_min, rpm_avg, maf_avg "
            "FROM fap_events ORDER BY start_ts").fetchall()
    except sqlite3.OperationalError:
        return 0
    if not rows:
        return 0
    inserted = 0
    for start_ts, end_ts, dur, rpm_avg, maf_avg in rows:
        exists = conn.execute(
            "SELECT 1 FROM fap_events WHERE start_ts=?", (start_ts,)).fetchone()
        if exists:
            continue
        conn.execute(
            "INSERT INTO fap_events (start_ts, end_ts, duration_min, rpm_avg, maf_avg) "
            "VALUES (?,?,?,?,?)",
            (start_ts, end_ts, dur, rpm_avg, maf_avg))
        inserted += 1
    return inserted


def import_calibration(conn, local_conn):
    """Importa calibración aprendida (valores normales del vehículo).
    Sobrescribe los valores existentes (la tablet tiene los datos más frescos)."""
    try:
        rows = local_conn.execute(
            "SELECT key, value, n, updated FROM calibration").fetchall()
    except sqlite3.OperationalError:
        return 0
    if not rows:
        return 0
    inserted = 0
    for key, value, n, updated in rows:
        exists = conn.execute(
            "SELECT 1 FROM calibration WHERE key=?", (key,)).fetchone()
        if exists:
            conn.execute(
                "UPDATE calibration SET value=?, n=?, updated=? WHERE key=?",
                (value, n, updated, key))
        else:
            conn.execute(
                "INSERT INTO calibration (key, value, n, updated) VALUES (?,?,?,?)",
                (key, value, n, updated))
        inserted += 1
    return inserted


def main():
    if not os.path.exists(INCOMING):
        return 0  # silencio: no hay datos entrantes

    try:
        local_conn = connect_db(INCOMING, wal=False)
        target_conn = connect_db(TARGET_DB)

        # Sesión activa (o crear una si no existe — caso tablet sin TCP previo).
        # ⚠️ BUG FIX 2026-08-03: antes se usaba el primer timestamp del fichero
        # local COMPLETO como start_time. Como el fichero acumula todo el
        # historial, cada sync (sin sesión activa) creaba una sesión fantasma
        # con el mismo start_time antiguo (p.ej. 09:28:06 repetido en sesiones
        # 8-11). Ahora se usa el primer timestamp REALMENTE NUEVO (no existente
        # en destino); si todo está ya importado, se reutiliza la activa o se
        # crea sin tocar el historial.
        #
        # ⚠️ BUG FIX 2026-08-04: además se cierra la sesión activa si el primer
        # dato nuevo llega más de SESSION_GAP_MINUTES después de su última
        # lectura (coche apagado entre medias = viaje nuevo, no continuación).
        start_ts = datetime.datetime.now().isoformat()
        first_new_ts = None
        last_dest_ts = None
        try:
            # Último timestamp ya presente en destino (global).
            last_dest = target_conn.execute(
                "SELECT MAX(timestamp) AS t FROM readings").fetchone()
            last_dest_ts = last_dest[0] if last_dest else None
            # Primer timestamp del fichero local estrictamente posterior al
            # último importado → primer dato realmente nuevo (o None si todo
            # está ya en destino).
            if last_dest_ts:
                first_new = local_conn.execute(
                    "SELECT timestamp FROM readings WHERE timestamp > ? "
                    "ORDER BY timestamp LIMIT 1", (last_dest_ts,)).fetchone()
            else:
                first_new = local_conn.execute(
                    "SELECT timestamp FROM readings ORDER BY timestamp LIMIT 1").fetchone()
            if first_new:
                first_new_ts = first_new[0]
                start_ts = first_new_ts
        except sqlite3.OperationalError:
            pass  # fichero sin tabla readings (solo dtc, caso borde)

        # ⚠️ BUG FIX 2026-08-08: dividir las lecturas nuevas en bloques por
        # hueco temporal. El fichero local puede contener varios viajes con
        # el coche apagado entre medias (p.ej. 07/08 21:24-21:30 + 08/08
        # 08:49-08:54 → 690 min de "viaje" falso). Cada bloque con gap >
        # SESSION_GAP_MINUTES crea su PROPIA sesión, con start_time = primer
        # dato del bloque (no el del día anterior).
        blocks = split_into_blocks(local_conn, last_dest_ts)
        if not blocks:
            # Sin lecturas nuevas: sesión para dtc/fap/cal (o nada)
            session_id = get_or_create_active_session(target_conn, start_ts, first_new_ts)
            n_read = 0
            n_pos = 0
        else:
            session_id = None
            n_read = 0
            n_pos = 0
            for (bmin, bmax) in blocks:
                if session_id is None:
                    # Primer bloque: reutiliza la sesión activa si el hueco es
                    # pequeño (viaje en curso) o crea una limpia.
                    session_id = get_or_create_active_session(target_conn, bmin, bmin)
                else:
                    # Bloques siguientes: SIEMPRE sesión nueva (viaje distinto)
                    cur = target_conn.execute(
                        "INSERT INTO sessions (start_time, status) VALUES (?, 'active')",
                        (bmin,))
                    session_id = cur.lastrowid
                n_read += import_readings(target_conn, local_conn, session_id, bmin, bmax)
                n_pos += import_positions(target_conn, local_conn, session_id, bmin, bmax)

        n_dtc = import_dtcs(target_conn, local_conn, session_id or 0)
        n_fap = import_fap_events(target_conn, local_conn)
        n_cal = import_calibration(target_conn, local_conn)
        n_can = import_can_readings(target_conn, local_conn)
        target_conn.commit()

        os.makedirs(PROCESSED_DIR, exist_ok=True)
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        shutil.move(INCOMING, os.path.join(PROCESSED_DIR, f"polar_obd_local_{stamp}.db"))

        if n_read or n_pos or n_dtc or n_fap or n_cal or n_can:
            # Silencio en stdout (cron no_agent): traza a log de import.
            try:
                with open(os.path.join(PROCESSED_DIR, "import.log"), "a") as lf:
                    lf.write(f"{datetime.datetime.now().isoformat()} "
                             f"import {n_read} readings, {n_pos} positions, "
                             f"{n_dtc} dtc, {n_fap} fap, {n_cal} cal, {n_can} can "
                             f"(session {session_id})\n")
            except Exception:
                pass
        return 0
    except Exception as error:
        # ⚠️ FIX 2026-08-22: BD entrante corrupta ("database disk image is
        # malformed"). Antes: exit 1 SIN mover el fichero → el cron reintentaba
        # cada 5 min y notificaba siempre el mismo error. Ahora: se mueve a
        # corrupt/ (preservando el dato para diagnóstico/recuperación manual)
        # y se sale en silencio (exit 0) para que el cron no vuelva a fallar.
        if os.path.exists(INCOMING):
            try:
                corrupt_dir = os.path.join(PROCESSED_DIR, "corrupt")
                os.makedirs(corrupt_dir, exist_ok=True)
                stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
                shutil.move(INCOMING, os.path.join(corrupt_dir, f"polar_obd_local_{stamp}.db"))
                try:
                    with open(os.path.join(PROCESSED_DIR, "import.log"), "a") as lf:
                        lf.write(f"{datetime.datetime.now().isoformat()} "
                                 f"CORRUPT moved to corrupt/ ({error})\n")
                except Exception:
                    pass
            except Exception as move_error:
                print(f"ERROR import: {error} (y no se pudo mover: {move_error})",
                      file=sys.stderr)
                return 1
        return 0


if __name__ == "__main__":
    sys.exit(main())
