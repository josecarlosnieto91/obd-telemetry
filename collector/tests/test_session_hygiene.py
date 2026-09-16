"""Regresión de los fixes de higiene de datos del 2026-09-11 (+ 2026-09-16).

Cubre fallos que ensuciaron el historial o tiraron datos en silencio:

1. `obd_local_import.py` — un bloque de lecturas cuyos timestamps ya están en
   destino (dedupe de `import_readings`) dejaba una sesión VACÍA con el mismo
   start_time que la real → pares fantasma de 0 km (152/160, 153/161...).
2. `obd_local_collector.py` — el fallo de `scp` en `sync_to_cassiopeia()`
   devolvía False sin registrar nada → sync muerto y log mudo.
3. `obd_local_import.py` — las POSICIONES se filtraban con la ventana de las
   LECTURAS, así que los puntos GPS entre la última posición importada y la
   primera lectura nueva (el GPS muestrea más fino que el OBD) se perdían en
   cada sync partido.

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


def test_posicion_en_el_seam_entre_syncs_no_se_pierde(imp):
    """El GPS muestrea más fino que el OBD: hay posiciones entre la última
    posición importada y la primera lectura nueva del bloque. Filtrarlas con la
    ventana de LECTURAS las tiraba en silencio (fix 2026-09-16)."""
    mod, target, local = imp
    # Sync anterior ya importado: lectura y posición a las 10:04:51
    conn = sqlite3.connect(target)
    sid = conn.execute(
        "INSERT INTO sessions (start_time, status) VALUES ('2026-09-15T10:02:06','active')"
    ).lastrowid
    conn.execute("INSERT INTO readings (session_id, timestamp, rpm, speed) VALUES (?,?,800,50)",
                 (sid, "2026-09-15T10:04:51"))
    conn.execute("INSERT INTO positions (session_id, timestamp, lat, lon) VALUES (?,?,43.3,-5.9)",
                 (sid, "2026-09-15T10:04:51"))
    conn.commit()
    conn.close()

    # El fichero de la tablet repite lo anterior y añade la posición del seam
    conn = sqlite3.connect(local)
    for ts in ("2026-09-15T10:04:51", "2026-09-15T10:05:54", "2026-09-15T10:06:35"):
        conn.execute(
            "INSERT INTO readings (timestamp, rpm, speed, coolant, voltage) "
            "VALUES (?,800,50,80,14.0)", (ts,))
    for ts in ("2026-09-15T10:04:51", "2026-09-15T10:05:22", "2026-09-15T10:05:54"):
        conn.execute("INSERT INTO positions (timestamp, lat, lon) VALUES (?,43.3,-5.9)", (ts,))
    conn.commit()
    conn.close()

    _sync(mod, local)

    conn = sqlite3.connect(target)
    posiciones = [r[0] for r in conn.execute("SELECT timestamp FROM positions ORDER BY timestamp")]
    conn.close()
    assert posiciones == ["2026-09-15T10:04:51", "2026-09-15T10:05:22",
                          "2026-09-15T10:05:54"], f"posición del seam perdida: {posiciones}"
