"""Subida incremental a Cassiopeia (delta + marca de agua).

Antes se subía la BD entera cada ~10 min (3,7 MB, ~25 s, +0,7 MB/semana). Ahora
se sube solo lo posterior a la marca de agua, con 1 h de solape.

La propiedad que estos tests protegen: **la marca de agua solo avanza si el sync
tuvo éxito**. Si avanza antes, los datos de un envío fallido se perderían para
siempre.
"""
import importlib.util
import os
import sqlite3

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
COLLECTOR_DIR = os.path.dirname(HERE)
SCHEMA_LOCAL = os.path.join(HERE, "schema_local.sql")


class _Result:
    def __init__(self, rc=0, stderr=""):
        self.returncode = rc
        self.stderr = stderr
        self.stdout = ""


def _load():
    path = os.path.join(COLLECTOR_DIR, "obd_local_collector.py")
    spec = importlib.util.spec_from_file_location("under_test_incr", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def coll(tmp_path, monkeypatch):
    db = tmp_path / "obd_local.db"
    conn = sqlite3.connect(db)
    with open(SCHEMA_LOCAL) as fh:
        conn.executescript(fh.read())
    conn.close()
    mod = _load()
    monkeypatch.setattr(mod, "DB_PATH", str(db))
    monkeypatch.setattr(mod, "WATERMARK_PATH", str(tmp_path / "last_synced_ts"))
    monkeypatch.setattr(mod, "LOG_PATH", str(tmp_path / "obd_local.log"))
    return mod, str(db)


def _add_reading(db, ts):
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO readings (timestamp, rpm, speed, coolant, throttle, "
                 "intake, fuel, maf, voltage) VALUES (?,800,0,70,20,25,0,5.0,14.0)",
                 (ts,))
    conn.commit()
    conn.close()


def _add_position(db, ts):
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO positions (timestamp, lat, lon, alt, speed, bearing, "
                 "accuracy, provider) VALUES (?,43.3,-5.8,100,0,0,10,'gps')", (ts,))
    conn.commit()
    conn.close()


def _add_can(db, ts):
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO can_readings (ts, consumption_l100, range_km, "
                 "odometer_km) VALUES (?,5.5,400,6553)", (ts,))
    conn.commit()
    conn.close()


def _add_dtc(db, code):
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO dtc (timestamp, code, description, kind) "
                 "VALUES ('2027-01-01T10:00:00',?,'','stored')", (code,))
    conn.commit()
    conn.close()


def _counts(snapshot):
    conn = sqlite3.connect(snapshot)
    out = {}
    for t in ("readings", "positions", "can_readings", "dtc", "calibration"):
        try:
            out[t] = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        except sqlite3.OperationalError:
            out[t] = None
    conn.close()
    return out


# ── build_snapshot ────────────────────────────────────────────────────

def test_sin_marca_sube_la_bd_completa(coll):
    mod, db = coll
    for h in (10, 11, 12):
        _add_reading(db, f"2027-01-01T{h}:00:00")
        _add_position(db, f"2027-01-01T{h}:00:00")
        _add_can(db, f"2027-01-01T{h}:00:00")
    _add_dtc(db, "P0113")

    snap = mod.build_snapshot(None)

    c = _counts(snap)
    assert c["readings"] == 3 and c["positions"] == 3 and c["can_readings"] == 3
    assert c["dtc"] == 1
    os.remove(snap)


def test_delta_solo_trae_lo_nuevo(coll):
    mod, db = coll
    for h in (10, 11, 12, 13):
        _add_reading(db, f"2027-01-01T{h}:00:00")
        _add_position(db, f"2027-01-01T{h}:00:00")
        _add_can(db, f"2027-01-01T{h}:00:00")
    _add_dtc(db, "P0113")

    snap = mod.build_snapshot("2027-01-01T11:00:00")

    c = _counts(snap)
    assert c["readings"] == 2, c          # 12:00 y 13:00
    assert c["positions"] == 2 and c["can_readings"] == 2
    assert c["dtc"] == 1, "las tablas pequeñas van enteras (upsert por clave)"
    conn = sqlite3.connect(snap)
    ts = [r[0] for r in conn.execute("SELECT timestamp FROM readings ORDER BY timestamp")]
    conn.close()
    assert ts == ["2027-01-01T12:00:00", "2027-01-01T13:00:00"]
    os.remove(snap)


def test_delta_sin_novedades_queda_vacio(coll):
    mod, db = coll
    _add_reading(db, "2027-01-01T10:00:00")
    snap = mod.build_snapshot("2027-01-01T23:00:00")
    assert _counts(snap)["readings"] == 0
    assert mod.snapshot_max_ts(snap) is None
    os.remove(snap)


def test_marca_de_agua_es_el_ts_mas_nuevo(coll):
    mod, db = coll
    _add_reading(db, "2027-01-01T10:00:00")
    _add_can(db, "2027-01-01T12:30:00")   # el más nuevo está en otra tabla
    snap = mod.build_snapshot(None)
    assert mod.snapshot_max_ts(snap) == "2027-01-01T12:30:00"
    os.remove(snap)


def test_solape_se_aplica_a_la_marca(coll):
    mod, _ = coll
    # marca 12:00, solape 60 min → corte 11:00
    assert mod._desde_solape("2027-01-01T12:00:00") == "2027-01-01T11:00:00"
    assert mod._desde_solape(None) is None
    assert mod._desde_solape("basura") is None, "marca ilegible → copia completa"


# ── sync_to_cassiopeia: la marca solo avanza con éxito ────────────────

def _subida_ok(mod, monkeypatch):
    """Simula scp + rename correctos y captura el fichero subido."""
    llamadas = []

    def fake_run(cmd, **kw):
        llamadas.append(cmd)
        return _Result(0)

    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    monkeypatch.setattr(mod, "CASSIOPEIA", "cassiopeia")
    monkeypatch.setattr(mod, "INCOMING_PATH", "/dest/polar_obd_local.db")
    return llamadas


def test_sync_ok_avanza_la_marca(coll, monkeypatch):
    mod, db = coll
    _add_reading(db, "2027-01-01T10:00:00")
    _subida_ok(mod, monkeypatch)

    assert mod.sync_to_cassiopeia() is True
    assert mod.read_synced_watermark() == "2027-01-01T10:00:00"


def test_scp_fallido_NO_avanza_la_marca(coll, monkeypatch):
    """Lo crítico: si la subida falla, la marca se queda atrás y los datos se
    reenvían. Si avanzara, ese tramo se perdería para siempre."""
    mod, db = coll
    _add_reading(db, "2027-01-01T10:00:00")
    monkeypatch.setattr(mod.subprocess, "run", lambda cmd, **kw: _Result(255, "timeout"))
    monkeypatch.setattr(mod, "CASSIOPEIA", "cassiopeia")
    monkeypatch.setattr(mod, "INCOMING_PATH", "/dest/x.db")

    assert mod.sync_to_cassiopeia() is False
    assert mod.read_synced_watermark() is None, "la marca no debe avanzar sin subir"


def test_rename_fallido_NO_avanza_la_marca(coll, monkeypatch):
    mod, db = coll
    _add_reading(db, "2027-01-01T10:00:00")
    seq = [_Result(0), _Result(1, "mv: denied")]
    monkeypatch.setattr(mod.subprocess, "run",
                        lambda cmd, **kw: seq.pop(0) if seq else _Result(0))
    monkeypatch.setattr(mod, "CASSIOPEIA", "cassiopeia")
    monkeypatch.setattr(mod, "INCOMING_PATH", "/dest/x.db")

    assert mod.sync_to_cassiopeia() is False
    assert mod.read_synced_watermark() is None


def test_el_snapshot_temporal_se_borra_siempre(coll, monkeypatch):
    mod, db = coll
    _add_reading(db, "2027-01-01T10:00:00")
    monkeypatch.setattr(mod.subprocess, "run", lambda cmd, **kw: _Result(255, "x"))
    monkeypatch.setattr(mod, "CASSIOPEIA", "cassiopeia")
    monkeypatch.setattr(mod, "INCOMING_PATH", "/dest/x.db")

    mod.sync_to_cassiopeia()
    assert not os.path.exists(mod.DB_PATH + ".sync")


# ── la propiedad que importa: dos deltas seguidos no dejan huecos ─────

def test_dos_syncs_consecutivos_no_pierden_ninguna_fila(coll, monkeypatch):
    mod, db = coll
    _add_reading(db, "2027-01-01T10:00:00")
    _subida_ok(mod, monkeypatch)
    assert mod.sync_to_cassiopeia() is True

    # Llegan datos nuevos entre syncs
    _add_reading(db, "2027-01-01T10:00:30")
    _add_reading(db, "2027-01-01T14:00:00")   # viaje distinto, 4 h después

    snap = mod.build_snapshot(mod._desde_solape(mod.read_synced_watermark()))
    conn = sqlite3.connect(snap)
    ts = [r[0] for r in conn.execute("SELECT timestamp FROM readings ORDER BY timestamp")]
    conn.close()
    os.remove(snap)

    assert "2027-01-01T10:00:30" in ts and "2027-01-01T14:00:00" in ts, \
        "el segundo sync debe traer todo lo posterior a la marca"
    assert "2027-01-01T10:00:00" in ts, "con solape, la fila de la marca se reenvía"


# ── Cadencia del sync: por TIEMPO, no por ciclos (fix 2026-09-19) ────────────
#
# El sync estaba condicionado a `counter % 20 == 0` y el contador se reinicia en
# cada arranque del recolector. Con la cadencia adaptativa hacían falta 40 min
# para juntar 20 ciclos: más de lo que vive el proceso (202 arranques en el log).
# Resultado real: desde el 17/09 no se intentaba NINGÚN sync y los viajes
# dejaron de llegar a Cassiopeia sin un solo error en el log.

def test_al_arrancar_se_intenta_un_sync():
    """ultimo_sync=0 ⇒ el primer ciclo sube: así un reinicio no deja días sin subir."""
    mod = _load()
    assert mod.toca_sync(0, 1_700_000_000) is True


def test_no_sincroniza_antes_del_plazo():
    mod = _load()
    ahora = 1_700_000_000
    assert mod.toca_sync(ahora - 9 * 60, ahora) is False


def test_sincroniza_al_cumplir_el_plazo():
    mod = _load()
    ahora = 1_700_000_000
    assert mod.toca_sync(ahora - mod.SYNC_EVERY_MIN * 60, ahora) is True
    assert mod.toca_sync(ahora - 60 * 60, ahora) is True   # muy pasado: también


def test_el_plazo_es_de_minutos_no_de_ciclos():
    mod = _load()
    assert mod.SYNC_EVERY_MIN == 10
    assert mod.SYNC_EVERY_MIN * 60 < mod.IDLE_INTERVAL * mod.SYNC_EVERY, \
        "el sync debe llegar antes de agotar los ciclos que antes hacían falta"


def test_el_bucle_ya_no_cuenta_ciclos_para_sincronizar():
    fuente = open(os.path.join(COLLECTOR_DIR, "obd_local_collector.py")).read()
    assert "if toca_sync(ultimo_sync, time.time()):" in fuente
    # El contador de ciclos ya solo gobierna los DTCs, no el sync
    assert "with_dtcs = (counter % SYNC_EVERY == 0)" in fuente
    # ...y que no quede ningún sync colgado del contador de ciclos
    assert "if counter % SYNC_EVERY == 0:" not in fuente
