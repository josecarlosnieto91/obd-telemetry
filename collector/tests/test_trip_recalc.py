"""Regresión del recálculo de viajes cerrados (2026-09-16).

Contexto real: `merge_sessions.py` une tramos partidos de un mismo viaje y solo
SUMABA distancia y minutos. La sesión fusionada quedaba quimérica —velocidad
máxima y avisos del primer tramo, con la distancia de los dos— y no había
manera de rehacerla sin devolverla a 'active', que reenvía el resumen por
Telegram y duplica el evento en context.db.

Cubre:
1. `recalc_session()` corrige las métricas de un viaje ya cerrado.
2. Recalcular dos veces no acumula avisos (idempotente).
3. Un viaje fusionado se recalcula: max_speed del tramo que más corrió y sin los
   avisos del tramo corto original.
4. `keep_aggregates=True` respeta la distancia/minutos que sumó la fusión: NO
   recomputa cruzando el hueco sin lecturas.
"""
import importlib.util
import os
import sqlite3
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
COLLECTOR_DIR = os.path.dirname(HERE)
SCHEMA = os.path.join(HERE, "schema.sql")


def _load(name):
    """Carga un script del recolector como módulo (no son paquetes)."""
    path = os.path.join(COLLECTOR_DIR, name)
    spec = importlib.util.spec_from_file_location(f"under_test_{name}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    """(trip_summary, merge_sessions, conn) con BD y config en tmp_path."""
    db = tmp_path / "obd.db"
    c0 = sqlite3.connect(db)
    with open(SCHEMA) as fh:
        c0.executescript(fh.read())
    c0.close()

    cfg_path = tmp_path / "vehicle.json"
    cfg_path.write_text('{"vehicle": {"range_km_per_l": 17.9, "reserve_liters": 5.6}}')

    ts = _load("trip_summary.py")
    # El import de `merge_sessions` debe resolver a ESTA instancia (con OBD_DB y
    # config en tmp_path). Sin esto cargaría una segunda copia sin monkeypatch y
    # el recálculo escribiría en la BD de producción.
    monkeypatch.setitem(sys.modules, "trip_summary", ts)
    mg = _load("merge_sessions.py")
    for mod in (ts, mg):
        monkeypatch.setattr(mod, "OBD_DB", str(db))
        monkeypatch.setattr(mod, "CONFIG_PATH", str(cfg_path))

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    yield ts, mg, conn
    conn.close()


def _session(conn, start, end, status="completed", **metrics):
    cols = {"start_time": start, "end_time": end, "status": status}
    cols.update(metrics)
    keys = ", ".join(cols)
    marks = ", ".join("?" * len(cols))
    cur = conn.execute(f"INSERT INTO sessions ({keys}) VALUES ({marks})",
                       tuple(cols.values()))
    conn.commit()
    return cur.lastrowid


def _readings(conn, sid, filas):
    for ts_, rpm, speed, temp in filas:
        conn.execute(
            "INSERT INTO readings (session_id, timestamp, rpm, speed, coolant_temp, maf) "
            "VALUES (?,?,?,?,?,5.0)", (sid, ts_, rpm, speed, temp))
    conn.commit()


def _positions(conn, sid, filas):
    for ts_, lat, lon in filas:
        conn.execute(
            "INSERT INTO positions (session_id, timestamp, lat, lon) VALUES (?,?,?,?)",
            (sid, ts_, lat, lon))
    conn.commit()


def _tips(conn, sid):
    return [r[0] for r in conn.execute(
        "SELECT message FROM alerts WHERE session_id=? AND category IN "
        "('conduccion','mantenimiento','uso')", (sid,))]


def _row(conn, sid):
    return conn.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()


def test_recalc_corrige_metricas_de_viaje_cerrado(cfg):
    """Un viaje cerrado con métricas viejas (0 km, 1 min) se rehace desde sus datos."""
    ts, _, conn = cfg
    sid = _session(conn, "2026-09-12T11:03:20", "2026-09-12T11:05:00",
                   distance_km=0.0, max_speed=0.0, avg_speed=0.0, driving_minutes=1)
    _readings(conn, sid, [
        ("2026-09-12T11:03:20", 1500, 30, 70),
        ("2026-09-12T11:20:00", 2600, 127, 80),
        ("2026-09-12T11:40:00", 2100, 90, 78),
    ])
    _positions(conn, sid, [
        ("2026-09-12T11:03:20", 43.3600, -5.8500),
        ("2026-09-12T11:40:00", 43.4000, -5.9000),
    ])

    r = ts.recalc_session(conn, sid)
    row = _row(conn, sid)
    assert row["max_speed"] == 127.0
    assert row["distance_km"] > 4.0
    assert row["driving_minutes"] == 36          # 11:03:20 → 11:40:00
    assert row["status"] == "completed"
    assert r["max_speed"] == 127.0 and r["antes"]["max_speed"] == 0.0
    avisos = _tips(conn, sid)
    assert any("127" in m for m in avisos)        # velocidad alta: aviso al día
    assert not any("Trayecto corto" in m for m in avisos)


def test_recalcular_dos_veces_no_acumula_avisos(cfg):
    """El recálculo es idempotente: los avisos se regeneran, no se suman."""
    ts, _, conn = cfg
    sid = _session(conn, "2026-09-12T11:03:20", "2026-09-12T11:40:00",
                   distance_km=4.0, max_speed=127.0, avg_speed=80.0, driving_minutes=36)
    _readings(conn, sid, [
        ("2026-09-12T11:03:20", 1500, 30, 70),
        ("2026-09-12T11:20:00", 2600, 127, 80),
        ("2026-09-12T11:40:00", 2100, 90, 78),
    ])
    ts.recalc_session(conn, sid)
    primeros = _tips(conn, sid)
    ts.recalc_session(conn, sid)
    assert _tips(conn, sid) == primeros
    assert len(primeros) >= 1


def test_fusion_recalcula_max_speed_y_limpia_avisos_del_tramo(cfg):
    """Fusionar dos tramos no puede dejar la velocidad máxima del primero.

    Caso real (12-sep): 168 (11:03→12:09) + 169 (12:20→12:48) fusionadas por el
    cron quedaron con máx 42 km/h cuando las lecturas del segundo tramo llegan a
    127, y con el aviso «Trayecto corto (1 min)» del tramo viejo.
    """
    ts, mg, conn = cfg
    a = _session(conn, "2026-09-12T11:03:20", "2026-09-12T12:09:06",
                 distance_km=4.0, max_speed=42.0, avg_speed=25.0, driving_minutes=65)
    _readings(conn, a, [("2026-09-12T11:03:20", 1400, 42, 70),
                        ("2026-09-12T11:40:00", 1500, 20, 72)])
    _positions(conn, a, [("2026-09-12T11:03:20", 43.3600, -5.8500),
                         ("2026-09-12T12:09:06", 43.3700, -5.8600)])
    b = _session(conn, "2026-09-12T12:20:24", "2026-09-12T12:48:36",
                 distance_km=0.0, max_speed=127.0, avg_speed=90.0, driving_minutes=28)
    _readings(conn, b, [("2026-09-12T12:20:24", 1300, 13, 74),
                        ("2026-09-12T12:34:30", 2642, 127, 77),
                        ("2026-09-12T12:48:36", 2100, 100, 76)])
    _positions(conn, b, [("2026-09-12T12:20:24", 43.3750, -5.8650)])
    conn.execute(
        "INSERT INTO alerts (session_id, timestamp, category, severity, message) "
        "VALUES (?,?,?,?,?)",
        (b, "2026-09-12T12:48:36", "uso", "info",
         "Trayecto corto (1 min). Los motores necesitan trayectos más largos "
         "para alcanzar temperatura óptima."))
    conn.commit()

    mg.main()

    assert conn.execute("SELECT COUNT(*) FROM sessions WHERE id=?",
                        (b,)).fetchone()[0] == 0        # absorbida
    row = _row(conn, a)
    assert row["max_speed"] == 127.0                    # ← el fix
    assert row["distance_km"] == 4.0                    # suma de tramos
    assert row["driving_minutes"] == 93                 # 65 + 28
    assert row["end_time"] == "2026-09-12T12:48:36"
    avisos = _tips(conn, a)
    assert not any("Trayecto corto" in m for m in avisos)
    assert any("127" in m for m in avisos)


def test_fusion_no_recomputa_distancia_cruzando_el_hueco(cfg):
    """`keep_aggregates`: la distancia es la suma de los tramos, no el salto del hueco.

    Entre el fin de un tramo y el inicio del siguiente hay 11 min sin lecturas;
    recomputar la distancia sobre las posiciones fusionadas añadiría una recta
    que el GPS no midió.
    """
    ts, mg, conn = cfg
    a = _session(conn, "2026-09-11T09:00:00", "2026-09-11T09:30:00",
                 distance_km=4.0, max_speed=50.0, avg_speed=30.0, driving_minutes=30)
    _readings(conn, a, [("2026-09-11T09:00:00", 1500, 40, 70),
                        ("2026-09-11T09:30:00", 1500, 20, 72)])
    _positions(conn, a, [("2026-09-11T09:00:00", 43.3600, -5.8500),
                         ("2026-09-11T09:30:00", 43.3900, -5.8500)])
    b = _session(conn, "2026-09-11T09:41:00", "2026-09-11T10:00:00",
                 distance_km=0.0, max_speed=45.0, avg_speed=25.0, driving_minutes=19)
    _readings(conn, b, [("2026-09-11T09:41:00", 1400, 30, 71),
                        ("2026-09-11T10:00:00", 1500, 10, 73)])
    # Única posición de B: 1,1 km al norte del fin de A (dentro del umbral de 2 km
    # de continuidad geográfica, para que la fusión tenga lugar).
    _positions(conn, b, [("2026-09-11T09:41:00", 43.4000, -5.8500)])

    mg.main()

    row = _row(conn, a)
    assert row["distance_km"] == 4.0, "la distancia debe ser la suma, no la recta del hueco"
    # La velocidad máxima sale de las lecturas fusionadas (máx 40), no del valor
    # almacenado antes de la fusión (50): el recálculo también aplica aquí.
    assert row["max_speed"] == 40.0
