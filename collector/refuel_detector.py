#!/usr/bin/env python3
"""Detector de repostajes — fuente primaria: rango CAN (can_readings.range_km).

v2 (2026-08-23): el C4 Grand Picasso no expone fuel_level (PID 012F → NULL),
así que el detector v1 (basado en fuel_level) nunca detectaba nada. La señal
fiable es el RANGO del decodificador Witson (range_km en can_readings), que
sube bruscamente al repostar.

  litros_introducidos ≈ (rango_despues − rango_antes) / range_km_per_l
  range_km_per_l se calibra con repostajes manuales conocidos
  (config vehicle.range_km_per_l; por defecto 21.04 = 9,98L → +210 km).

Mantiene fallback a fuel_level si algún día el vehículo lo expone.
"""
import json
import os
import sqlite3
import sys
from datetime import datetime

OBD_DB = os.path.expanduser("~/.hermes/data/obd_telemetry.db")
CTX_DB = os.path.expanduser("~/.hermes/data/context/context.db")
CONFIG_PATH = os.path.expanduser("~/.hermes/scripts/obd_vehicle_config.json")

DEFAULT_CAPACITY = 60.0      # litros — C4 Grand Picasso I (fuente: Motorpasión)
DEFAULT_KM_PER_L = 21.04     # calibrado con el repostaje real de 2026-08-22
DEFAULT_MIN_JUMP_KM = 30.0   # salto mínimo de rango (km) para considerarlo repostaje
DEFAULT_MIN_JUMP_PCT = 8.0   # fallback fuel_level (%)


def load_config():
    try:
        with open(CONFIG_PATH) as fh:
            return json.load(fh)
    except Exception:
        return {}


def get_price_at(lat, lon):
    """Precio del diésel en la estación más cercana (si hay API configurada)."""
    try:
        cfg = load_config()
        api = cfg.get("fuel_price_api", {})
        if not api.get("url"):
            return None, None
        import urllib.request
        url = api["url"].replace("{lat}", str(lat)).replace("{lon}", str(lon))
        with urllib.request.urlopen(url, timeout=8) as r:
            data = json.loads(r.read().decode())
        items = data.get("ListaEESSPrecio", [])
        if not items:
            return None, None
        best = min(items, key=lambda x: float(x.get("PrecioGasoleoA", "999") or 999))
        p = float(best.get("PrecioGasoleoA", 0))
        if p <= 0:
            return None, None
        return p, best
    except Exception:
        return None, None


def main():
    cfg = load_config()
    vehicle = cfg.get("vehicle", {})
    thr = cfg.get("thresholds", {})
    capacity = float(vehicle.get("tank_capacity_l", DEFAULT_CAPACITY))
    km_per_l = float(vehicle.get("range_km_per_l", DEFAULT_KM_PER_L))
    min_jump_km = float(thr.get("refuel_min_jump_km", DEFAULT_MIN_JUMP_KM))
    min_jump_pct = float(thr.get("refuel_min_jump_pct", DEFAULT_MIN_JUMP_PCT))
    # Rango esperado con depósito lleno (calibrable) — para marcar full_tank
    full_range_km = float(vehicle.get("full_range_km", capacity * km_per_l))
    # NOTA: no sumar reserva al cálculo. El km_per_l está calibrado con el
    # surtidor real (54,35 L → 973 km de salto = 17,90 km/L), así que la
    # reserva (~5,6 L con rango a 0) YA queda absorbida en el factor.

    conn = sqlite3.connect(OBD_DB)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    c.execute("""CREATE TABLE IF NOT EXISTS refuels (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT NOT NULL,
        prev_ts TEXT,
        fuel_before REAL,
        fuel_after REAL,
        jump_pct REAL,
        liters REAL,
        full_tank INTEGER DEFAULT 0,
        session_id INTEGER,
        price_per_l REAL,
        cost REAL,
        station TEXT,
        source TEXT DEFAULT 'level',
        UNIQUE(prev_ts, ts)
    )""")
    for col in ("price_per_l", "cost", "station", "source"):
        try:
            c.execute(f"ALTER TABLE refuels ADD COLUMN {col} "
                      + ("TEXT" if col in ("station", "source") else "REAL"))
        except sqlite3.OperationalError:
            pass  # ya existe

    def position_near(ts):
        try:
            c.execute(
                "SELECT lat, lon FROM positions "
                "ORDER BY ABS(julianday(timestamp) - julianday(?)) LIMIT 1",
                (ts,),
            )
            row = c.fetchone()
            if row and row["lat"] is not None and row["lon"] is not None:
                return row["lat"], row["lon"]
        except Exception:
            pass
        return None

    def insert_refuel(ts, prev_ts, before, after, liters, full, session_id, source):
        """Inserta repostaje si no existe (UNIQUE prev_ts+ts)."""
        price, cost, station = None, None, None
        pos = position_near(ts)
        if pos:
            try:
                p, s = get_price_at(pos[0], pos[1])
                if p:
                    price = round(p, 3)
                    cost = round(liters * p, 2)
                    station = f"{s.get('rotulo', '')} · {s.get('municipio', '')}".strip(" ·")
            except Exception as e:
                sys.stderr.write(f"fuel price fail: {e}\n")
        try:
            c.execute(
                """INSERT OR IGNORE INTO refuels
                   (ts, prev_ts, fuel_before, fuel_after, jump_pct,
                    liters, full_tank, session_id, price_per_l, cost, station, source)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (ts, prev_ts, before, after, round(after - before, 1),
                 round(liters, 1), 1 if full else 0,
                 session_id, price, cost, station, source),
            )
        except sqlite3.Error as e:
            sys.stderr.write(f"refuel insert fail: {e}\n")
        return c.rowcount

    reports = []

    # ── Fuente 1 (PRIMARIA): rango CAN (can_readings.range_km) ────────────
    try:
        can_cols = [r[1] for r in c.execute("PRAGMA table_info(can_readings)")]
        if "range_km" in can_cols:
            can_rows = c.execute(
                "SELECT ts, range_km FROM can_readings WHERE range_km IS NOT NULL "
                "ORDER BY ts").fetchall()
            prev = None
            for r in can_rows:
                if prev is not None:
                    jump_km = r["range_km"] - prev["range_km"]
                    # Salto positivo de rango = repostaje (el rango solo sube al
                    # repostar o por maniobras de decodificador; min_jump filtra)
                    if jump_km >= min_jump_km:
                        liters = jump_km / km_per_l
                        # Lleno si el rango tras repostar está cerca del máximo
                        # (el depósito del C4 no es lineal: 60 L no implican
                        # que un llenado dé litros >= 80% capacidad)
                        full = 1 if r["range_km"] >= full_range_km * 0.8 else 0
                        n = insert_refuel(r["ts"], prev["ts"],
                                          prev["range_km"], r["range_km"],
                                          liters, full, None, "can_range")
                        if n:
                            reports.append((r["ts"], prev["range_km"], r["range_km"],
                                            liters, full, None, None, None))
                prev = r
    except Exception as e:
        sys.stderr.write(f"can range scan fail: {e}\n")

    # ── Fuente 2 (fallback): fuel_level (si algún día el vehículo lo expone)
    try:
        rows = c.execute(
            "SELECT timestamp, fuel_level, session_id FROM readings "
            "WHERE fuel_level IS NOT NULL ORDER BY timestamp").fetchall()
        prev = None
        for r in rows:
            if prev is not None:
                jump = r["fuel_level"] - prev["fuel_level"]
                if jump >= min_jump_pct:
                    liters = jump / 100.0 * capacity
                    full = 1 if r["fuel_level"] >= 90 else 0
                    n = insert_refuel(r["timestamp"], prev["timestamp"],
                                      prev["fuel_level"], r["fuel_level"],
                                      liters, full, r["session_id"], "level")
                    if n:
                        reports.append((r["timestamp"], prev["fuel_level"],
                                        r["fuel_level"], liters, full,
                                        None, None, None))
            prev = r
    except Exception:
        pass

    conn.commit()

    if reports:
        try:
            ctx = sqlite3.connect(CTX_DB)
            cc = ctx.cursor()
            for ts, before, after, liters, full, price, cost, station in reports:
                detail = json.dumps({
                    "litros": round(liters, 1),
                    "rango_antes_km": before, "rango_despues_km": after,
                    "deposito_lleno": bool(full),
                    "capacidad_l": capacity,
                    "precio_l": price, "coste": cost, "estacion": station,
                }, ensure_ascii=False)
                try:
                    cc.execute(
                        "INSERT INTO events (ts, ts_unix, type, value, detail) "
                        "VALUES (?,?,?,?,?)",
                        (ts, int(datetime.fromisoformat(ts).timestamp()),
                         "vehiculo", "repostaje", detail),
                    )
                except Exception as e:
                    sys.stderr.write(f"ctx event fail: {e}\n")
            ctx.commit()
            ctx.close()
        except Exception as e:
            sys.stderr.write(f"ctx open fail: {e}\n")

    conn.close()

    if reports:
        lines = []
        for ts, before, after, liters, full, price, cost, station in reports:
            d = datetime.fromisoformat(ts)
            block = [
                f"⛽ Repostaje detectado — {d.strftime('%d/%m %H:%M')}",
                f"📊 rango {before:.0f} → {after:.0f} km (+{after - before:.0f})",
                f"🛢️ ~{liters:.1f} L estimados (depósito {capacity:.0f} L)",
            ]
            if full:
                block.append("✅ Depósito lleno")
            if price and cost:
                block.append(f"💶 {cost:.2f} € @ {price:.3f} €/L" + (f" — {station}" if station else ""))
            lines.append("\n".join(block))
        print("\n\n".join(lines))


if __name__ == "__main__":
    main()
