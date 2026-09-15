"""Tests de `trip_summary.real_consumption` contra una BD temporal.

Regresión del bug REAL detectado el 15/09/2026: sin filtrar los repostajes
anteriores al viaje, un viaje del 13/09 cogía el llenado del 15/09 (posterior a
él) y el consumo salía **39 l/100km**.
"""
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import trip_summary as ts  # noqa: E402

KM_PER_L = 15.537


def _db(tmp_path):
    """BD mínima con lo que consulta real_consumption."""
    conn = sqlite3.connect(str(tmp_path / "t.db"))
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("CREATE TABLE refuels (id INTEGER PRIMARY KEY, ts TEXT, liters REAL,"
              " fuel_after REAL, full_tank INTEGER)")
    c.execute("CREATE TABLE can_readings (id INTEGER PRIMARY KEY, ts TEXT, range_km REAL)")
    c.execute("CREATE TABLE sessions (id INTEGER PRIMARY KEY, start_time TEXT,"
              " end_time TEXT, distance_km REAL)")
    return conn, c


def _escenario(tmp_path):
    """Dos llenados (01/09 y 15/09) y viajes repartidos entre ellos."""
    conn, c = _db(tmp_path)
    c.execute("INSERT INTO refuels VALUES (1,'2026-09-01T12:49:39',44.1,920,1)")
    c.execute("INSERT INTO refuels VALUES (2,'2026-09-15T10:22:26',55.03,920,1)")
    # viajes acumulados entre el llenado del 01/09 y el del 15/09 (600 km)
    c.execute("INSERT INTO sessions VALUES (10,'2026-09-10T09:00:00','2026-09-10T10:00:00',300.0)")
    c.execute("INSERT INTO sessions VALUES (11,'2026-09-12T09:00:00','2026-09-12T10:00:00',300.0)")
    # el viaje que se está cerrando el 13/09 (id 12), aún sin distance_km en BD
    c.execute("INSERT INTO sessions VALUES (12,'2026-09-13T20:00:00',NULL,NULL)")
    # viaje posterior al llenado del 15/09
    c.execute("INSERT INTO sessions VALUES (13,'2026-09-15T11:00:00','2026-09-15T11:10:00',5.0)")
    c.execute("INSERT INTO can_readings VALUES (1,'2026-09-13T20:30:00',105.0)")
    c.execute("INSERT INTO can_readings VALUES (2,'2026-09-15T11:10:00',915.0)")
    conn.commit()
    return conn, c


def test_viaje_anterior_al_ultimo_llenado_no_usa_ese_llenado(tmp_path):
    """El bug: un viaje del 13/09 NO puede usar el repostaje del 15/09."""
    conn, c = _escenario(tmp_path)
    # viaje 12: 133,5 km, cierra el 13/09 con rango 105
    real, etiqueta = ts.real_consumption(c, "2026-09-13T21:22:46", KM_PER_L, 12, 133.5)
    assert real is not None
    # caída desde el llenado del 01/09 (920 → 105) = 815 km de rango
    # litros = 815/15.537 = 52,5 L · km = 300+300+133,5 = 733,5 → 7,2 l/100km
    assert real == pytest.approx(100 * (815 / KM_PER_L) / 733.5, abs=0.1)
    assert real < 10, "con el llenado posterior daba 39 l/100km"
    assert "depósito en curso" in (etiqueta or "")


def test_recien_repostado_usa_el_ultimo_deposito_completo(tmp_path):
    """Tras repostar, con 5 km hechos, la cifra del depósito no dice nada."""
    conn, c = _escenario(tmp_path)
    real, etiqueta = ts.real_consumption(c, "2026-09-15T11:10:00", KM_PER_L, 13, 5.0)
    # km entre llenados = 600 + 133,5 (el que cerró el 13/09) ... solo los que
    # tienen distance_km: 300+300+5 → la cifra sale de esos
    assert etiqueta == "último depósito completo"
    assert real is not None and real < 20


def test_sin_repostajes_no_inventa_nada(tmp_path):
    conn, c = _db(tmp_path)
    c.execute("INSERT INTO can_readings VALUES (1,'2026-09-13T20:30:00',105.0)")
    conn.commit()
    assert ts.real_consumption(c, "2026-09-13T21:00:00", KM_PER_L, 1, 50.0) == (None, None)


def test_sin_rango_can_no_inventa_nada(tmp_path):
    conn, c = _db(tmp_path)
    c.execute("INSERT INTO refuels VALUES (1,'2026-09-01T12:49:39',44.1,920,1)")
    conn.commit()
    assert ts.real_consumption(c, "2026-09-13T21:00:00", KM_PER_L, 1, 50.0) == (None, None)


def test_no_cuenta_dos_veces_el_viaje_que_se_cierra(tmp_path):
    """El viaje en curso no tiene distance_km: se suma `dist` una sola vez."""
    conn, c = _escenario(tmp_path)
    real_1, _ = ts.real_consumption(c, "2026-09-13T21:22:46", KM_PER_L, 12, 133.5)
    real_2, _ = ts.real_consumption(c, "2026-09-13T21:22:46", KM_PER_L, 12, 133.5)
    assert real_1 == real_2
    # y con la mitad de distancia el consumo sube (no se ignora `dist`)
    real_3, _ = ts.real_consumption(c, "2026-09-13T21:22:46", KM_PER_L, 12, 66.75)
    assert real_3 is not None and real_1 is not None
    assert real_3 > real_1
