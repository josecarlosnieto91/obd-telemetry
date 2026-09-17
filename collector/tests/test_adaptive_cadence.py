"""Cadencia adaptativa del recolector y freno del rescate del bridge.

Antes el bucle dormía `INTERVAL` (30 s) pasara lo que pasara, y con el coche
parado —cuando el bridge no responde— eso significaba dos cosas:

  1. 2.880 despertares al día dentro del coche para no leer nada.
  2. Un `am startservice` del bridge CADA ciclo (el patrón del incidente
     «The VEGATES is starting continuously»).

Estos tests protegen las dos propiedades: sin lectura la espera se dobla hasta
un techo y vuelve sola al recuperar el bus, y el rescate solo se dispara tras
varios fallos seguidos y con un hueco mínimo entre intentos.
"""
import importlib.util
import os

HERE = os.path.dirname(os.path.abspath(__file__))
COLLECTOR_DIR = os.path.dirname(HERE)
FUENTE = os.path.join(COLLECTOR_DIR, "obd_local_collector.py")


def _load():
    spec = importlib.util.spec_from_file_location("under_test_cadence", FUENTE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


coll = _load()


# ── Espera entre ciclos ──────────────────────────────────────────────────────

def test_con_lectura_mantiene_la_cadencia_base():
    assert coll.siguiente_espera(True, 30) == coll.INTERVAL
    # Aunque venga de una racha parada, una lectura la resetea al instante
    assert coll.siguiente_espera(True, coll.IDLE_INTERVAL) == coll.INTERVAL


def test_sin_lectura_la_espera_se_dobla():
    assert coll.siguiente_espera(False, 30) == 60
    assert coll.siguiente_espera(False, 60) == 120


def test_sin_lectura_no_pasa_del_techo():
    assert coll.siguiente_espera(False, coll.IDLE_INTERVAL) == coll.IDLE_INTERVAL
    # Techo coherente: espaciar más que el hueco del rescate no aporta nada
    assert coll.IDLE_INTERVAL <= coll.REVIVE_MIN_GAP


def test_el_techo_recorta_lo_que_sea():
    assert coll.siguiente_espera(False, 10_000) == coll.IDLE_INTERVAL


def test_la_rampa_es_corta():
    """En 2 ciclos sin datos ya está en el techo: no tarda media hora en
    dejar de molestar."""
    espera, ciclos = coll.INTERVAL, 0
    while espera < coll.IDLE_INTERVAL:
        espera = coll.siguiente_espera(False, espera)
        ciclos += 1
    assert ciclos <= 2


# ── Rescate del bridge ───────────────────────────────────────────────────────

def test_no_rescata_al_primer_fallo():
    """Un fallo suelto es contienda con el sniffing, no un bridge muerto."""
    assert coll.toca_rescatar(1, None, 1000) is False
    assert coll.toca_rescatar(coll.REVIVE_AFTER_FALLOS - 1, None, 1000) is False


def test_rescata_tras_varios_fallos_seguidos():
    assert coll.toca_rescatar(coll.REVIVE_AFTER_FALLOS, None, 1000) is True


def test_respeta_el_hueco_minimo_entre_rescates():
    ahora = 10_000
    reciente = ahora - (coll.REVIVE_MIN_GAP - 1)
    antiguo = ahora - coll.REVIVE_MIN_GAP
    assert coll.toca_rescatar(9, reciente, ahora) is False
    assert coll.toca_rescatar(9, antiguo, ahora) is True


def test_un_bridge_muerto_sigue_rescatandose():
    """El freno no puede volverlo inútil: con fallos persistentes espacia el
    rescate, no lo cancela."""
    rescates = 0
    ultimo = None
    for t in range(0, 7200, 30):        # 2 h fallando
        if coll.toca_rescatar(50, ultimo, t):
            rescates += 1
            ultimo = t
    assert rescates >= 10            # antes serían 240 lanzamientos


# ── Cableado en el bucle (regresión) ─────────────────────────────────────────

def test_el_bucle_usa_la_espera_adaptativa():
    fuente = open(FUENTE).read()
    assert "time.sleep(espera)" in fuente
    assert "time.sleep(INTERVAL)" not in fuente
    assert "espera = siguiente_espera(reading is not None, espera)" in fuente


def test_el_bucle_no_rescata_en_cada_ciclo():
    fuente = open(FUENTE).read()
    assert "if toca_rescatar(fallos_bridge, ultimo_rescate, time.time()):" in fuente


def test_el_escaneo_de_pids_espera_al_bus():
    fuente = open(FUENTE).read()
    assert "if reading is not None and (" in fuente
