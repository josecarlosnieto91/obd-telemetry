"""Parseo de PIDs OBD y filtrado por PIDs soportados.

Las respuestas crudas de los tests son las REALES del C4 Grand Picasso
capturadas con el probe del 2026-09-13 (`probe_pids.py`), no inventadas.
"""
import importlib.util
import os

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
COLLECTOR_DIR = os.path.dirname(HERE)


def _load():
    path = os.path.join(COLLECTOR_DIR, "obd_local_collector.py")
    spec = importlib.util.spec_from_file_location("under_test_parser", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def coll():
    return _load()


# Respuestas reales del ELM327 en este coche (motor al ralentí, 13/09).
CASOS = [
    (b"41 0B 65\r>", 101.0),        # MAP — 1 BYTE (regresión: se exigían 2)
    (b"41 0B 01 2C\r>", 300.0),     # MAP en 2 bytes (otros adaptadores)
    (b"41 0C 0C C4\r>", 817.0),     # rpm
    (b"41 0D 00\r>", 0.0),          # velocidad
    (b"41 04 39\r>", 22.4),         # carga motor
    (b"41 05 68\r>", 64.0),         # refrigerante
    (b"41 0F 54\r>", 44.0),         # temperatura admisión
    (b"41 10 05 19\r>", 13.05),     # MAF
    (b"41 21 00 4F\r>", None),      # distancia con MIL: sin parser
    (b"NO DATA\r>", None),          # PID no soportado
    (b"", None),                    # sin respuesta
]


@pytest.mark.parametrize("crudo,esperado", CASOS)
def test_parse_hex_response(coll, crudo, esperado):
    assert coll.parse_hex_response(crudo) == esperado


def test_read_bridge_omite_pids_no_soportados(coll, monkeypatch):
    """Los PIDs que el motor no expone no deben pedirse: cada uno cuesta la
    espera del ELM327 y devuelve None igualmente."""
    pedidos = []
    respuestas = {
        "ATRV": b"14.2V", "010C": b"41 0C 0C C4", "010D": b"41 0D 00",
        "0105": b"41 05 68", "0104": b"41 04 39", "010B": b"41 0B 65",
        "010F": b"41 0F 54", "0110": b"41 10 05 19",
    }

    def fake_read_pid(sock, cmd, timeout=6):
        pedidos.append(cmd)
        return respuestas.get(cmd, b"NO DATA")

    monkeypatch.setattr(coll, "read_pid", fake_read_pid)
    monkeypatch.setattr(coll, "_BRIDGE_SOCK", object())
    monkeypatch.setattr(coll, "_BRIDGE_INIT_DONE", True)

    # Escaneo real de este coche (supported_pids.json).
    supported = {"01", "04", "05", "0B", "0C", "0D", "0F", "10", "12", "1C", "20", "21"}
    out = coll.read_bridge(supported=supported)

    assert out is not None
    pedidos_01 = [c for c in pedidos if c.startswith("01")]
    for no_soportado in ("0111", "012F", "015E"):
        assert no_soportado not in pedidos_01, f"se pidió {no_soportado} sin soporte"

    lectura = out["reading"]
    assert lectura["map"] == 101.0, "el MAP soportado debe guardarse (fix 1 byte)"
    assert lectura["rpm"] == 817.0
    assert lectura["maf"] == 13.05


def test_sin_escaneo_se_piden_todos(coll, monkeypatch):
    """Sin datos de escaneo (primer arranque) se mantiene el comportamiento
    anterior: pedir todos los PIDs."""
    pedidos = []

    def fake_read_pid(sock, cmd, timeout=6):
        pedidos.append(cmd)
        return b"NO DATA"

    monkeypatch.setattr(coll, "read_pid", fake_read_pid)
    monkeypatch.setattr(coll, "_BRIDGE_SOCK", object())
    monkeypatch.setattr(coll, "_BRIDGE_INIT_DONE", True)

    coll.read_bridge(supported=set())

    pedidos_01 = [c for c in pedidos if c.startswith("01")]
    assert "0111" in pedidos_01 and "012F" in pedidos_01
