"""Seguridad de los POST y verdad de los datos (goal 2026-10).

Dos defectos reales que esto vigila:

1. `/api/dtcs/clear` SIN cuerpo marcaba como borrados TODOS los códigos
   (`UPDATE dtc SET cleared=1`). Así quedó oculto P0113 el 2026-10-28: el
   dashboard tenía un botón con una confirmación y nadie sabía qué había pasado.
2. `/api/alerts` devolvía 100 de 195 alertas y no lo decía: la página parecía
   completa cuando faltaban 95.
"""
import importlib.util
import os
import sqlite3
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
WEBAPP = os.path.join(os.path.dirname(HERE), "webapp")

TOKEN = "token-de-prueba"


@pytest.fixture(scope="module")
def app_mod():
    if "webapp_under_test" in sys.modules:
        return sys.modules["webapp_under_test"]
    sys.path.insert(0, WEBAPP)
    spec = importlib.util.spec_from_file_location(
        "webapp_under_test", os.path.join(WEBAPP, "app.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["webapp_under_test"] = mod
    spec.loader.exec_module(mod)
    mod.app.config["TESTING"] = True
    return mod


@pytest.fixture
def client(app_mod, tmp_path, monkeypatch):
    """Cliente con BD sintética, token fijo y config limpia."""
    db = tmp_path / "obd.db"
    con = sqlite3.connect(db)
    con.execute("""CREATE TABLE dtc (
        id INTEGER PRIMARY KEY AUTOINCREMENT, session_id INTEGER,
        timestamp TEXT NOT NULL, code TEXT NOT NULL, description TEXT,
        cleared INTEGER DEFAULT 0)""")
    con.execute("INSERT INTO dtc (session_id, timestamp, code, description, cleared) "
                "VALUES (1, '2026-10-28T14:36:20', 'P0113', 'Sensor temp aire', 0)")
    con.execute("INSERT INTO dtc (session_id, timestamp, code, description, cleared) "
                "VALUES (1, '2026-09-20T23:13:03', 'P0213', 'Frio arranque', 1)")
    con.execute("""CREATE TABLE alerts (
        id INTEGER PRIMARY KEY AUTOINCREMENT, session_id INTEGER,
        timestamp TEXT NOT NULL, category TEXT NOT NULL,
        severity TEXT DEFAULT 'info', message TEXT NOT NULL,
        acknowledged INTEGER DEFAULT 0)""")
    con.execute("CREATE TABLE sessions (id INTEGER PRIMARY KEY, start_time TEXT)")
    for i in range(1, 196):
        con.execute("INSERT INTO alerts (session_id, timestamp, category, severity, message) "
                    "VALUES (1, ?, 'uso', 'info', ?)",
                    (f"2026-10-{(i % 28) + 1:02d}T10:00:00", f"Trayecto corto ({i % 9 + 1} min)"))
    con.commit()
    con.close()
    monkeypatch.setattr(app_mod, "DB_PATH", str(db))
    monkeypatch.setattr(app_mod, "_api_token", lambda: TOKEN)
    app_mod._ensure_auditoria_dtc()   # añade cleared_ts/cleared_by a esta BD
    app_mod.app.config["TESTING"] = True
    return app_mod.app.test_client()


def cleared_count(client):
    import importlib
    mod = sys.modules["webapp_under_test"]
    con = sqlite3.connect(mod.DB_PATH)
    n = con.execute("SELECT COUNT(*) FROM dtc WHERE cleared=1").fetchone()[0]
    con.close()
    return n


# ── Borrado de DTCs ───────────────────────────────────────────

def test_dtcs_clear_sin_codigos_es_400_y_no_toca_nada(client):
    antes = cleared_count(client)
    r = client.post("/api/dtcs/clear", json={}, headers={"X-OBD-Token": TOKEN})
    assert r.status_code == 400, "sin `codes` NO puede marcar todo el histórico"
    assert cleared_count(client) == antes


def test_dtcs_clear_con_lista_marca_solo_ese_codigo_y_deja_auditoria(client):
    r = client.post("/api/dtcs/clear", json={"codes": ["P0113"], "motivo": "test"},
                    headers={"X-OBD-Token": TOKEN})
    assert r.status_code == 200, r.get_data(as_text=True)
    mod = sys.modules["webapp_under_test"]
    con = sqlite3.connect(mod.DB_PATH)
    fila = con.execute("SELECT cleared, cleared_ts, cleared_by FROM dtc WHERE code='P0113'").fetchone()
    con.close()
    assert fila[0] == 1
    assert fila[1], "falta la marca de tiempo del borrado"
    assert fila[2] == "test", "falta quién lo marcó"


# ── Token en los POST ─────────────────────────────────────────

@pytest.mark.parametrize("url,cuerpo", [
    ("/api/dtcs/clear", {"codes": ["P0113"]}),
    ("/api/alerts/acknowledge", {"id": 1}),
    ("/api/refuels/manual", {"liters": 10}),
    ("/api/services", {"maintenance_id": "oil"}),
])
def test_post_sin_token_es_401(client, url, cuerpo):
    r = client.post(url, json=cuerpo)
    assert r.status_code == 401, f"{url} sin token debería ser 401"


def test_post_con_token_pasa(client):
    r = client.post("/api/alerts/acknowledge", json={"id": 1},
                    headers={"X-OBD-Token": TOKEN})
    assert r.status_code == 200


# ── Total de alertas ──────────────────────────────────────────

def test_alerts_dice_el_total_real(client):
    r = client.get("/api/alerts")
    assert r.status_code == 200
    assert len(r.get_json()) == 100, "la página sigue trayendo 100"
    assert r.headers["X-Total-Count"] == "195", "y ahora dice cuántas hay en total"


def test_alerts_pagina_y_filtra(client):
    r = client.get("/api/alerts?limit=10&offset=190")
    assert len(r.get_json()) == 5
    assert r.headers["X-Total-Count"] == "195"
    assert r.headers["X-Offset"] == "190"
