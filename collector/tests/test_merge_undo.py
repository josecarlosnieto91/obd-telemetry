"""Regresión del arreglo de 2026-09-19: la fusión de sesiones deja copia de deshacer.

`merge_sessions` une tramos del mismo viaje y BORRA la sesión absorbida tras mover sus
lecturas. Las heurísticas (hueco + continuidad GPS + geografía) pueden equivocarse — el
propio módulo documenta el caso de "22 km a 120 km/h en 11 min" y los pares fantasma
152/160 — y el job corre cada 5 minutos: un falso positivo se llevaba por delante un viaje
real sin dejar rastro.

Estos tests fijan el contrato: antes del DELETE, la fila completa de la sesión absorbida
queda en `sessions_merged_undo` y es reconstruible con un INSERT del payload.
"""
import importlib.util
import json
import os
import sqlite3

HERE = os.path.dirname(os.path.abspath(__file__))
COLLECTOR_DIR = os.path.dirname(HERE)
SCHEMA_TARGET = os.path.join(HERE, "schema.sql")


def _load(name):
    path = os.path.join(COLLECTOR_DIR, name)
    spec = importlib.util.spec_from_file_location(f"under_test_{name}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _db_con_sesiones(tmp_path, gap_min):
    """Dos sesiones completadas separadas `gap_min` minutos, con lecturas a cada lado."""
    db = str(tmp_path / "obd.db")
    conn = sqlite3.connect(db)
    with open(SCHEMA_TARGET) as fh:
        conn.executescript(fh.read())
    conn.execute(
        "INSERT INTO sessions (id, start_time, end_time, distance_km, max_speed, avg_speed, "
        "max_rpm, driving_minutes, status) VALUES "
        "(1,'2026-09-19T10:00:00','2026-09-19T10:20:00',5.0,80,60,2000,20,'completed')")
    fin_a = "10:20"
    # inicio de la segunda sesión
    h, m = (10, 20 + gap_min) if 20 + gap_min < 60 else (11, (20 + gap_min) - 60)
    inicio_b = f"{h:02d}:{m:02d}"
    fin_b_h, fin_b_m = (h, m + 20) if m + 20 < 60 else (h + 1, (m + 20) - 60)
    conn.execute(
        "INSERT INTO sessions (id, start_time, end_time, distance_km, max_speed, avg_speed, "
        "max_rpm, driving_minutes, status) VALUES "
        f"(2,'2026-09-19T{inicio_b}:00','2026-09-19T{fin_b_h:02d}:{fin_b_m:02d}:00',4.0,90,65,"
        "2100,20,'completed')")
    for sid, base in ((1, "10:00"), (2, inicio_b)):
        hh, mm = base.split(":")
        for i in range(0, 20, 2):
            conn.execute(
                "INSERT INTO readings (session_id, timestamp, rpm, speed, coolant_temp, "
                "engine_load, intake_temp, throttle_pos, fuel_level, voltage, maf, map) "
                "VALUES (?,?,2000,60,90,30,25,30,40,14.0,5.0,1000)",
                (sid, f"2026-09-19T{hh}:{int(mm) + i:02d}:00"))
    conn.commit()
    conn.close()
    assert fin_a  # el primer tramo acaba siempre a las 10:20
    return db


def _ejecutar(tmp_path, monkeypatch, gap_min):
    db = _db_con_sesiones(tmp_path, gap_min)
    ms = _load("merge_sessions.py")
    monkeypatch.setattr(ms, "OBD_DB", db)
    # Janus no forma parte de este contrato (toca la BD de contexto real)
    monkeypatch.setattr(ms, "consolidar_janus",
                        lambda: {"actualizar": [], "borrar": [], "insertar": [],
                                 "dudosas": [], "anidadas": []})
    ms.main()
    return sqlite3.connect(db)


def test_fusion_guarda_copia_de_la_sesion_absorbida(tmp_path, monkeypatch):
    conn = _ejecutar(tmp_path, monkeypatch, gap_min=5)

    # La fusión ocurrió: queda una sola sesión, con los datos sumados
    sesiones = conn.execute("SELECT id, start_time, end_time, distance_km, driving_minutes "
                            "FROM sessions ORDER BY id").fetchall()
    assert [s[0] for s in sesiones] == [1]
    assert sesiones[0][3] == 9.0 and sesiones[0][4] == 40

    # Y la fila borrada está guardada entera, no solo el id
    undo = conn.execute("SELECT dropped_id, kept_id, gap_min, n_readings, payload "
                        "FROM sessions_merged_undo").fetchall()
    assert len(undo) == 1
    dropped_id, kept_id, gap, n_readings, payload = undo[0]
    assert (dropped_id, kept_id) == (2, 1)
    assert gap == 5.0 and n_readings == 10

    fila = json.loads(payload)
    assert fila["id"] == 2
    assert fila["start_time"] == "2026-09-19T10:25:00"
    assert fila["end_time"] == "2026-09-19T10:45:00"
    assert fila["distance_km"] == 4.0 and fila["driving_minutes"] == 20
    assert fila["status"] == "completed"

    # Reconstruible: el payload es un INSERT válido (así se deshace un falso positivo)
    cols = ", ".join(fila.keys())
    placeholders = ", ".join("?" * len(fila))
    conn.execute(f"INSERT OR REPLACE INTO sessions ({cols}) VALUES ({placeholders})",
                 list(fila.values()))
    conn.commit()
    restaurada = conn.execute("SELECT start_time, end_time, distance_km FROM sessions "
                              "WHERE id=2").fetchone()
    assert restaurada == ("2026-09-19T10:25:00", "2026-09-19T10:45:00", 4.0)


def test_sin_fusion_no_se_escribe_en_la_tabla_de_deshacer(tmp_path, monkeypatch):
    # Hueco de 60 min (> MERGE_GAP_MINUTES): son dos viajes distintos
    conn = _ejecutar(tmp_path, monkeypatch, gap_min=60)
    assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM sessions_merged_undo").fetchone()[0] == 0
