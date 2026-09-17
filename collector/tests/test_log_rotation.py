"""Higiene de arranque en la tablet: `.sync` huérfano y rotación del log.

Dos residuos reales medidos el 2026-09-17:

  - `obd_data/obd_local.sync` (2,1 MB, del 3/09): un sync interrumpido a lo
    bruto deja el SQLite temporal, porque el `finally` que lo borra no corre
    cuando Android mata Termux. Se regenera solo y su contenido ya está subido
    (la marca de agua solo avanza con éxito) → se puede borrar sin riesgo.
  - `obd_data/obd_local.log`: crecía sin límite (~3 KB/día) y nadie lo rotaba.

La propiedad que protegen: la limpieza **no toca nada más** y no falla si los
ficheros no existen (primer arranque, tablet recién encendida).
"""
import importlib.util
import os

HERE = os.path.dirname(os.path.abspath(__file__))
COLLECTOR_DIR = os.path.dirname(HERE)
FUENTE = os.path.join(COLLECTOR_DIR, "obd_local_collector.py")


def _load():
    spec = importlib.util.spec_from_file_location("under_test_hygiene", FUENTE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


coll = _load()


def test_borra_el_sync_huerfano(tmp_path):
    db = tmp_path / "obd_local.db"
    log = tmp_path / "obd_local.log"
    db.write_bytes(b"base")
    log.write_text("una linea\n")
    huerfano = tmp_path / "obd_local.db.sync"
    huerfano.write_bytes(b"SQLite format 3\x00" + b"\x00" * 5000)

    hechas = coll.limpiar_arranque(str(db), str(log))

    assert "sync_huerfano" in hechas
    assert not huerfano.exists()
    # Y no se lleva por delante la BD ni el log
    assert db.read_bytes() == b"base"
    assert log.read_text() == "una linea\n"


def test_rota_el_log_cuando_pasa_del_tope(tmp_path):
    db = tmp_path / "obd_local.db"
    log = tmp_path / "obd_local.log"
    db.write_bytes(b"base")
    log.write_text("x" * 2000)

    hechas = coll.limpiar_arranque(str(db), str(log), max_bytes=1000)

    assert "log_rotado" in hechas
    assert not log.exists()                        # empieza limpio
    assert (tmp_path / "obd_local.log.old").read_text() == "x" * 2000  # no se pierde


def test_no_toca_el_log_pequeno(tmp_path):
    db = tmp_path / "obd_local.db"
    log = tmp_path / "obd_local.log"
    db.write_bytes(b"base")
    log.write_text("poco\n")

    hechas = coll.limpiar_arranque(str(db), str(log), max_bytes=1_000_000)

    assert hechas == []
    assert log.read_text() == "poco\n"
    assert not (tmp_path / "obd_local.log.old").exists()


def test_no_falla_si_no_hay_ficheros(tmp_path):
    """Primer arranque en un directorio vacío: ni excepción ni acciones."""
    hechas = coll.limpiar_arranque(str(tmp_path / "nada.db"), str(tmp_path / "nada.log"))
    assert hechas == []


def test_el_tope_es_razonable():
    """A ~3 KB/día, 1 MB son ~11 meses: se rota de verdad, no cada semana."""
    dias = coll.LOG_MAX_BYTES / 3000
    assert 100 < dias < 500


def test_main_llama_a_la_limpieza():
    fuente = open(FUENTE).read()
    assert "for accion in limpiar_arranque():" in fuente
