"""Regresión de la recuperación de datos del fichero marcado como corrupto.

Contexto (2026-09-16): si el importador abre el fichero entrante mientras el SCP
sigue subiéndolo, lo ve a medias y lo mueve a `processed/corrupt/` — pero el
fichero puede acabar COMPLETO, porque el SCP sigue escribiendo el mismo inodo.
Eso significa que `corrupt/` puede contener datos legítimos sin importar.

`obd_corrupt_recovery.py` agrupa los datos huérfanos en bloques de viaje (hueco >
60 min) y cuelga cada bloque de la sesión del ancla más cercana del maestro:
anterior al bloque a ≤60 min → posterior a ≤60 min → ancla DENTRO del bloque →
sesión nueva. Y corrige el `start_time` de las sesiones afectadas.

Fixtures de esquema (los mismos que usa el importador):
  - `schema.sql`       → BD destino de Cassiopeia (sessions, nombres nuevos).
  - `schema_local.sql` → BD de la tablet (sin sessions, nombres antiguos).
"""
import importlib.util
import os
import sqlite3
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
COLLECTOR_DIR = os.path.dirname(HERE)
SCHEMA_TARGET = os.path.join(HERE, "schema.sql")
SCHEMA_LOCAL = os.path.join(HERE, "schema_local.sql")


def _new_db(path, schema):
    conn = sqlite3.connect(path)
    with open(schema) as fh:
        conn.executescript(fh.read())
    return conn


def _script():
    """Carga el recuperador como módulo (los scripts del colector no son paquete)."""
    path = os.path.join(COLLECTOR_DIR, "obd_corrupt_recovery.py")
    spec = importlib.util.spec_from_file_location("under_test_recovery", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _seed_master(path, session, timestamps, positions=()):
    """Sesión del maestro con sus lecturas (y posiciones) ya importadas."""
    conn = sqlite3.connect(path)
    conn.execute("INSERT INTO sessions (id, start_time, status) VALUES (?,?,'completed')",
                 (session, timestamps[0]))
    conn.executemany(
        "INSERT INTO readings (session_id, timestamp, rpm, speed, coolant_temp, voltage) "
        "VALUES (?,?,800,50,80,14.0)", [(session, ts) for ts in timestamps])
    conn.executemany(
        "INSERT INTO positions (session_id, timestamp, lat, lon) VALUES (?,?,43.3,-5.9)",
        [(session, ts) for ts in positions])
    conn.commit()
    conn.close()


def _seed_tablet(path, timestamps, positions=()):
    """Lecturas/posiciones en el fichero de la tablet (nombres antiguos, sin session_id)."""
    conn = sqlite3.connect(path)
    conn.executemany(
        "INSERT INTO readings (timestamp, rpm, speed, coolant, voltage) VALUES (?,800,50,80,14.0)",
        [(ts,) for ts in timestamps])
    conn.executemany(
        "INSERT INTO positions (timestamp, lat, lon) VALUES (?,43.3,-5.9)",
        [(ts,) for ts in positions])
    conn.commit()
    conn.close()


def _rows(db, sql, args=()):
    conn = sqlite3.connect(db)
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


def _run(mod, src, apply=False):
    """Ejecuta el recuperador contra el maestro monkeypatcheado."""
    argv = ["obd_corrupt_recovery.py", str(src)] + (["--apply"] if apply else [])
    original, sys.argv = sys.argv, argv
    try:
        return mod.main()
    finally:
        sys.argv = original


@pytest.fixture
def rec(tmp_path, monkeypatch):
    """Recuperador con el maestro redirigido a tmp_path (nunca toca producción)."""
    master, src = tmp_path / "master.db", tmp_path / "tablet.db"
    _new_db(master, SCHEMA_TARGET).close()
    _new_db(src, SCHEMA_LOCAL).close()
    mod = _script()
    monkeypatch.setattr(mod, "MASTER", str(master))
    return mod, str(master), str(src)


def test_dry_run_no_toca_nada(rec):
    """Sin --apply solo informa: la BD maestra queda intacta."""
    mod, master, src = rec
    _seed_master(master, 1, ["2026-09-11T10:00:00"])
    _seed_tablet(src, ["2026-09-11T10:00:00", "2026-09-11T10:01:00"])

    assert _run(mod, src) == 0

    assert _rows(master, "SELECT COUNT(*) FROM readings") == [(1,)]
    assert _rows(master, "SELECT COUNT(*) FROM sessions") == [(1,)]


def test_bloque_que_continua_la_sesion_anterior(rec):
    """Un hueco de 30 s es el MISMO viaje: la fila huérfana va a la sesión 1."""
    mod, master, src = rec
    _seed_master(master, 1, ["2026-09-11T10:00:00", "2026-09-11T10:00:30"],
                 positions=["2026-09-11T10:00:00"])
    _seed_tablet(src, ["2026-09-11T10:00:00", "2026-09-11T10:00:30", "2026-09-11T10:01:00"],
                 positions=["2026-09-11T10:00:00", "2026-09-11T10:01:00"])

    _run(mod, src, apply=True)

    assert _rows(master, "SELECT session_id, timestamp FROM readings ORDER BY timestamp") == [
        (1, "2026-09-11T10:00:00"), (1, "2026-09-11T10:00:30"), (1, "2026-09-11T10:01:00")]
    assert _rows(master, "SELECT session_id, timestamp FROM positions ORDER BY timestamp") == [
        (1, "2026-09-11T10:00:00"), (1, "2026-09-11T10:01:00")]
    assert _rows(master, "SELECT COUNT(*) FROM sessions") == [(1,)], "no debe abrir viaje nuevo"


def test_ancla_dentro_del_bloque_reutiliza_su_sesion(rec):
    """Viaje cuyo principio ya estaba importado a medias: los datos que faltan
    van a ESA sesión, y su start_time se corrige a la primera lectura real."""
    mod, master, src = rec
    _seed_master(master, 5, ["2026-09-11T18:00:06"])          # ancla dentro del bloque
    _seed_tablet(src, ["2026-09-11T17:41:07", "2026-09-11T17:50:00",
                       "2026-09-11T18:00:06", "2026-09-11T18:32:43"])

    _run(mod, src, apply=True)

    assert _rows(master, "SELECT COUNT(*) FROM sessions") == [(1,)]
    assert _rows(master, "SELECT COUNT(*) FROM readings WHERE session_id=5") == [(4,)]
    assert _rows(master, "SELECT start_time FROM sessions WHERE id=5") == [
        ("2026-09-11T17:41:07",)]


def test_bloque_sin_ancla_abre_viaje_nuevo(rec):
    """6 h después del último dato no es continuación: sesión nueva con
    start_time = inicio del bloque."""
    mod, master, src = rec
    _seed_master(master, 1, ["2026-09-11T10:00:00"])
    _seed_tablet(src, ["2026-09-11T10:00:00", "2026-09-11T16:00:00", "2026-09-11T16:01:00"])

    _run(mod, src, apply=True)

    assert _rows(master, "SELECT id, start_time FROM sessions ORDER BY id") == [
        (1, "2026-09-11T10:00:00"), (2, "2026-09-11T16:00:00")]
    assert _rows(master, "SELECT COUNT(*) FROM readings WHERE session_id=2") == [(2,)]


def test_fichero_sin_tabla_positions_no_revienta(rec):
    """Ficheros antiguos (03-ago) no tienen `positions`: se omite la tabla y se
    recupera lo demás (antes: sqlite3.OperationalError y sin recuperar nada)."""
    mod, master, src = rec
    _seed_master(master, 1, ["2026-08-03T18:59:00"])
    conn = sqlite3.connect(src)
    conn.execute("DROP TABLE positions")
    conn.execute("INSERT INTO readings (timestamp, rpm, speed) VALUES ('2026-08-03T19:00:00',820,0)")
    conn.commit()
    conn.close()

    assert _run(mod, src, apply=True) == 0

    assert _rows(master, "SELECT timestamp FROM readings ORDER BY timestamp") == [
        ("2026-08-03T18:59:00",), ("2026-08-03T19:00:00",)]


def test_repetir_es_idempotente_y_deja_copia(rec, tmp_path):
    """Reejecutar no duplica filas; --apply hace copia previa del maestro."""
    mod, master, src = rec
    _seed_master(master, 1, ["2026-09-11T10:00:00"])
    _seed_tablet(src, ["2026-09-11T10:00:00", "2026-09-11T10:01:00"])

    _run(mod, src, apply=True)
    _run(mod, src, apply=True)

    assert _rows(master, "SELECT COUNT(*) FROM readings") == [(2,)]
    copias = list(tmp_path.glob("master.db.bak-*"))
    assert len(copias) == 1, f"copia previa esperada: {copias}"
