"""Consolidación de `context.trips` (Janus) contra las sesiones reales.

Contexto real medido: un viaje fusionado deja en Janus las filas de los tramos
absorbidos (13/09: 0,86+49,18+78,37+5,06 = 133,47 km, la sesión 177), así que el
panel/consola los enseñaba como viajes sueltos y troceados. Aquí se prueba que la
consolidación deja UNA fila por sesión con sus valores, y que lo que no encaja en
ninguna sesión NO se borra.
"""
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import trip_summary as ts  # noqa: E402


@pytest.fixture
def bds(tmp_path, monkeypatch):
    """context.db + obd_telemetry.db con el caso real del 13/09 y uno de relleno."""
    monkeypatch.setattr(ts, "GEOCODE_CACHE", str(tmp_path / "geo.json"))
    monkeypatch.setattr(ts, "GEOCODE_MIN_INTERVAL", 0)    # sin esperas reales
    monkeypatch.setattr(ts, "reverse_geocode", lambda lat, lon: "Oviedo")
    obd = sqlite3.connect(str(tmp_path / "obd.db"))
    obd.execute("CREATE TABLE sessions (id INTEGER PRIMARY KEY, start_time TEXT,"
                " end_time TEXT, distance_km REAL, driving_minutes INTEGER)")
    obd.execute("CREATE TABLE positions (id INTEGER PRIMARY KEY, session_id INTEGER,"
                " timestamp TEXT, lat REAL, lon REAL)")
    # sesión fusionada (4 tramos) y otra que no tiene fila en Janus
    obd.execute("INSERT INTO sessions VALUES (177,'2026-09-13T17:38:07',"
                "'2026-09-13T21:22:46',133.5,224)")
    obd.execute("INSERT INTO sessions VALUES (99,'2026-09-10T08:00:00',"
                "'2026-09-10T08:30:00',12.0,30)")
    for i, ts_ in enumerate(("2026-09-13T17:38:07", "2026-09-13T21:22:46"), start=1):
        obd.execute("INSERT INTO positions VALUES (?,?,?,?,?)",
                    (i, 177, ts_, 43.36 + i / 100, -5.85 + i / 100))
    obd.commit()
    obd.close()

    ctx = sqlite3.connect(str(tmp_path / "ctx.db"))
    ctx.execute("CREATE TABLE trips (id INTEGER PRIMARY KEY, date TEXT, start_time TEXT,"
                " end_time TEXT, start_place TEXT, end_place TEXT, distance_km REAL,"
                " duration_min INTEGER)")
    for fila in ((163, "2026-09-13", "2026-09-13T17:38:07", "2026-09-13T17:44:51", 0.86, 6),
                 (164, "2026-09-13", "2026-09-13T17:46:05", "2026-09-13T19:37:49", 49.18, 111),
                 (165, "2026-09-13", "2026-09-13T19:38:50", "2026-09-13T21:12:48", 78.37, 93),
                 (166, "2026-09-13", "2026-09-13T21:13:56", "2026-09-13T21:22:46", 5.06, 8),
                 # dudosa: no la cubre ninguna sesión y no casa con ninguna
                 (999, "2026-09-01", "2026-09-01T10:00:00", "2026-09-01T10:00:00", 5.0, 1)):
        ctx.execute("INSERT INTO trips VALUES (?,?,?,?,NULL,NULL,?,?)", fila)
    ctx.commit()
    ctx.close()
    return str(tmp_path / "obd.db"), str(tmp_path / "ctx.db")


def test_dry_run_no_toca_nada(bds):
    obd, ctx = bds
    plan = ts.consolidar_janus(obd_db=obd, ctx_db=ctx, dry_run=True)
    assert len(plan["actualizar"]) == 1     # la fila 163 (casa por start_time)
    assert len(plan["insertar"]) == 1       # la sesión 99 no tiene fila
    assert sorted(plan["borrar"]) == [164, 165, 166]
    assert plan["dudosas"] == [999]
    c = sqlite3.connect(ctx)
    assert c.execute("SELECT COUNT(*) FROM trips").fetchone()[0] == 5   # intacto
    c.close()


def test_consolida_una_fila_por_sesion(bds):
    obd, ctx = bds
    ts.consolidar_janus(obd_db=obd, ctx_db=ctx, dry_run=False)
    c = sqlite3.connect(ctx)
    c.row_factory = sqlite3.Row
    filas = c.execute("SELECT * FROM trips ORDER BY start_time").fetchall()
    c.close()
    assert len(filas) == 3, "2 filas de sesión real (fusionada + insertada) + la dudosa"
    fus = [f for f in filas if f["start_time"] == "2026-09-13T17:38:07"][0]
    # la fila del tramo (0,86 km) pasa a ser la del viaje fusionado
    assert fus["distance_km"] == pytest.approx(133.5)
    assert fus["duration_min"] == 224
    assert fus["end_time"] == "2026-09-13T21:22:46"
    assert fus["start_place"] == "Oviedo" and fus["end_place"] == "Oviedo"
    # y la sesión que no tenía fila, la tiene
    nueva = [f for f in filas if f["start_time"] == "2026-09-10T08:00:00"]
    assert nueva and nueva[0]["distance_km"] == pytest.approx(12.0)


def test_no_borra_lo_que_no_encaja(bds):
    """La dudosa (sin sesión que la cubra) se queda y se avisa."""
    obd, ctx = bds
    ts.consolidar_janus(obd_db=obd, ctx_db=ctx, dry_run=False)
    c = sqlite3.connect(ctx)
    dudosa = c.execute("SELECT distance_km FROM trips WHERE id=999").fetchone()
    c.close()
    assert dudosa == (5.0,)


def test_los_km_cuadran_con_las_sesiones(bds):
    """Tras consolidar, los km de Janus son los de las sesiones (no la suma de tramos)."""
    obd, ctx = bds
    ts.consolidar_janus(obd_db=obd, ctx_db=ctx, dry_run=False)
    c = sqlite3.connect(ctx)
    c.row_factory = sqlite3.Row
    # solo las filas que casan con una sesión
    o = sqlite3.connect(obd)
    ses = {r[0]: r[1] for r in o.execute("SELECT start_time, distance_km FROM sessions")}
    total = sum(r["distance_km"] for r in c.execute("SELECT start_time, distance_km FROM trips")
                if r["start_time"] in ses)
    c.close()
    o.close()
    assert total == pytest.approx(145.5)   # 133.5 + 12.0, no 133.47+0.86+...


def test_no_inventa_fila_para_una_sesion_anidada(tmp_path, monkeypatch):
    """Una sesión DENTRO de otra (secuela de fusión: 48 dentro de la 43) no tiene
    fila propia: su tramo ya está contado en la de fuera. Si se le inventara, la
    siguiente pasada la borraría (se anularía a sí misma)."""
    monkeypatch.setattr(ts, "GEOCODE_CACHE", str(tmp_path / "geo.json"))
    monkeypatch.setattr(ts, "GEOCODE_MIN_INTERVAL", 0)
    monkeypatch.setattr(ts, "reverse_geocode", lambda lat, lon: "Oviedo")
    obd = sqlite3.connect(str(tmp_path / "obd.db"))
    obd.execute("CREATE TABLE sessions (id INTEGER PRIMARY KEY, start_time TEXT,"
                " end_time TEXT, distance_km REAL, driving_minutes INTEGER)")
    obd.execute("CREATE TABLE positions (id INTEGER PRIMARY KEY, session_id INTEGER,"
                " timestamp TEXT, lat REAL, lon REAL)")
    obd.execute("INSERT INTO sessions VALUES (43,'2026-08-12T12:32:09',"
                "'2026-08-12T12:47:21',9.83,10)")     # la de fuera
    obd.execute("INSERT INTO sessions VALUES (48,'2026-08-12T12:44:11',"
                "'2026-08-12T12:46:34',2.81,2)")      # la anidada
    obd.commit()
    obd.close()
    ctx = sqlite3.connect(str(tmp_path / "ctx.db"))
    ctx.execute("CREATE TABLE trips (id INTEGER PRIMARY KEY, date TEXT, start_time TEXT,"
                " end_time TEXT, start_place TEXT, end_place TEXT, distance_km REAL,"
                " duration_min INTEGER)")
    ctx.execute("INSERT INTO trips VALUES (52,'2026-08-12','2026-08-12T12:32:09',"
                "'2026-08-12T12:47:21',NULL,NULL,9.83,10)")
    ctx.commit()
    ctx.close()

    plan = ts.consolidar_janus(obd_db=str(tmp_path / "obd.db"),
                               ctx_db=str(tmp_path / "ctx.db"), dry_run=True)
    assert plan["anidadas"] == [48]
    assert plan["insertar"] == [], "no debe inventarle fila a la anidada"
    assert plan["borrar"] == [], "y no debe borrar la fila de la sesión de fuera"


def test_es_idempotente(bds):
    obd, ctx = bds
    ts.consolidar_janus(obd_db=obd, ctx_db=ctx, dry_run=False)
    plan = ts.consolidar_janus(obd_db=obd, ctx_db=ctx, dry_run=True)
    assert plan["borrar"] == [] and plan["insertar"] == []
    assert len(plan["actualizar"]) == 2
