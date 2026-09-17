"""Origen y destino de los viajes (`trips.start_place` / `end_place`).

Lo que ve el panel/consola del salón: la tabla `trips` de Janus la escribe
`trip_summary`, y al principio la dejaba a NULL → «origen sin determinar →
destino sin determinar». Aquí se prueba la caché de geocodificación (que evita
preguntar cien veces por casa y trabajo) y el relleno del histórico.
"""
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import trip_summary as ts  # noqa: E402


@pytest.fixture
def cache(tmp_path, monkeypatch):
    ruta = tmp_path / "geocode.json"
    monkeypatch.setattr(ts, "GEOCODE_CACHE", str(ruta))
    monkeypatch.setattr(ts, "GEOCODE_MIN_INTERVAL", 0)   # sin esperas en los tests
    return str(ruta)


def test_clave_de_cache_agrupa_el_mismo_sitio():
    """A 3 decimales (~110 m) dos paradas en el mismo sitio son la misma consulta."""
    assert ts._cache_key(43.39212, -5.80328) == ts._cache_key(43.39218, -5.80319)
    assert ts._cache_key(43.3921, -5.8032) != ts._cache_key(43.4001, -5.8032)


def test_geocodifica_una_vez_y_reutiliza(cache, monkeypatch):
    llamadas = []

    def falso(lat, lon):
        llamadas.append((lat, lon))
        return "Oviedo"

    monkeypatch.setattr(ts, "reverse_geocode", falso)
    assert ts.reverse_geocode_cached(43.39212, -5.80328, esperar=False) == "Oviedo"
    # el mismo sitio con 10 m de deriva GPS → de caché, sin nueva consulta
    assert ts.reverse_geocode_cached(43.39218, -5.80319, esperar=False) == "Oviedo"
    assert len(llamadas) == 1, "la caché no evitó la segunda consulta"
    # y queda escrito para la próxima ejecución del script
    assert ts._leer_cache(cache)


def test_un_fallo_no_se_cachea(cache, monkeypatch):
    """Si Nominatim no responde, se reintenta otro día en vez de guardar el hueco."""
    monkeypatch.setattr(ts, "reverse_geocode", lambda lat, lon: None)
    assert ts.reverse_geocode_cached(43.39, -5.80, esperar=False) is None
    assert ts._leer_cache(cache) == {}


def test_respeta_el_minuto_de_espera(cache, monkeypatch):
    """Política de Nominatim: máximo una petición por segundo."""
    dormido = []
    monkeypatch.setattr(ts, "GEOCODE_MIN_INTERVAL", 1.1)
    monkeypatch.setattr(ts.time, "sleep", lambda s: dormido.append(s))
    monkeypatch.setattr(ts, "reverse_geocode", lambda lat, lon: "Siero")
    ts.reverse_geocode_cached(43.39, -5.80)
    assert dormido == [1.1]
    # con la respuesta ya en caché, ni se duerme ni se pregunta
    ts.reverse_geocode_cached(43.39, -5.80)
    assert dormido == [1.1]


def test_nombre_lugar_cae_a_las_coordenadas(cache, monkeypatch):
    monkeypatch.setattr(ts, "reverse_geocode", lambda lat, lon: "Oviedo")
    assert ts.nombre_lugar(43.39, -5.80) == "Oviedo"
    monkeypatch.setattr(ts, "reverse_geocode", lambda lat, lon: None)
    # Sin nombre quedan las coordenadas: eso es un dato, «sin determinar» no.
    assert ts.nombre_lugar(43.3921, -5.8033) == "43.3921,-5.8033"
    assert ts.nombre_lugar(None, None) is None


def _bds(tmp_path):
    """context.db (trips) + obd_telemetry.db (sessions/positions) temporales."""
    ctx = sqlite3.connect(str(tmp_path / "ctx.db"))
    ctx.execute("CREATE TABLE trips (id INTEGER PRIMARY KEY, date TEXT, start_time TEXT,"
                " end_time TEXT, start_place TEXT, end_place TEXT, distance_km REAL,"
                " duration_min INTEGER)")
    ctx.executemany("INSERT INTO trips VALUES (?,?,?,?,NULL,NULL,?,?)",
                    [(1, "2026-09-15", "2026-09-15T09:05:06", "2026-09-15T10:24:54", 8.2, 79),
                     (2, "2026-09-13", "2026-09-13T17:38:07", "2026-09-13T21:22:46", 133.5, 224)])
    ctx.commit()
    ctx.close()

    obd = sqlite3.connect(str(tmp_path / "obd.db"))
    obd.execute("CREATE TABLE sessions (id INTEGER PRIMARY KEY, start_time TEXT)")
    obd.execute("CREATE TABLE positions (id INTEGER PRIMARY KEY, session_id INTEGER,"
                " timestamp TEXT, lat REAL, lon REAL)")
    obd.execute("INSERT INTO sessions VALUES (181, '2026-09-15T09:05:06')")
    obd.execute("INSERT INTO positions VALUES (1, 181, '2026-09-15T09:05:06', 43.3621, -5.8510)")
    obd.execute("INSERT INTO positions VALUES (2, 181, '2026-09-15T10:24:54', 43.3921, -5.8033)")
    obd.commit()
    obd.close()
    # el segundo viaje se queda sin posiciones: no se inventa nada
    return str(tmp_path / "obd.db"), str(tmp_path / "ctx.db")


def test_rellena_el_historico_con_los_lugares(tmp_path, cache, monkeypatch):
    obd, ctx = _bds(tmp_path)
    monkeypatch.setattr(ts, "reverse_geocode",
                        lambda lat, lon: "Oviedo" if lon > -5.83 else "Gijón")
    rellenos, huerfanas, sin_gps = ts._cli_lugares(obd_db=obd, ctx_db=ctx)

    c = sqlite3.connect(ctx)
    viaje1 = c.execute("SELECT start_place, end_place FROM trips WHERE id=1").fetchone()
    viaje2 = c.execute("SELECT start_place, end_place FROM trips WHERE id=2").fetchone()
    c.close()
    assert viaje1 == ("Gijón", "Oviedo")      # primero y último punto GPS
    assert viaje2 == (None, None)             # sin sesión: no se inventa nada
    assert (rellenos, huerfanas, sin_gps) == (1, 1, 0)


def test_distingue_huerfana_de_sin_gps(tmp_path, cache, monkeypatch):
    """Una fila SIN SESIÓN (fragmento de fusión) no es lo mismo que un viaje real
    cuyas posiciones no llegaron: se cuentan por separado, que es lo que hace que
    el informe diga la verdad."""
    obd, ctx = _bds(tmp_path)
    # viaje real (tiene sesión) pero sin posiciones GPS
    c = sqlite3.connect(obd)
    c.execute("INSERT INTO sessions VALUES (99, '2026-09-10T08:00:00')")
    c.commit()
    c.close()
    c = sqlite3.connect(ctx)
    c.execute("INSERT INTO trips VALUES (3,'2026-09-10','2026-09-10T08:00:00',"
              "'2026-09-10T08:30:00',NULL,NULL,12.0,30)")
    c.commit()
    c.close()
    monkeypatch.setattr(ts, "reverse_geocode", lambda lat, lon: "Oviedo")
    rellenos, huerfanas, sin_gps = ts._cli_lugares(obd_db=obd, ctx_db=ctx)
    assert rellenos == 1          # el que tiene GPS
    assert huerfanas == 1         # el 2: no hay sesión que le corresponda
    assert sin_gps == 1           # el 3: sesión sí, posiciones no


def test_no_repite_los_que_ya_tienen_lugar(tmp_path, cache, monkeypatch):
    """Idempotente: a la segunda pasada no hay nada que rellenar."""
    obd, ctx = _bds(tmp_path)
    llamadas = []

    def contar(lat, lon):
        llamadas.append((lat, lon))
        return "Oviedo"

    monkeypatch.setattr(ts, "reverse_geocode", contar)
    ts._cli_lugares(obd_db=obd, ctx_db=ctx)
    primera = len(llamadas)
    rellenos, _, _ = ts._cli_lugares(obd_db=obd, ctx_db=ctx)
    assert rellenos == 0
    assert len(llamadas) == primera, "volvió a geocodificar viajes que ya tenían lugar"
