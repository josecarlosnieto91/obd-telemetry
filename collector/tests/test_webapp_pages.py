"""Render de las pantallas de la webapp: HTTP 200 + JavaScript VÁLIDO.

Regresión del bug del 2026-09-13: `const CAPACITY = {{ capacity_l }};` en
`refuels.html` sin que la ruta pasara la variable → Jinja lo dejaba vacío →
`const CAPACITY = ;` → SyntaxError que mataba TODO el JS de la página (el
listado y el formulario de repostajes parecían "vacíos", sin error visible).

Un `node --check` por bloque <script> caza esa clase de fallo (y cualquier
error de sintaxis introducido al tocar las plantillas).
"""
import importlib.util
import os
import re
import shutil
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
