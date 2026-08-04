#!/usr/bin/env python3
"""Precios de carburante — API oficial del Ministerio (España completa).

Fuente: https://sedeaplicaciones.minetur.gob.es/ServiciosRESTCarburantes/
Sin API key. Descarga TODAS las estaciones de España (11.5k) con lat/lon y
precios del día (se actualiza a diario). La estación se elige por proximidad
GPS al repostaje, da igual en qué provincia estés.

Uso:
    from fuel_price import get_price_at, nearest_station, get_province_avg

Cache: listado reducido (solo campos útiles) en
~/.hermes/data/fuel_cache.json, TTL 12h. Si la API falla se usa la cache
vieja; si no hay nada, None.

⚠️ El servidor del Ministerio solo habla TLS 1.2 antiguo — Python urllib
falla con UNEXPECTED_EOF. Se usa curl como transporte (subprocess).
"""
import json, os, math, subprocess, time

CACHE_PATH = os.path.expanduser("~/.hermes/data/fuel_cache.json")
CACHE_TTL = 12 * 3600  # 12 horas
API_URL = "https://sedeaplicaciones.minetur.gob.es/ServiciosRESTCarburantes/PreciosCarburantes/EstacionesTerrestres/"
CONFIG_PATH = os.path.expanduser("~/.hermes/scripts/obd_vehicle_config.json")
USER_AGENT = "VehicleTelemetry/1.0 (personal vehicle tracker)"


def _fuel_product():
    """Producto del Ministerio para este vehículo (portabilidad 2026-08-05).
    Lee vehicle.fuel_price_product del config (p.ej. 'Gasoleo A', 'Gasolina 95 E5').
    Fallback: Gasoleo A (configuración histórica del C4)."""
    try:
        with open(CONFIG_PATH) as fh:
            return json.load(fh).get("vehicle", {}).get("fuel_price_product", "Gasoleo A")
    except Exception:
        return "Gasoleo A"


FUEL_FIELD = _fuel_product()  # campo del listado del Ministerio para este coche


def _parse_price(s):
    """'1,898' → 1.898. Vacío → None."""
    if not s:
        return None
    try:
        return float(str(s).strip().replace(",", "."))
    except Exception:
        return None


def _load_cache():
    try:
        with open(CACHE_PATH) as fh:
            return json.load(fh)
    except Exception:
        return None


def _save_cache(data):
    try:
        with open(CACHE_PATH, "w") as fh:
            json.dump(data, fh)
    except Exception:
        pass


def get_stations(force=False):
    """Todas las estaciones de España: [{lat, lon, rotulo, direccion,
    municipio, provincia, precio_gasoleo_a}]. Con cache de 12h."""
    cache = _load_cache()
    now = time.time()
    if cache and not force and (now - cache.get("ts", 0)) < CACHE_TTL:
        return cache.get("stations", [])

    try:
        # curl: TLS 1.2 antiguo del Ministerio (urllib falla con UNEXPECTED_EOF)
        proc = subprocess.run(
            ["curl", "-s", "--max-time", "60", "-A", USER_AGENT, API_URL],
            capture_output=True, text=True, timeout=75,
        )
        data = json.loads(proc.stdout)
        raw = data.get("ListaEESSPrecio", [])
        stations = []
        for s in raw:
            lat = _parse_price(s.get("Latitud"))
            lon = _parse_price(s.get("Longitud (WGS84)"))
            if lat is None or lon is None:
                continue
            stations.append({
                "lat": lat, "lon": lon,
                "rotulo": s.get("Rótulo", ""),
                "direccion": s.get("Dirección", ""),
                "municipio": s.get("Municipio", ""),
                "provincia": s.get("Provincia", ""),
                "precio_gasoleo_a": _parse_price(s.get(FUEL_FIELD)),
            })
        _save_cache({"ts": now, "stations": stations})
        return stations
    except Exception as e:
        # Fallback a cache vieja (mejor precio viejo que nada)
        if cache:
            return cache.get("stations", [])
        print(f"fuel_price: API error: {e}", flush=True)
        return []


def haversine(lat1, lon1, lat2, lon2):
    R = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def nearest_station(lat, lon, stations=None):
    """Estación más cercana a (lat, lon) en toda España. Dict o None."""
    if stations is None:
        stations = get_stations()
    best, best_d = None, float("inf")
    for s in stations:
        d = haversine(lat, lon, s["lat"], s["lon"])
        if d < best_d:
            best, best_d = s, d
    return best


def get_price_at(lat, lon):
    """Precio Gasoleo A (€/L) de la estación más cercana a (lat, lon).
    Devuelve (precio, estación_dict) o (None, None)."""
    stations = get_stations()
    if not stations:
        return None, None
    s = nearest_station(lat, lon, stations)
    if not s:
        return None, None
    return s.get("precio_gasoleo_a"), s


def get_province_avg(provincia=None):
    """Media de Gasoleo A. Sin provincia = media de España."""
    stations = get_stations()
    if provincia:
        stations = [s for s in stations if s.get("provincia") == provincia]
    prices = [s["precio_gasoleo_a"] for s in stations if s.get("precio_gasoleo_a")]
    if not prices:
        return None
    return round(sum(prices) / len(prices), 3)


if __name__ == "__main__":
    stations = get_stations(force=True)
    print(f"{len(stations)} estaciones en España")
    print(f"Media España Gasoleo A: {get_province_avg()} €/L")
    # Oviedo
    p, s = get_price_at(43.3603, -5.8448)
    if s:
        print(f"Oviedo: {p} €/L en {s['rotulo']} ({s['municipio']}, {s['provincia']})")
    # Salamanca (Castilla y León)
    p2, s2 = get_price_at(40.9701, -5.6634)
    if s2:
        print(f"Salamanca: {p2} €/L en {s2['rotulo']} ({s2['municipio']}, {s2['provincia']})")
