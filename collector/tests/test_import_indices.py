"""El importador deja el índice de can_readings(ts) creado.

Sin ese índice, la consulta de combustible restante de la webapp
(`SELECT range_km FROM can_readings ... ORDER BY ts DESC LIMIT 1`) escaneaba la
tabla entera y ordenaba en un TEMP B-TREE. Medido el 2026-10-28 con 86.738
filas: /api/status 14,8 ms → 1,8 ms al crear el índice, y esa ruta se llama
cada 5 s desde cada pestaña abierta.
"""
import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
COLLECTOR = os.path.dirname(HERE)


def _load():
    if "obd_local_import" in sys.modules:
        return sys.modules["obd_local_import"]
    sys.path.insert(0, COLLECTOR)
    spec = importlib.util.spec_from_file_location(
        "obd_local_import", os.path.join(COLLECTOR, "obd_local_import.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["obd_local_import"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_connect_crea_el_indice_de_can_readings(tmp_path):
    im = _load()
    db = tmp_path / "obd.db"
    con = im.connect_db(str(db))
    indices = {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='index'")}
    assert "idx_can_readings_ts" in indices
    con.close()


def test_connect_es_idempotente(tmp_path):
    """Dos aperturas seguidas no fallan ni duplican el índice."""
    im = _load()
    db = tmp_path / "obd.db"
    for _ in range(2):
        im.connect_db(str(db)).close()
    con = im.connect_db(str(db))
    n = con.execute("SELECT count(*) FROM sqlite_master WHERE type='index' "
                    "AND name='idx_can_readings_ts'").fetchone()[0]
    assert n == 1
    con.close()
