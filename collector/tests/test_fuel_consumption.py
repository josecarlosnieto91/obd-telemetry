"""Tests del consumo real (fuel_consumption).

Caso de referencia REAL (repostaje 15/09/2026, Eroski A-8): 55,03 L del surtidor,
663,6 km recorridos entre llenados, rango 920 → 65 km.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import fuel_consumption as fc  # noqa: E402

KM_PER_L = 15.537          # calibración vigente del depósito
LITROS_SURTIDOR = 55.03
KM_TANQUE = 663.6
CAIDA_RANGO = 920.0 - 65.0  # 855 km


def test_l100_caso_real():
    assert fc.l100(KM_TANQUE, LITROS_SURTIDOR) == pytest.approx(8.3, abs=0.05)


def test_caida_de_rango_da_los_litros_del_surtidor():
    """La caída de rango / calibración debe reproducir los litros reales."""
    litros = fc.liters_from_range(CAIDA_RANGO, KM_PER_L)
    assert litros == pytest.approx(LITROS_SURTIDOR, abs=0.1)


def test_consumo_desde_rango_cuadra_con_el_surtidor():
    """El mismo dato por las dos vías (surtidor vs rango) da la misma cifra."""
    por_rango = fc.consumption_from_range(CAIDA_RANGO, KM_TANQUE, KM_PER_L)
    assert por_rango == pytest.approx(8.3, abs=0.05)


def test_l100_rechaza_datos_absurdos():
    assert fc.l100(0, 5) is None            # sin km
    assert fc.l100(100, 0) is None          # sin litros
    assert fc.l100(100, None) is None
    assert fc.l100(-5, 3) is None
    assert fc.l100(1, 5) is None            # 500 l/100km: motor parado/GPS perdido


def test_liters_from_range_protege_entradas():
    assert fc.liters_from_range(None, KM_PER_L) is None
    assert fc.liters_from_range(0, KM_PER_L) is None
    assert fc.liters_from_range(-10, KM_PER_L) is None
    assert fc.liters_from_range(100, 0) is None


def test_pick_usa_el_deposito_en_curso_cuando_hay_km():
    valor, etiqueta = fc.pick_current_or_last(250.0, 8.3, 7.9)
    assert valor == 8.3
    assert etiqueta is not None
    assert "depósito en curso" in etiqueta and "250" in etiqueta


def test_pick_ignora_el_en_curso_con_pocos_km():
    """Con 120 km la ventana es corta: mejor el último depósito completo."""
    valor, etiqueta = fc.pick_current_or_last(120.0, 8.3, 7.9)
    assert valor == 7.9
    assert etiqueta == "último depósito completo"


def test_pick_descarta_cifras_imposibles():
    """41 l/100km es ruido del rango (recalcula y sube 40 km solo), no consumo."""
    valor, etiqueta = fc.pick_current_or_last(400.0, 41.0, 8.3)
    assert valor == 8.3 and etiqueta == "último depósito completo"
    # y si tampoco hay último creíble, no se inventa nada
    assert fc.pick_current_or_last(400.0, 41.0, 45.0) == (None, None)
    assert fc.pick_current_or_last(400.0, 3.8, None) == (None, None)


def test_pick_cae_al_ultimo_completo_recien_repostado():
    """Con 8 km desde el repostaje, la cifra del depósito no vale: la anterior sí."""
    valor, etiqueta = fc.pick_current_or_last(8.0, 12.5, 8.3)
    assert valor == 8.3
    assert etiqueta == "último depósito completo"


def test_pick_sin_ultimo_y_pocos_km_no_ensena_cifra():
    assert fc.pick_current_or_last(8.0, 12.5, None) == (None, None)


def test_plausible_rango():
    assert fc.plausible(4.5) is True
    assert fc.plausible(8.3) is True
    assert fc.plausible(20.0) is True
    assert fc.plausible(4.4) is False      # este coche gasta ~8, no 4
    assert fc.plausible(41.0) is False
    assert fc.plausible(None) is False


def test_pick_sin_datos():
    assert fc.pick_current_or_last(0, None, None) == (None, None)
    assert fc.pick_current_or_last(None, None, None) == (None, None)
