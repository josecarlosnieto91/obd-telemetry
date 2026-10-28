#!/usr/bin/env python3
"""Higiene de la BD de telemetría: backups y filas de deshacer.

No borra nada que no sea una copia de seguridad o un residuo de fusión.
Por defecto SIMULA: con `--aplicar` ejecuta.

    python3 limpiar_backups.py                 # enseña qué haría
    python3 limpiar_backups.py --aplicar       # lo hace
    python3 limpiar_backups.py --aplicar --undo-dias 90

Política de backups de obd_telemetry.db:
  · se conservan siempre los 3 más recientes;
  · y el más reciente de cada mes anterior (para poder volver atrás lejos).

Política de sessions_merged_undo (fila por fusión deshacible):
  · se conservan las de los últimos N días (90 por defecto);
  · lo anterior se cuenta y solo se borra con --aplicar.
"""
import argparse
import os
import re
import sqlite3
from datetime import datetime, timedelta

DB = os.path.expanduser("~/.hermes/data/obd_telemetry.db")
PATRON = re.compile(r"^" + re.escape(os.path.basename(DB)) + r"\.bak-(.+)$")


def backups():
    carpeta = os.path.dirname(DB)
    base = os.path.basename(DB)
    salida = []
    for f in os.listdir(carpeta):
        if f.startswith(base + ".bak-"):
            ruta = os.path.join(carpeta, f)
            salida.append((os.path.getmtime(ruta), ruta))
    return sorted(salida, reverse=True)


def plan_backups(conservar_recientes=3):
    lista = backups()
    if len(lista) <= conservar_recientes:
        return [], lista
    quedan = {r for _, r in lista[:conservar_recientes]}
    vistos_mes = set()
    for mtime, ruta in lista[conservar_recientes:]:
        mes = datetime.fromtimestamp(mtime).strftime("%Y-%m")
        if mes not in vistos_mes:
            vistos_mes.add(mes)
            quedan.add(ruta)
    borrar = [r for _, r in lista if r not in quedan]
    return borrar, [r for _, r in lista if r in quedan]


def plan_undo(dias):
    if not os.path.exists(DB):
        return None, "BD no encontrada"
    con = sqlite3.connect(DB, timeout=10)
    try:
        total = con.execute("SELECT COUNT(*) FROM sessions_merged_undo").fetchone()[0]
        limite = (datetime.now() - timedelta(days=dias)).isoformat()
        viejas = con.execute(
            "SELECT COUNT(*) FROM sessions_merged_undo WHERE ts < ?", (limite,)).fetchone()[0]
        return (total, viejas, limite), None
    except sqlite3.Error as e:
        return None, str(e)
    finally:
        con.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--aplicar", action="store_true", help="ejecuta (por defecto simula)")
    ap.add_argument("--conservar", type=int, default=3, help="backups recientes a conservar")
    ap.add_argument("--undo-dias", type=int, default=90, help="antigüedad de las filas de deshacer")
    args = ap.parse_args()

    borrar, quedan = plan_backups(args.conservar)
    print(f"Backups: {len(quedan) + len(borrar)} en total · conservar {len(quedan)} · borrar {len(borrar)}")
    for r in borrar:
        print("  - ", r)
    for r in quedan:
        print("  = ", r)

    datos, err = plan_undo(args.undo_dias)
    if err:
        print(f"Fusiones deshacibles: no se pudo leer ({err})")
    else:
        total, viejas, limite = datos  # type: ignore[misc]
        print(f"Fusiones deshacibles: {total} filas · anteriores a {limite}: {viejas}")

    if not args.aplicar:
        print("\n(simulación: añade --aplicar para ejecutar)")
        return

    for r in borrar:
        os.remove(r)
    print(f"borrados {len(borrar)} backups")

    if datos is not None and not err and datos[1]:
        con = sqlite3.connect(DB, timeout=10)
        con.execute("DELETE FROM sessions_merged_undo WHERE ts < ?", (datos[2],))
        con.commit()
        con.close()
        print(f"borradas {datos[1]} filas de sessions_merged_undo")


if __name__ == "__main__":
    main()
