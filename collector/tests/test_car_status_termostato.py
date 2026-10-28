"""Diagnóstico de termostato contra el dato del fabricante.

El C4 Grand Picasso I 2.0 HDi 138 (DW10BTED4) lleva un termostato que **empieza a
abrir a 83 °C y completa a 95 °C** (tabla PSA; ver docs/parametros-referencia-c4.md).
Temperatura de servicio: 85-92 °C.

Por qué importa: con el umbral antiguo (78 °C) el diagnóstico decía «OK» con 78 °C
medidos, y el aviso real quedaba enterrado. Este coche mide 73,8 °C de media en
crucero: por debajo del inicio de apertura, o sea, termostato que no cierra.
"""
import importlib.util
import json
import os
import sqlite3
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
COLLECTOR = os.path.dirname(HERE)


def _load():
    if "car_status" in sys.modules:
        return sys.modules["car_status"]
    sys.path.insert(0, COLLECTOR)
    spec = importlib.util.spec_from_file_location(
        "car_status", os.path.join(COLLECTOR, "car_status.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["car_status"] = mod
    spec.loader.exec_module(mod)
    return mod


def _bd(tmp_path, lecturas_crucero, temp_max_viaje=None, km_viaje=33.0):
    """BD mínima con lo que consulta get_diagnostics()."""
    db = tmp_path / "obd.db"
    con = sqlite3.connect(db)
    con.execute("""CREATE TABLE readings (
        id INTEGER PRIMARY KEY AUTOINCREMENT, session_id INTEGER, timestamp TEXT,
        rpm REAL, speed REAL, coolant_temp REAL, engine_load REAL, voltage REAL,
        maf REAL, map REAL)""")
    con.execute("""CREATE TABLE sessions (
        id INTEGER PRIMARY KEY, start_time TEXT, end_time TEXT, status TEXT,
        distance_km REAL)""")
    con.execute("CREATE TABLE calibration (key TEXT PRIMARY KEY, value REAL, n INTEGER, updated TEXT)")
    con.execute("CREATE TABLE dtc (id INTEGER PRIMARY KEY, code TEXT, cleared INTEGER)")
    for i, (vel, temp) in enumerate(lecturas_crucero):
        con.execute("INSERT INTO readings (session_id, timestamp, rpm, speed, coolant_temp, "
                    "voltage, maf, map) VALUES (1, ?, ?, ?, ?, ?, ?, ?)",
                    (f"2026-10-28T10:{i % 60:02d}:00", 2200, vel, temp, 14.2, 45.0, 128.0))
    if temp_max_viaje is not None:
        con.execute("INSERT INTO sessions VALUES (1, '2026-10-28T09:00:00', "
                    "'2026-10-28T10:00:00', 'completed', ?)", (km_viaje,))
        con.execute("INSERT INTO readings (session_id, timestamp, speed, coolant_temp) "
                    "VALUES (1, '2026-10-28T09:30:00', 60, ?)", (temp_max_viaje,))
    con.commit()
    con.close()
    return db


@pytest.fixture
def config(tmp_path, monkeypatch):
    """Config con los umbrales que usa el coche real."""
    cfg = tmp_path / "cfg.json"
    cfg.write_text(json.dumps({
        "vehicle": {"has_turbo": True, "unsupported_pids": []},
        "thresholds": {
            "thermostat_open_c": 83.0, "thermostat_full_open_c": 95.0,
            "thermostat_min_c": 85.0, "thermostat_cruise_min_c": 83.0,
            "thermostat_cruise_frias_pct": 50, "thermostat_trip_km": 20,
            "speed_cruise_kmh": 80, "speed_econ_kmh": 60,
            "battery_ok_v": 12.4, "battery_warn_v": 12.0,
            "map_cruise_min_kpa": 90, "idle_rpm_stddev_max": 40,
            "coolant_idle_sample_c": 70,
        },
        "maintenance": [],
    }))
    mod = _load()
    monkeypatch.setattr(mod, "CONFIG_PATH", str(cfg))
    return mod


def test_motor_que_no_cierra_el_termostato_da_warn(config, tmp_path):
    """30 lecturas de crucero a 75 °C: por debajo de los 83 de apertura."""
    db = _bd(tmp_path, [(110, 75.0)] * 30, temp_max_viaje=78.0)
    con = sqlite3.connect(db)   # BD temporal, NUNCA la de producción
    con.row_factory = sqlite3.Row
    diag = config.get_diagnostics(con)
    con.close()
    assert diag["thermostat"]["status"] == "warn", diag["thermostat"]
    detalle = diag["thermostat"]["detail"]
    assert "83" in detalle, f"no cita el punto de apertura del fabricante: {detalle}"


def test_motor_en_temperatura_de_servicio_no_avisa(config, tmp_path):
    """88 °C de crucero y 90 de máximo: dentro de 85-92, nada que avisar."""
    db = _bd(tmp_path, [(110, 88.0)] * 30, temp_max_viaje=90.0)
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    diag = config.get_diagnostics(con)
    con.close()
    assert diag["thermostat"]["status"] == "ok", diag["thermostat"]


def test_sin_muestras_suficientes_no_inventa(config, tmp_path):
    db = _bd(tmp_path, [(110, 75.0)] * 5)
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    diag = config.get_diagnostics(con)
    con.close()
    assert diag["thermostat"]["status"] == "nodata", diag["thermostat"]
