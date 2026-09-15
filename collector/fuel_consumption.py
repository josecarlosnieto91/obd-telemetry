#!/usr/bin/env python3
"""Consumo real del coche (depósito a depósito), NO el del cuadro.

El cuadro (consumo CAN, ID 51) marca bastante menos de lo que el coche gasta de
verdad: el 15/09/2026, con **55,03 L del surtidor y 663,6 km** recorridos, el real
fue **8,29 l/100km** mientras el cuadro marcaba 4,2-5,7.

Cómo se calcula el real (dos señales fiables, nada de física):

    litros_gastados = caída_de_rango_CAN / range_km_per_l   (rango calibrado
                      contra el surtidor; ver referencia de repostajes)
    l/100km         = 100 × litros_gastados / km_recorridos

Regla de oro: **el consumo real solo es fiable entre repostajes** (depósito a
depósito). Por trayecto suelto el rango va cuantizado (saltos de 5 km) y una ida
al pueblo de 2,5 km daría cualquier cosa.
"""

MIN_KM_DEPOSITO = 200.0   # km mínimos para fiarse del depósito en curso
MIN_L100 = 4.5            # por debajo, sospecha: este coche gasta ~8 (medido 7,9-9,6)
MAX_L100 = 20.0           # por encima, ruido del rango (medido real: ~8,3)
MAX_L100_ABSOLUTO = 60.0  # fuera de esto, dato imposible


def l100(km, liters):
    """l/100km a 1 decimal; None si el dato no es sensato."""
    if not km or km <= 0 or liters is None or liters <= 0:
        return None
    valor = 100.0 * liters / km
    return round(valor, 1) if 0 < valor <= MAX_L100_ABSOLUTO else None


def plausible(valor, minimo=MIN_L100, maximo=MAX_L100):
    """¿Es una cifra de consumo creíble para este coche?"""
    return valor is not None and minimo <= valor <= maximo


def liters_from_range(drop_km, km_per_l):
    """Litros gastados según la caída de rango CAN (calibrada)."""
    if drop_km is None or drop_km <= 0 or km_per_l <= 0:
        return None
    return drop_km / km_per_l


def consumption_from_range(drop_km, km, km_per_l):
    """l/100km a partir de la caída de rango y los km hechos."""
    return l100(km, liters_from_range(drop_km, km_per_l))


def pick_current_or_last(km_actual, real_actual, real_ultimo, min_km=MIN_KM_DEPOSITO):
    """Elige qué cifra enseñar: el depósito en curso o el último completo.

    Devuelve ``(valor, etiqueta)``. Reglas, aprendidas con datos reales:

    - El depósito en curso solo se usa con **km suficientes** (200) y si la
      cifra es **creíble** (3-20 l/100km): con poca ventana el decodificador
      recalcula el rango (sube 40 km solo) y salían 34-41 l/100km de ruido.
    - Si no, el **último depósito completo**: una cifra de hace dos semanas pero
      cierta, medida entre repostajes con los litros del surtidor.
    - Si tampoco hay, ``(None, None)``: mejor sin cifra que con una falsa (el
      resumen enseña entonces la del cuadro, etiquetada como tal).
    """
    if (km_actual is not None and km_actual >= min_km
            and plausible(real_actual)):
        return real_actual, f"depósito en curso ({km_actual:.0f} km)"
    if plausible(real_ultimo):
        return real_ultimo, "último depósito completo"
    return None, None
