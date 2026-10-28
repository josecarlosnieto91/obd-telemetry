"""Enlace al sitio en el resumen de viaje (trip_summary.site_link).

El resumen de viaje llega por Telegram y es la vía normal de JC para ver el
coche. Tiene que llevar el enlace al dashboard y, si se puede, al detalle de
ESE viaje (/trips?trip=<id>, enlace permanente que abre el modal).
"""
import importlib.util
import json
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
COLLECTOR = os.path.dirname(HERE)


def _load():
    if "trip_summary" in sys.modules:
        return sys.modules["trip_summary"]
    sys.path.insert(0, COLLECTOR)
    spec = importlib.util.spec_from_file_location(
        "trip_summary", os.path.join(COLLECTOR, "trip_summary.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["trip_summary"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_enlace_por_defecto_es_la_tailnet(monkeypatch, tmp_path):
    """Sin config: MagicDNS de la tailnet (no una IP, que cambia)."""
    ts = _load()
    monkeypatch.setattr(ts, "CONFIG_PATH", str(tmp_path / "no-existe.json"))
    url = ts.site_url()
    assert url.startswith("https://") and url.endswith(":8446")
    assert "127.0.0.1" not in url and "100." not in url


def test_enlace_apunta_al_viaje_concreto(monkeypatch, tmp_path):
    ts = _load()
    cfg = tmp_path / "cfg.json"
    cfg.write_text(json.dumps({"webapp": {"site_url": "https://coche.example:8446/"}}))
    monkeypatch.setattr(ts, "CONFIG_PATH", str(cfg))

    assert ts.site_link(207) == "🔗 https://coche.example:8446/trips?trip=207"
    # La barra final del config no puede duplicarse
    assert "//trips" not in ts.site_link()
    assert ts.site_link() == "🔗 https://coche.example:8446/trips"


def test_url_vacia_cae_al_valor_por_defecto(monkeypatch, tmp_path):
    """`site_url` vacío en config no deja el resumen sin enlace."""
    ts = _load()
    cfg = tmp_path / "cfg.json"
    cfg.write_text(json.dumps({"webapp": {"site_url": ""}}))
    monkeypatch.setattr(ts, "CONFIG_PATH", str(cfg))
    assert ts.site_link(1).endswith("/trips?trip=1")
    assert ts.site_url() == ts.SITE_URL_DEFAULT


@pytest.mark.parametrize("sid,esperado", [(5, "/trips?trip=5"), (None, "/trips")])
def test_destino_del_enlace(monkeypatch, tmp_path, sid, esperado):
    ts = _load()
    cfg = tmp_path / "cfg.json"
    cfg.write_text(json.dumps({"webapp": {"site_url": "https://x.ts.net:8446"}}))
    monkeypatch.setattr(ts, "CONFIG_PATH", str(cfg))
    assert ts.site_link(sid).endswith(esperado)
