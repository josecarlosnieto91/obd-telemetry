"""Sync a Cassiopeia: subida en dos fases (staging + rename atómico).

Regresión del fallo real del 2026-09-13: el snapshot pesa ~3.7 MB y el enlace
tarda ~25 s, pero el timeout era de 20 s → el `scp` se cortaba a medias y
dejaba un SQLite TRUNCADO en el destino, que el importador descartaba con
"database disk image is malformed" en cada ciclo.
"""
import importlib.util
import os
import sqlite3

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
COLLECTOR_DIR = os.path.dirname(HERE)


class _Result:
    def __init__(self, rc=0, stderr=""):
        self.returncode = rc
        self.stderr = stderr
        self.stdout = ""


@pytest.fixture
def coll(tmp_path, monkeypatch):
    path = os.path.join(COLLECTOR_DIR, "obd_local_collector.py")
    spec = importlib.util.spec_from_file_location("under_test_sync", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    db = tmp_path / "obd_local.db"
    sqlite3.connect(db).close()
    monkeypatch.setattr(mod, "DB_PATH", str(db))
    monkeypatch.setattr(mod, "LOG_PATH", str(tmp_path / "obd_local.log"))
    monkeypatch.setattr(mod, "CASSIOPEIA", "cassiopeia")
    monkeypatch.setattr(mod, "INCOMING_PATH", "/dest/polar_obd_local.db")
    return mod


def _captura(mod, monkeypatch, resultados):
    llamadas = []

    def fake_run(cmd, **kw):
        llamadas.append(cmd)
        return resultados[min(len(llamadas) - 1, len(resultados) - 1)]

    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    return llamadas


def test_subida_en_dos_fases(coll, monkeypatch):
    """scp a .part y luego rename: el importador nunca ve un fichero parcial."""
    llamadas = _captura(coll, monkeypatch, [_Result(0), _Result(0)])

    assert coll.sync_to_cassiopeia() is True

    scp, ssh = llamadas[0], llamadas[1]
    assert scp[0] == "scp"
    assert scp[-1].endswith(".part"), "el destino del scp debe ser .part"
    assert ssh[0] == "ssh"
    assert "mv -f" in ssh[-1] and ssh[-1].endswith("/dest/polar_obd_local.db")


def test_timeout_generoso_para_el_enlace(coll):
    """El enlace tarda ~25 s: el timeout no puede quedarse corto."""
    assert coll.SYNC_TIMEOUT >= 60


def test_scp_fallido_no_renombra(coll, monkeypatch):
    """Si la subida falla, NO debe renombrarse (el .part no es válido) y el
    fallo queda registrado."""
    llamadas = _captura(coll, monkeypatch, [_Result(255, "timeout")])

    assert coll.sync_to_cassiopeia() is False
    assert len(llamadas) == 1, "no debe intentar el rename tras fallar el scp"
    assert "Sync FALLÓ" in open(coll.LOG_PATH).read()


def test_rename_fallido_se_registra(coll, monkeypatch):
    """Subida OK pero rename KO → False y traza (no dar por bueno el sync)."""
    llamadas = _captura(coll, monkeypatch, [_Result(0), _Result(1, "mv: denied")])

    assert coll.sync_to_cassiopeia() is False
    assert len(llamadas) == 2
    assert "rename" in open(coll.LOG_PATH).read()
