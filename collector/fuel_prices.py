#!/usr/bin/env python3
"""Precios oficiales de carburante (Ministerio) — caché local + estación cercana.

Fuente: API pública del Ministerio para la Transición Ecológica
(`EstacionesTerrestres`), un JSON de ~12 MB con las ~11.500 estaciones de
España y sus precios.

Por qué se cachea: el detector de repostajes no debe depender de la red en el
momento de detectar (el repostaje puede pasar con la tablet sin cobertura). El
fichero se descarga cuando falta o tiene más de `max_age_h` horas, y si la
descarga falla se sigue usando la copia vieja (mejor un precio de ayer que
ninguno).

Uso:
    from fuel_prices import load_stations, nearest_station
    est = nearest_station(43.39, -5.80, radius_m=500, product="Gasoleo A")
"""
import json
import math
import os
import shutil
import subprocess
import sys
import time
import urllib.request

CACHE_PATH = os.path.expanduser("~/.hermes/data/fuel_prices.json")
API_URL = ("https://sedeaplicaciones.minetur.gob.es/ServiciosRESTCarburantes/"
           "PreciosCarburantes/EstacionesTerrestres/")
MAX_AGE_H = 24
RADIUS_M = 500.0
PRODUCT = "Gasoleo A"        # diésel A (el C4 es diésel)
USER_AGENT = "obd-telemetry/1.0"


def _cache_age_h():
    try:
        return (time.time() - os.path.getmtime(CACHE_PATH)) / 3600.0
    except OSError:
        return None      # no existe


def _descargar(destino, timeout=120):
    """Baja el listado a `destino`.

    `curl` primero y urllib de reserva: el TLS del servidor del Ministerio
    corta la conexión con urllib/sondas de Python (SSLEOFError reproducido);
    curl negocia bien. No es un capricho, es lo que funciona.
    """
    if shutil.which("curl"):
        r = subprocess.run(
            ["curl", "-sSfL", "--max-time", str(timeout), "-A", USER_AGENT,
             "-o", destino, API_URL],
            capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"curl rc={r.returncode}: {r.stderr.strip()[:200]}")
        return
    req = urllib.request.Request(API_URL, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp, \
            open(destino, "wb") as fh:
        shutil.copyfileobj(resp, fh)


def _valido(ruta):
    """¿Trae estaciones de verdad? Evita cambiar la caché buena por la página
    de error que el servidor devuelva algún día."""
    try:
        with open(ruta) as fh:
            return len(json.load(fh).get("ListaEESSPrecio", [])) > 100
    except (OSError, ValueError):
        return False


def refresh(force=False, max_age_h=MAX_AGE_H, timeout=120):
    """Descarga el listado si falta o está viejo. Devuelve True si hay caché.

    Si la descarga falla pero hay copia previa, se conserva (devuelve True): es
    preferible un precio de ayer a quedarse sin precio.
    """
    age = _cache_age_h()
    if age is not None and age < max_age_h and not force:
        return True
    tmp = CACHE_PATH + ".part"
    try:
        _descargar(tmp, timeout)
        if not _valido(tmp):
            raise ValueError("respuesta sin estaciones (¿página de error?)")
        os.replace(tmp, CACHE_PATH)      # atómico: nunca queda un JSON a medias
        return True
    except Exception as e:
        sys.stderr.write(f"fuel price refresh fail: {type(e).__name__}: {e}\n")
        try:
            os.remove(tmp)
        except OSError:
            pass
        return age is not None           # hay caché vieja → seguimos


def _num(valor):
    """'1,787' → 1.787 · '' → None."""
    try:
        return float(str(valor).replace(",", ".").strip())
    except (TypeError, ValueError):
        return None


def load_stations(refresh_if_stale=True, max_age_h=MAX_AGE_H):
    """Lista normalizada de estaciones: dicts con nombre, dirección y coords.

    Devuelve [] si no hay caché ni red.
    """
    if refresh_if_stale:
        refresh(max_age_h=max_age_h)
    try:
        with open(CACHE_PATH) as fh:
            datos = json.load(fh)
    except (OSError, ValueError):
        return []
    salida = []
    for e in datos.get("ListaEESSPrecio", []):
        lat, lon = _num(e.get("Latitud")), _num(e.get("Longitud (WGS84)"))
        if lat is None or lon is None:
            continue
        salida.append({
            "nombre": (e.get("Rótulo") or "").strip(),
            "direccion": (e.get("Dirección") or "").strip(),
            "municipio": (e.get("Municipio") or "").strip(),
            "localidad": (e.get("Localidad") or "").strip(),
            "horario": (e.get("Horario") or "").strip(),
            "lat": lat, "lon": lon,
            "precios": {k[len("Precio "):]: _num(v)
                        for k, v in e.items() if k.startswith("Precio ")},
        })
    return salida


def haversine_m(lat1, lon1, lat2, lon2):
    """Distancia en metros entre dos puntos (WGS84)."""
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def nearest_station(lat, lon, radius_m=RADIUS_M, product=PRODUCT, stations=None):
    """Estación más cercana dentro de `radius_m` que tenga precio del producto.

    Devuelve el dict de la estación + {'dist_m': float, 'precio': float|None},
    o None si no hay ninguna (sin caché, o el coche no está en una gasolinera).
    """
    if stations is None:
        stations = load_stations()
    mejor, mejor_d = None, float("inf")
    for e in stations:
        d = haversine_m(lat, lon, e["lat"], e["lon"])
        if d <= radius_m and d < mejor_d:
            mejor, mejor_d = e, d
    if mejor is None:
        return None
    salida = dict(mejor)
    salida["dist_m"] = round(mejor_d, 1)
    salida["precio"] = mejor["precios"].get(product)
    return salida


if __name__ == "__main__":       # refresco manual / cron diario
    ok = refresh(force=True)
    print("caché de precios actualizada" if ok else "no se pudo actualizar")
