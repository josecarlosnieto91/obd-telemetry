"""Regresión de los fixes de higiene de datos del 2026-09-11.

Cubre dos fallos que ensuciaron el historial y ocultaron 3 días de viajes:

1. `obd_local_import.py` — un bloque de lecturas cuyos timestamps ya están en
   destino (dedupe de `import_readings`) dejaba una sesión VACÍA con el mismo
   start_time que la real → pares fantasma de 0 km (152/160, 153/161...).
2. `obd_local_collector.py` — el fallo de `scp` en `sync_to_cassiopeia()`
   devolvía False sin registrar nada → sync muerto y log mudo.

Fixtures de esquema (volcados reales):
  - `schema.sql`       → BD destino de Cassiopeia (sessions, nombres nuevos).
  - `schema_local.sql` → BD de la tablet (sin sessions, nombres antiguos).
"""
import importlib.util
import os
import shutil
import sqlite3

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
COLLECTOR_DIR = os.path.dirname(HERE)
SCHEMA_TARGET = os.path.join(HERE, "schema.sql")
SCHEMA_LOCAL = os.path.join(HERE, "schema_local.sql")


def _load(name):
    """Carga un script del recolector como módulo (no son paquetes)."""
    path = os.path.join(COLLECTOR_DIR, name)
    spec = importlib.util.spec_from_file_location(f"under_test_{name}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _new_db(path, schema):
    conn = sqlite3.connect(path)
    with open(schema) as fh:
        conn.executescript(fh.read())
    return conn


def _seed_local(path, timestamps):
    """Lecturas en el fichero de la tablet (nombres antiguos, sin session_id)."""
    conn = sqlite3.connect(path)
    for ts in timestamps:
        conn.execute(
            "INSERT INTO readings (timestamp, rpm, speed, coolant, throttle, "
            "intake, fuel, maf, voltage) VALUES (?,800,0,70,20,25,0,5.0,14.0)",
            (ts,))
    conn.commit()
    conn.close()


def _count(db, table):
    conn = sqlite3.connect(db)
    n = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    conn.close()
    return n


def _empty_sessions(db):
    """Sesiones sin lecturas NI posiciones (las de GPX sí tienen posiciones)."""
    conn = sqlite3.connect(db)
    n = conn.execute(
        "SELECT COUNT(*) FROM sessions WHERE NOT EXISTS "
        "(SELECT 1 FROM readings WHERE session_id=sessions.id) AND NOT EXISTS "
        "(SELECT 1 FROM positions WHERE session_id=sessions.id)").fetchone()[0]
    conn.close()
    return n


@pytest.fixture
def imp(tmp_path):
    """Importador con las rutas redirigidas a tmp_path (nunca toca producción)."""
    target, local = tmp_path / "target.db", tmp_path / "local.db"
    _new_db(target, SCHEMA_TARGET).close()
    _new_db(local, SCHEMA_LOCAL).close()
    mod = _load("obd_local_import.py")
    mod.TARGET_DB = str(target)
    mod.PROCESSED_DIR = str(tmp_path / "processed")
    mod.INCOMING = str(tmp_path / "incoming.db")
    return mod, str(target), str(local)


def _sync(mod, local):
    """Simula la llegada de un sync: copia el fichero de la tablet y lo importa."""
    shutil.copy(local, mod.INCOMING)
    mod.main()


def test_bloque_sin_datos_no_deja_sesion_vacia(imp, monkeypatch):
    """Dos bloques; el 2º no aporta filas (ya insertadas en otra pasada) →
    no debe quedar sesión fantasma."""
    mod, target, local = imp
    _seed_local(local, ["2027-01-01T10:00:00", "2027-01-01T14:00:00"])  # gap 4 h

    real = mod.import_readings
    calls = {"n": 0}

    def dedupe(conn, lc, sid, a, b):
        calls["n"] += 1
        return 0 if calls["n"] > 1 else real(conn, lc, sid, a, b)

    monkeypatch.setattr(mod, "import_readings", dedupe)
    _sync(mod, local)

    assert _count(target, "sessions") == 1, "solo sobrevive la sesión con datos"
    assert _empty_sessions(target) == 0, "el bloque sin datos dejó una fantasma"
    assert _count(target, "readings") == 1


@pytest.mark.parametrize("vueltas", [2, 3])
def test_reimportar_es_idempotente(imp, vueltas):
    """Reimportar el mismo fichero N veces no crea sesiones ni duplica filas."""
    mod, target, local = imp
    _seed_local(local, ["2027-01-01T10:00:00", "2027-01-01T10:00:30"])

    for _ in range(vueltas):
        _sync(mod, local)

    assert _count(target, "sessions") == 1
    assert _count(target, "readings") == 2
    assert _empty_sessions(target) == 0


def test_dos_viajes_reales_crean_dos_sesiones(imp):
    """Control: la corrección no debe fusionar viajes separados de verdad."""
    mod, target, local = imp
    _seed_local(local, ["2027-01-01T10:00:00", "2027-01-01T14:00:00"])  # gap 4 h

    _sync(mod, local)

    assert _count(target, "sessions") == 2, "dos bloques reales → dos sesiones"
    assert _empty_sessions(target) == 0


def test_fallo_de_sync_se_registra(tmp_path, monkeypatch):
    """El fallo de scp debe quedar en el log con su motivo (antes: silencio)."""
    coll = _load("obd_local_collector.py")
    sqlite3.connect(tmp_path / "local.db").close()
    monkeypatch.setattr(coll, "DB_PATH", str(tmp_path / "local.db"))
    monkeypatch.setattr(coll, "LOG_PATH", str(tmp_path / "obd_local.log"))
    monkeypatch.setattr(coll, "CASSIOPEIA", "host-que-no-existe.invalid")
    monkeypatch.setattr(coll, "INCOMING_PATH", str(tmp_path / "incoming.db"))

    assert coll.sync_to_cassiopeia() is False

    contenido = (tmp_path / "obd_local.log").read_text()
    assert "Sync FALLÓ" in contenido, f"el fallo no se registró: {contenido!r}"
