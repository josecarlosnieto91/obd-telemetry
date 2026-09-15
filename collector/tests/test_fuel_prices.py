"""Tests de fuel_prices: normalización, distancia y estación más cercana.

Sin red: `refresh()` se prueba contra un `urlopen` que falla (caché ausente y
caché vieja), y `load_stations()` contra un JSON sintético con la misma forma
que el del Ministerio.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import fuel_prices  # noqa: E402


ESTACIONES = [
    {"Rótulo": "EROSKI", "Dirección": "AUTOVIA A8, SALIDA 456", "Municipio": "Siero",
     "Localidad": "POLA DE SIERO", "Horario": "L-D: 24H",
     "Latitud": "43,39197", "Longitud (WGS84)": "-5,80319",
     "Precio Gasoleo A": "1,767", "Precio Gasolina 95 E5": "1,839"},
    {"Rótulo": "BALLENOIL", "Dirección": "C/ NARANCO 4", "Municipio": "Oviedo",
     "Localidad": "OVIEDO", "Horario": "L-V: 07-22",
     "Latitud": "43,38230", "Longitud (WGS84)": "-5,81070",
     "Precio Gasoleo A": "1,597", "Precio Gasolina 95 E5": ""},
    {"Rótulo": "SIN COORDS", "Dirección": "X", "Municipio": "X", "Localidad": "X",
     "Horario": "", "Latitud": "", "Longitud (WGS84)": "", "Precio Gasoleo A": "1,700"},
]


@pytest.fixture
def cache(tmp_path, monkeypatch):
    """Caché sintética en un fichero temporal (no toca ~/.hermes/data)."""
    ruta = tmp_path / "fuel_prices.json"
    ruta.write_text(json.dumps({"ListaEESSPrecio": ESTACIONES}))
    monkeypatch.setattr(fuel_prices, "CACHE_PATH", str(ruta))
    return str(ruta)


def test_load_normaliza_precios_y_coords(cache):
    ests = fuel_prices.load_stations(refresh_if_stale=False)
    # la estación sin coordenadas se descarta
    assert len(ests) == 2
    eroski = ests[0]
    assert eroski["nombre"] == "EROSKI"
    assert eroski["municipio"] == "Siero"
    assert eroski["lat"] == pytest.approx(43.39197)
    assert eroski["lon"] == pytest.approx(-5.80319)
    # la coma decimal se convierte; el hueco queda en None
    assert eroski["precios"]["Gasoleo A"] == pytest.approx(1.767)
    assert ests[1]["precios"]["Gasolina 95 E5"] is None


def test_haversine_distancias_conocidas():
    assert fuel_prices.haversine_m(43.0, -5.0, 43.0, -5.0) == pytest.approx(0, abs=0.01)
    # 0.01 grados de latitud ≈ 1.11 km
    assert fuel_prices.haversine_m(43.0, -5.0, 43.01, -5.0) == pytest.approx(1112, abs=5)
    # mismo punto a distinta longitud, en Oviedo a 43.39° → ~810 m por 0.01°
    assert fuel_prices.haversine_m(43.39, -5.80, 43.39, -5.81) == pytest.approx(810, abs=15)


def test_nearest_estacion_cercana(cache):
    ests = fuel_prices.load_stations(refresh_if_stale=False)
    # punto clavado en la Eroski
    est = fuel_prices.nearest_station(43.39212, -5.80328, radius_m=500, stations=ests)
    assert est is not None
    assert est["nombre"] == "EROSKI"
    assert est["dist_m"] < 30
    assert est["precio"] == pytest.approx(1.767)


def test_nearest_devuelve_none_si_no_hay_nada_en_radio(cache):
    ests = fuel_prices.load_stations(refresh_if_stale=False)
    # punto lejos de las dos (más de 500 m)
    assert fuel_prices.nearest_station(43.45, -5.90, radius_m=500, stations=ests) is None


def test_nearest_elige_la_mas_cercana_no_la_mas_barata(cache):
    """Regla: la gasolinera donde está el coche, no la más barata de la zona."""
    ests = fuel_prices.load_stations(refresh_if_stale=False)
    # punto a ~15 m de Ballenoil y a ~1 km de Eroski
    est = fuel_prices.nearest_station(43.38238, -5.81078, radius_m=1500, stations=ests)
    assert est["nombre"] == "BALLENOIL"
    assert est["dist_m"] < 50


def test_producto_sin_precio_devuelve_none(cache):
    ests = fuel_prices.load_stations(refresh_if_stale=False)
    est = fuel_prices.nearest_station(43.38238, -5.81078, radius_m=1500,
                                      product="Gasoleo Premium", stations=ests)
    assert est is not None
    assert est["precio"] is None


def test_nearest_sin_catalogo():
    assert fuel_prices.nearest_station(43.39, -5.80, stations=[]) is None


def test_refresh_sin_red_y_sin_cache_devuelve_false(tmp_path, monkeypatch):
    monkeypatch.setattr(fuel_prices, "CACHE_PATH", str(tmp_path / "no_existe.json"))

    def falla(*_a, **_k):
        raise RuntimeError("sin red")

    # `_descargar` es la costura: los tests nunca tocan la red
    monkeypatch.setattr(fuel_prices, "_descargar", falla)
    assert fuel_prices.refresh(force=True) is False


def test_refresh_sin_red_con_cache_vieja_la_conserva(cache, monkeypatch):
    """Sin red pero con copia previa: se sigue trabajando con la copia."""
    def falla(*_a, **_k):
        raise RuntimeError("sin red")

    monkeypatch.setattr(fuel_prices, "_descargar", falla)
    assert fuel_prices.refresh(force=True) is True
    # y la caché sigue siendo legible
    assert len(fuel_prices.load_stations(refresh_if_stale=False)) == 2


def test_refresh_no_descarga_si_esta_fresca(cache, monkeypatch):
    """Caché recién escrita → no se toca la red."""
    llamado = {"n": 0}

    def no_llamar(*_a, **_k):
        llamado["n"] += 1
        raise AssertionError("no debía descargar")

    monkeypatch.setattr(fuel_prices, "_descargar", no_llamar)
    assert fuel_prices.refresh(max_age_h=24) is True
    assert llamado["n"] == 0


def test_refresh_rechaza_respuesta_basura(cache, monkeypatch):
    """Un 200 con HTML/página de error no debe pisar la caché buena."""
    def basura(destino, *_a, **_k):
        with open(destino, "w") as fh:
            fh.write("<html>error del servidor</html>")

    monkeypatch.setattr(fuel_prices, "_descargar", basura)
    assert fuel_prices.refresh(force=True) is True          # conserva la vieja
    assert len(fuel_prices.load_stations(refresh_if_stale=False)) == 2


def test_valido_detecta_json_sin_estaciones(tmp_path):
    p = tmp_path / "x.json"
    p.write_text(json.dumps({"ListaEESSPrecio": []}))
    assert fuel_prices._valido(str(p)) is False
    p.write_text(json.dumps({"ListaEESSPrecio": ESTACIONES}))
    assert fuel_prices._valido(str(p)) is False             # solo 3, umbral 100
    assert fuel_prices._valido(str(tmp_path / "no_existe")) is False
