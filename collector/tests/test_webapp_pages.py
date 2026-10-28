"""Render de las pantallas de la webapp: HTTP 200 + JavaScript VÁLIDO.

Regresión del bug del 2026-09-13: `const CAPACITY = {{ capacity_l }};` en
`refuels.html` sin que la ruta pasara la variable → Jinja lo dejaba vacío →
`const CAPACITY = ;` → SyntaxError que mataba TODO el JS de la página (el
listado y el formulario de repostajes parecían "vacíos", sin error visible).

Un `node --check` por bloque <script> caza esa clase de fallo (y cualquier
error de sintaxis introducido al tocar las plantillas).
"""
import importlib.util
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(HERE))
WEBAPP = os.path.join(REPO_ROOT, "webapp")

RUTAS = ["/", "/trips", "/alerts", "/dtcs", "/maintenance", "/refuels", "/map"]


@pytest.fixture(scope="module")
def client():
    sys.path.insert(0, WEBAPP)
    spec = importlib.util.spec_from_file_location(
        "webapp_under_test", os.path.join(WEBAPP, "app.py"))
    mod = importlib.util.module_from_spec(spec)
    # Registrarlo ANTES de ejecutarlo: Flask resuelve el directorio de
    # plantillas buscando el módulo en sys.modules (si no, TemplateNotFound).
    sys.modules["webapp_under_test"] = mod
    spec.loader.exec_module(mod)
    mod.app.config["TESTING"] = True
    return mod.app.test_client()


@pytest.mark.parametrize("ruta", RUTAS)
def test_pantalla_responde_200(client, ruta):
    r = client.get(ruta)
    assert r.status_code == 200, f"{ruta} → {r.status_code}"


@pytest.mark.parametrize("ruta", RUTAS)
def test_javascript_inline_es_valido(client, ruta, tmp_path):
    """Ninguna plantilla puede servir JS que no parsee."""
    if shutil.which("node") is None:
        pytest.skip("node no disponible")
    html = client.get(ruta).get_data(as_text=True)
    bloques = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", html, re.S)
    assert bloques, f"{ruta}: sin <script> inline (¿plantilla rota?)"

    for i, js in enumerate(bloques):
        fichero = tmp_path / f"bloque{i}.js"
        fichero.write_text(js)
        res = subprocess.run(["node", "--check", str(fichero)],
                             capture_output=True, text=True)
        assert res.returncode == 0, (
            f"{ruta} bloque {i}: JavaScript inválido\n{res.stderr[:400]}")


@pytest.mark.parametrize("ruta", RUTAS)
def test_sin_variables_jinja_sin_renderizar(client, ruta):
    """Una {{ variable }} sin pasar deja el JS roto en silencio."""
    html = client.get(ruta).get_data(as_text=True)
    restos = re.findall(r"\{\{.*?\}\}|\{%.*?%\}", html)
    assert not restos, f"{ruta}: quedaron expresiones Jinja sin renderizar: {restos[:3]}"


def test_confirmar_litros_conserva_los_decimales_del_surtidor(client, tmp_path,
                                                             monkeypatch):
    """Confirmar los litros de un repostaje NO puede redondear a un decimal.

    Regresión del 2026-10-15: el endpoint hacía `round(litros, 1)`, así que un
    surtidor que da 49,16 L quedaba guardado como 49,2 L y el coste salía 7
    céntimos por encima del ticket. Es la misma clase de fallo que ya se arregló
    en /api/refuels/manual (9,98 L → 10,0 L) y que aquí había quedado sin tocar.
    """
    mod = sys.modules["webapp_under_test"]
    db = tmp_path / "refuels.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE refuels (id INTEGER PRIMARY KEY, ts TEXT, "
                "liters REAL, price_per_l REAL, cost REAL, station TEXT)")
    con.execute("INSERT INTO refuels (id, ts, liters, price_per_l) "
                "VALUES (112, '2026-10-15T12:18:51', 44.7, 1.827)")
    con.commit()
    con.close()
    monkeypatch.setattr(mod, "DB_PATH", str(db))

    r = client.post("/api/refuels/112/liters", json={"liters": 49.16})
    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.get_json() == {"ok": True, "liters": 49.16, "cost": 89.82,
                            "price_per_l": 1.827}

    fila = sqlite3.connect(db).execute(
        "SELECT liters, cost FROM refuels WHERE id = 112").fetchone()
    assert fila == (49.16, 89.82), f"guardado redondeado: {fila}"


def test_hoja_de_estilo_sellada_con_su_fecha(client):
    """Los estáticos van con `?v=<mtime del más reciente de static/>`.

    Sin el sello el navegador sirve la copia cacheada y un cambio de estilo
    "no se aplica": se diagnostica como si la regla no existiera cuando lo que
    pasa es que no ha llegado. Desde 2026-10-28 el sello cubre también nav.js y
    chart.umd.min.js, así que es el máximo de las tres fechas (nunca menor que
    la de style.css).
    """
    html = client.get("/").get_data(as_text=True)
    m = re.search(r'href="(/static/style\.css\?v=(\d+))"', html)
    assert m, "la hoja de estilo no va sellada con su fecha"
    sello = int(m.group(2))
    mtime_css = int(os.path.getmtime(os.path.join(WEBAPP, "static", "style.css")))
    assert sello >= mtime_css, "el sello es más viejo que la hoja de estilo"
    mtime_chart = int(os.path.getmtime(
        os.path.join(WEBAPP, "static", "chart.umd.min.js")))
    assert sello == max(mtime_css, mtime_chart), (
        "el sello no es la fecha del estático más reciente")


def test_css_estiliza_los_controles_nativos(client):
    """`input`/`select` no tenían NINGUNA regla: se veían los nativos claros.

    Se comprueba el CSS que sirve el servidor (con el sello), no el fichero.
    """
    html = client.get("/map").get_data(as_text=True)
    m = re.search(r'href="(/static/style\.css[^"]*)"', html)
    assert m, "la página no enlaza la hoja de estilo"
    css = client.get(m.group(1)).get_data(as_text=True)

    bloques = [(sel.strip(), cuerpo) for sel, cuerpo in
               re.findall(r"([^{}]+)\{([^{}]*)\}", css)]
    control = [cuerpo for sel, cuerpo in bloques
               if "select" in sel and "input" in sel]
    assert control, "el CSS servido no trae regla para input/select"
    # Hay varias reglas para los controles (normal, :focus, checkbox...): lo que
    # se exige es que al menos UNA fije fondo, tinta y borde del tema.
    esperadas = ("background: var(--bg)", "color: var(--text)",
                 "border: 1px solid var(--border)")
    assert any(all(d in cuerpo for d in esperadas) for cuerpo in control), (
        "ninguna regla de controles fija fondo, tinta y borde del tema: "
        "seguirían viéndose los nativos claros")


# ── Servicio: sonda, favicon, gzip, 404 y estáticos locales ────────────────
# Añadidos al revisar la web (2026-10-28): la app no tenía /health ni favicon
# (404 en cada carga), no comprimía (1 MB por el mapa), devolvía 500 en un
# viaje inexistente y cargaba Chart.js desde un CDN (sin Internet, gráfico
# muerto en la tablet).

def _bd_minima(tmp_path):
    """BD mínima con lo que consultan /api/trips y /health."""
    db = tmp_path / "obd.db"
    con = sqlite3.connect(db)
    con.execute("""CREATE TABLE sessions (
        id INTEGER PRIMARY KEY, start_time TEXT, end_time TEXT,
        distance_km REAL, max_speed REAL, avg_speed REAL, max_rpm REAL,
        driving_minutes INTEGER, status TEXT, consumption_l100 REAL,
        real_l100 REAL)""")
    con.execute("""CREATE TABLE readings (
        id INTEGER PRIMARY KEY, session_id INTEGER, timestamp TEXT,
        rpm REAL, speed REAL, coolant_temp REAL, engine_load REAL,
        throttle_pos REAL, fuel_level REAL, voltage REAL, maf REAL)""")
    con.execute("INSERT INTO sessions (id, start_time, status, distance_km, "
                "real_l100, consumption_l100) VALUES "
                "(1, '2026-10-28T14:10:58', 'completed', 12.5, 5.4, 3.1)")
    con.execute("INSERT INTO readings (session_id, timestamp, maf, speed) "
                "VALUES (1, '2026-10-28T14:11:00', 25.0, 90.0)")
    con.commit()
    con.close()
    return db


def test_health_responde_y_dice_la_ultima_lectura(client, tmp_path, monkeypatch):
    mod = sys.modules["webapp_under_test"]
    monkeypatch.setattr(mod, "DB_PATH", str(_bd_minima(tmp_path)))
    r = client.get("/health")
    assert r.status_code == 200
    assert r.get_json()["ok"] is True


def test_health_avisa_si_la_bd_no_esta(client, tmp_path, monkeypatch):
    """Una BD ausente es 503, no un 200 optimista."""
    mod = sys.modules["webapp_under_test"]
    monkeypatch.setattr(mod, "DB_PATH", str(tmp_path / "no-existe.db"))
    r = client.get("/health")
    assert r.status_code == 503
    assert r.get_json()["ok"] is False


def test_favicon_no_da_404(client):
    r = client.get("/favicon.ico")
    assert r.status_code == 200
    assert "svg" in r.headers["Content-Type"]


def test_viaje_inexistente_es_404_no_500(client, tmp_path, monkeypatch):
    mod = sys.modules["webapp_under_test"]
    monkeypatch.setattr(mod, "DB_PATH", str(_bd_minima(tmp_path)))
    r = client.get("/api/trip/999999")
    assert r.status_code == 404, "dict(None) → TypeError → 500"


def test_chartjs_es_local_y_el_menu_lleva_dtcs(client):
    html = client.get("/").get_data(as_text=True)
    assert "cdn.jsdelivr.net" not in html, "Chart.js vuelve a depender de un CDN"
    assert re.search(r'src="/static/chart\.umd\.min\.js\?v=\d+"', html)
    assert client.get("/static/chart.umd.min.js").status_code == 200
    assert 'href="/dtcs"' in html, "la página de DTCs no está en el menú"


def test_api_grande_va_comprimida(client, tmp_path, monkeypatch):
    mod = sys.modules["webapp_under_test"]
    db = _bd_minima(tmp_path)
    con = sqlite3.connect(db)
    for i in range(2, 25):
        con.execute("INSERT INTO sessions (id, start_time, status, distance_km) "
                    "VALUES (?, ?, 'completed', ?)",
                    (i, f"2026-10-{i:02d}T08:00:00", 10 + i))
    con.commit()
    con.close()
    monkeypatch.setattr(mod, "DB_PATH", str(db))
    r = client.get("/api/trips", headers={"Accept-Encoding": "gzip"})
    assert r.status_code == 200
    cuerpo = r.get_data()
    assert r.headers.get("Content-Encoding") == "gzip", (
        "sin gzip: el mapa y los listados viajan sin comprimir")
    import gzip as _gz
    assert len(json.loads(_gz.decompress(cuerpo))) == 24


def test_mapa_puede_pedirse_sin_tracks(client, tmp_path, monkeypatch):
    """`?tracks=0` no debe parsear los 180 GPX (1,9 MB) para nada."""
    mod = sys.modules["webapp_under_test"]
    db = _bd_minima(tmp_path)
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE positions (id INTEGER PRIMARY KEY, session_id "
                "INTEGER, timestamp TEXT, lat REAL, lon REAL, gps_speed REAL)")
    con.execute("INSERT INTO positions (session_id, timestamp, lat, lon, "
                "gps_speed) VALUES (1, '2026-10-28T14:11:00', 43.36, -5.85, 80)")
    con.commit()
    con.close()
    monkeypatch.setattr(mod, "DB_PATH", str(db))

    llamadas = []
    monkeypatch.setattr(mod, "parse_gpx", lambda ruta: llamadas.append(ruta) or [])
    r = client.get("/api/map?session_id=1&tracks=0")
    assert r.status_code == 200
    assert llamadas == [], "se han parseado los GPX con tracks=0"
    assert len(r.get_json()["positions"]) == 1
