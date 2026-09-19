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

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import fuel_prices          # caché local de precios oficiales (Ministerio)
except ImportError:             # el detector sigue funcionando sin precios
    fuel_prices = None

OBD_DB = os.path.expanduser("~/.hermes/data/obd_telemetry.db")
CTX_DB = os.path.expanduser("~/.hermes/data/context/context.db")
CONFIG_PATH = os.path.expanduser("~/.hermes/scripts/obd_vehicle_config.json")

DEFAULT_CAPACITY = 60.0      # litros — C4 Grand Picasso I (fuente: Motorpasión)
DEFAULT_KM_PER_L = 21.04     # calibrado con el repostaje real de 2026-08-22
DEFAULT_MIN_JUMP_KM = 30.0   # salto mínimo de rango (km) para considerarlo repostaje
DEFAULT_MIN_JUMP_PCT = 8.0   # fallback fuel_level (%)
POS_WINDOW_MIN = 15          # ventana para buscar la posición GPS del coche


def load_config():
    try:
        with open(CONFIG_PATH) as fh:
            return json.load(fh)
    except Exception:
        return {}


def get_station(lat, lon, radius_m, product):
    """Gasolinera más cercana al coche y su precio publicado.

    Usa la caché local del listado oficial (fuel_prices): si no hay caché ni
    red, devuelve None y el repostaje se registra igual, sin precio.
    """
    if fuel_prices is None:
        return None
    try:
        return fuel_prices.nearest_station(lat, lon, radius_m=radius_m, product=product)
    except Exception as e:
        sys.stderr.write(f"station lookup fail: {e}\n")
        return None


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
    # Gasolinera: de qué producto se lee el precio y a qué distancia se acepta
    price_product = vehicle.get("fuel_price_product", "Gasoleo A")
    price_radius_m = float(vehicle.get("fuel_price_radius_m", 500.0))
    # NOTA: no sumar reserva al cálculo. El km_per_l está calibrado con el
    # surtidor real (54,35 L → 973 km de salto = 17,90 km/L), así que la
    # reserva (~5,6 L con rango a 0) YA queda absorbida en el factor.

    # Mismo motivo que en merge_sessions: la BD la escriben varios jobs a la vez (este corre
    # cada 10 min) y sin busy_timeout un choque se convierte en un fallo inmediato.
    conn = sqlite3.connect(OBD_DB, timeout=10.0)
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA journal_mode=WAL")
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
    # Columnas añadidas con el tiempo (ALTER idempotente, no borra datos)
    nuevas = {"price_per_l": "REAL", "cost": "REAL", "station": "TEXT",
              "source": "TEXT", "station_addr": "TEXT",
              "station_dist_m": "REAL", "car_lat": "REAL", "car_lon": "REAL"}
    for col, tipo in nuevas.items():
        try:
            c.execute(f"ALTER TABLE refuels ADD COLUMN {col} {tipo}")
        except sqlite3.OperationalError:
            pass  # ya existe

    def position_near(ts, window_min=POS_WINDOW_MIN):
        """Posición del coche cerca del repostaje (±`window_min`).

        `datetime(timestamp)`: las posiciones guardan ISO con 'T' y las cadenas
        con 'T' NO comparan con las de `datetime()` (espacio) — el BETWEEN daba
        cero filas y el repostaje quedaba sin ubicación.
        """
        try:
            c.execute(
                "SELECT lat, lon, timestamp FROM positions "
                "WHERE datetime(timestamp) BETWEEN datetime(?, ?) AND datetime(?, ?) "
                "ORDER BY ABS(julianday(timestamp) - julianday(?)) LIMIT 1",
                (ts, f"-{window_min} minutes", ts, f"+{window_min} minutes", ts),
            )
            row = c.fetchone()
            if row and row["lat"] is not None and row["lon"] is not None:
                return row["lat"], row["lon"]
        except Exception as e:
            sys.stderr.write(f"position lookup fail: {e}\n")
        return None

    def insert_refuel(ts, prev_ts, before, after, liters, full, session_id, source):
        """Inserta repostaje si no existe (UNIQUE prev_ts+ts) y no duplica un
        repostaje lleno reciente con el mismo rango final (el mismo llenado
        llega en varias tandas de sync con timestamps distintos — FIX
        2026-08-23: guarda anti-duplicado por rango_despues similar)."""
        if full:
            # Si ya hay un repostaje lleno en las últimas 12h con rango final
            # cercano (±30 km), es el MISMO llenado re-importado → ignorar
            try:
                from datetime import timedelta
                cutoff = (datetime.fromisoformat(ts) - timedelta(hours=12)).isoformat()
                dup = c.execute(
                    "SELECT id FROM refuels WHERE full_tank=1 AND source=? "
                    "AND fuel_after BETWEEN ? AND ? AND ts >= ? ORDER BY id DESC LIMIT 1",
                    (source, after - 30, after + 30, cutoff)).fetchone()
            except Exception:
                dup = None
            if dup:
                return None
        info = {"ts": ts, "prev_ts": prev_ts, "range_before": before,
                "range_after": after, "liters": round(liters, 1),
                "full": bool(full), "price": None, "cost": None,
                "station": None, "addr": None, "dist_m": None,
                "car_lat": None, "car_lon": None,
                "radius_m": price_radius_m, "product": price_product}
        # ¿Dónde estaba el coche? ¿Qué gasolinera había? ¿A qué precio?
        pos = position_near(ts)
        if pos:
            info["car_lat"], info["car_lon"] = pos
            est = get_station(pos[0], pos[1], price_radius_m, price_product)
            if est:
                info["station"] = est["nombre"] or est["municipio"]
                info["addr"] = ", ".join(
                    x for x in (est["direccion"], est["municipio"]) if x)
                info["dist_m"] = est["dist_m"]
                if est["precio"]:
                    info["price"] = round(est["precio"], 3)
                    info["cost"] = round(liters * est["precio"], 2)
        try:
            c.execute(
                """INSERT OR IGNORE INTO refuels
                   (ts, prev_ts, fuel_before, fuel_after, jump_pct,
                    liters, full_tank, session_id, price_per_l, cost, station,
                    source, station_addr, station_dist_m, car_lat, car_lon)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (ts, prev_ts, before, after, round(after - before, 1),
                 round(liters, 1), 1 if full else 0, session_id,
                 info["price"], info["cost"], info["station"], source,
                 info["addr"], info["dist_m"], info["car_lat"], info["car_lon"]),
            )
        except sqlite3.Error as e:
            sys.stderr.write(f"refuel insert fail: {e}\n")
        info["inserted"] = c.rowcount
        return info

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
                    # Repostaje real: salto GRANDE (>= min_jump_km, 100 km) y
                    # rango previo BAJO (< 60% del lleno). FIX 2026-08-23:
                    # antes bastaba salto >= 30 km → ruido del decodificador
                    # con depósito lleno (840→880) se detectaba como repostaje.
                    # Con el depósito ya lleno el rango fluctúa pero no es un
                    # repostaje: exigir rango_prev claramente por debajo.
                    if (jump_km >= min_jump_km
                            and prev["range_km"] < full_range_km * 0.6):
                        liters = jump_km / km_per_l
                        # Lleno si el rango tras repostar está cerca del máximo
                        full = 1 if r["range_km"] >= full_range_km * 0.8 else 0
                        info = insert_refuel(r["ts"], prev["ts"],
                                             prev["range_km"], r["range_km"],
                                             liters, full, None, "can_range")
                        if info and info["inserted"]:
                            reports.append(info)
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
                    info = insert_refuel(r["timestamp"], prev["timestamp"],
                                         prev["fuel_level"], r["fuel_level"],
                                         liters, full, r["session_id"], "level")
                    if info and info["inserted"]:
                        reports.append(info)
            prev = r
    except Exception:
        pass

    conn.commit()

    if reports:
        try:
            ctx = sqlite3.connect(CTX_DB, timeout=10.0)
            ctx.execute("PRAGMA busy_timeout=10000")
            cc = ctx.cursor()
            for r in reports:
                detail = json.dumps({
                    "litros": r["liters"],
                    "rango_antes_km": r["range_before"],
                    "rango_despues_km": r["range_after"],
                    "deposito_lleno": r["full"],
                    "capacidad_l": capacity,
                    "precio_l": r["price"], "coste": r["cost"],
                    "estacion": r["station"], "direccion": r["addr"],
                    "dist_estacion_m": r["dist_m"],
                    "coche_lat": r["car_lat"], "coche_lon": r["car_lon"],
                }, ensure_ascii=False)
                try:
                    cc.execute(
                        "INSERT INTO events (ts, ts_unix, type, value, detail) "
                        "VALUES (?,?,?,?,?)",
                        (r["ts"], int(datetime.fromisoformat(r["ts"]).timestamp()),
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
        for r in reports:
            d = datetime.fromisoformat(r["ts"])
            block = [
                f"⛽ Repostaje detectado — {d.strftime('%d/%m %H:%M')}",
                f"📊 rango {r['range_before']:.0f} → {r['range_after']:.0f} km "
                f"(+{r['range_after'] - r['range_before']:.0f})",
                f"🛢️ ~{r['liters']:.1f} L estimados (depósito {capacity:.0f} L)",
            ]
            if r["full"]:
                block.append("✅ Depósito lleno")
            if r["station"]:
                donde = f"📍 {r['station']}"
                if r["addr"]:
                    donde += f" — {r['addr']}"
                if r["dist_m"] is not None:
                    donde += f" (a {r['dist_m']:.0f} m del coche)"
                block.append(donde)
            elif r["car_lat"] is not None:
                block.append(f"📍 ninguna gasolinera a <{r['radius_m']:.0f} m "
                             f"del coche ({r['car_lat']:.5f}, {r['car_lon']:.5f})")
            if r["cost"]:
                block.append(f"💶 {r['cost']:.2f} € @ {r['price']:.3f} €/L "
                             f"(precio publicado; ajústalo con el surtidor)")
            lines.append("\n".join(block))
        print("\n\n".join(lines))


if __name__ == "__main__":
    main()
