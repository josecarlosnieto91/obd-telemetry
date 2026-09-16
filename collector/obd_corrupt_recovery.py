#!/usr/bin/env python3
"""Recupera en obd_telemetry.db las lecturas/posiciones que quedaron sin importar
en un fichero válido de processed/corrupt/ (tablet que subió el fichero a medias).

Criterio de asignación de sesión (el mismo que usa el importador):

1. Los datos huérfanos se agrupan en bloques de viaje: un hueco > SESSION_GAP_MIN
   entre datos consecutivos abre un bloque nuevo.
2. Cada bloque se cuelga de la sesión del ancla más cercana del maestro:
   - el dato del maestro inmediatamente anterior al bloque, si está a ≤ 60 min;
   - si no, el inmediatamente posterior al bloque, si está a ≤ 60 min;
   - si no, un dato del maestro que caiga DENTRO del bloque (el bloque es el
     principio del mismo viaje que ya se importó a medias);
   - si no hay ancla, se crea una sesión nueva con start_time = inicio del bloque.

Uso:  obd_corrupt_recovery.py <fichero_origen> [--apply]
Sin --apply solo informa (dry-run).
"""
import bisect
import datetime
import os
import shutil
import sqlite3
import sys

MASTER = os.path.expanduser("~/.hermes/data/obd_telemetry.db")
GAP_MIN = 60
READINGS_COLS = ("timestamp", "rpm", "speed", "coolant_temp", "engine_load",
                 "intake_temp", "throttle_pos", "fuel_level", "voltage", "maf",
                 "dtc_count", "dtc_codes", "map", "ambient", "fuel_pressure",
                 "fuel_rate")
POSITIONS_COLS = ("timestamp", "lat", "lon", "gps_speed", "bearing",
                  "accuracy", "altitude", "provider")


def iso(ts):
    try:
        return datetime.datetime.fromisoformat(ts)
    except Exception:
        return None


def minutes(a, b):
    da, db = iso(a), iso(b)
    if da is None or db is None:
        return float("inf")
    return abs((da - db).total_seconds()) / 60.0


def main():
    src_path = os.path.abspath(sys.argv[1])
    apply = "--apply" in sys.argv

    src = sqlite3.connect(f"file:{src_path}?mode=ro", uri=True)
    mst = sqlite3.connect(MASTER, timeout=30.0)
    mst.execute("PRAGMA busy_timeout=30000")

    # --- anclas del maestro: timestamp -> session_id (lecturas y posiciones) ---
    anchor = {}
    for table in ("readings", "positions"):
        for ts, sid in mst.execute(f"SELECT timestamp, session_id FROM {table}"):
            anchor.setdefault(ts, sid)
    akeys = sorted(anchor)

    # --- filas huérfanas por tabla ---
    todo = {}          # table -> (cols, índice de timestamp, filas ausentes)
    ts_all = set()
    for table, cols in (("readings", READINGS_COLS), ("positions", POSITIONS_COLS)):
        src_cols = [r[1] for r in src.execute(f"PRAGMA table_info({table})")]
        cols = tuple(c for c in cols if c in src_cols)
        have = {r[0] for r in mst.execute(f"SELECT timestamp FROM {table}")}
        rows = src.execute(f"SELECT {','.join(cols)} FROM {table} ORDER BY timestamp").fetchall()
        ti = cols.index("timestamp")
        missing = [r for r in rows if r[ti] not in have]
        todo[table] = (cols, ti, missing)
        ts_all |= {r[ti] for r in missing}
        print(f"{table}: origen={len(rows)} sin importar={len(missing)}")

    if not ts_all:
        print("nada que recuperar")
        return 0

    # --- bloques de viaje sobre el conjunto de datos huérfanos ---
    ts_sorted = sorted(ts_all)
    blocks, cur = [], [ts_sorted[0]]
    for a, b in zip(ts_sorted, ts_sorted[1:]):
        if minutes(a, b) > GAP_MIN:
            blocks.append(cur)
            cur = []
        cur.append(b)
    blocks.append(cur)

    # --- sesión destino de cada bloque ---
    assign = {}
    for blk in blocks:
        start, end = blk[0], blk[-1]
        sid = None
        i = bisect.bisect_left(akeys, start)
        if i > 0 and minutes(akeys[i - 1], start) <= GAP_MIN:
            sid = anchor[akeys[i - 1]]                       # continúa el viaje anterior
        if sid is None:
            j = bisect.bisect_right(akeys, end)
            if j < len(akeys) and minutes(end, akeys[j]) <= GAP_MIN:
                sid = anchor[akeys[j]]                       # mismo viaje, cola ya importada
        if sid is None:
            inside = [k for k in akeys if start <= k <= end]  # ancla dentro del bloque
            if inside:
                sid = anchor[min(inside, key=lambda k: minutes(k, start))]
        if sid is None and apply:
            sid = mst.execute(
                "INSERT INTO sessions (start_time, status) VALUES (?, 'active')",
                (start,)).lastrowid
            print(f"  sesión nueva {sid} (start {start})")
        if sid is None:
            print(f"  bloque {start} -> {end} ({len(blk)} datos): haría falta SESIÓN NUEVA")
            continue
        for ts in blk:
            assign[ts] = sid
        print(f"  bloque {start} -> {end} ({len(blk)} datos) → sesión {sid}")

    if not apply:
        for table, (cols, ti, missing) in todo.items():
            print(f"  {table}: a insertar {sum(1 for r in missing if assign.get(r[ti]))}")
        print("\n[dry-run] sin cambios. Repetir con --apply")
        return 0

    backup = MASTER + ".bak-" + datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    shutil.copy2(MASTER, backup)
    print(f"\ncopia de seguridad: {backup}")

    affected = set()
    for table, (cols, ti, missing) in todo.items():
        rows = [(assign[r[ti]],) + tuple(r) for r in missing if assign.get(r[ti])]
        if not rows:
            continue
        allcols = ("session_id",) + cols
        ph = ",".join("?" * len(allcols))
        mst.executemany(f"INSERT INTO {table} ({','.join(allcols)}) VALUES ({ph})", rows)
        affected |= {r[0] for r in rows}

    # start_time real de las sesiones afectadas (el resumen de viaje usa la 1ª
    # lectura, pero el registro histórico no debe arrastrar una hora falsa)
    for sid in sorted(affected):
        row = mst.execute("SELECT MIN(timestamp) FROM readings WHERE session_id=?",
                          (sid,)).fetchone()
        cur_ = mst.execute("SELECT start_time FROM sessions WHERE id=?", (sid,)).fetchone()
        if row and row[0] and cur_ and row[0] < cur_[0]:
            mst.execute("UPDATE sessions SET start_time=? WHERE id=?", (row[0], sid))
            print(f"  sesión {sid}: start_time {cur_[0]} -> {row[0]}")
    mst.commit()

    for table, (cols, ti, missing) in todo.items():
        have = {r[0] for r in mst.execute(f"SELECT timestamp FROM {table}")}
        left = sum(1 for r in missing if r[ti] not in have)
        print(f"verificación {table}: siguen sin estar en el maestro = {left}")
    print(f"sesiones afectadas: {sorted(affected)}")
    mst.close()
    src.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
