#!/usr/bin/env python3
"""Detector de repostajes por GPS + OSM — para vehículos sin PID 0x2F.

El C4 Grand Picasso I (DW10BTED4) NO expone el nivel de combustible por OBD
(012F → NO DATA; el nivel va por el BSI, no por la ECU del motor). El detector
de salto de nivel no puede funcionar. Este script lo sustituye:

  - Al cerrar un viaje, si la última posición GPS está a <200 m de una
    gasolinera (amenity=fuel en OpenStreetMap) → repostaje probable.
  - Estación = nombre OSM (brand/name). Precio = API Ministerio (fuel_price)
    para la estación más cercana, ese día.
  - Litros: NULL por defecto (sin nivel no hay porcentaje). El usuario los
    confirma en la webapp (botón "Añadir litros") o se estiman por consumo
    MAF desde el último repostaje si el usuario marca "depósito lleno".

Patrón cron no_agent (cada 10 min):
  - stdout VACÍO  → silencio
  - stdout TEXTO  → posible repostaje detectado (Telegram)
"""
import sqlite3, os, json, sys, math, subprocess, time
from datetime import datetime, timedelta

OBD_DB = os.path.expanduser("~/.hermes/data/obd_telemetry.db")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fuel_price import get_price_at  # noqa: E402

RADIUS_M = 200       # distancia máx a una gasolinera
LOOKBACK_H = 3       # sesiones completadas en las últimas 3h
USER_AGENT = "VehicleTelemetry/1.0 (personal vehicle tracker)"
OVERPASS = "https://overpass-api.de/api/interpreter"


def haversine(lat1, lon1, lat2, lon2):
    R = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def find_fuel_station(lat, lon):
    """Busca gasolinera (amenity=fuel) a <RADIUS_M del punto. Devuelve
    (nombre, distancia_m) o (None, None)."""
    query = f"""[out:json][timeout:20];
(
  node["amenity"="fuel"](around:{RADIUS_M},{lat},{lon});
  way["amenity"="fuel"](around:{RADIUS_M},{lat},{lon});
);
out center tags;"""
    try:
        proc = subprocess.run(
            ["curl", "-s", "--max-time", "25", "-A", USER_AGENT,
             "-d", query, OVERPASS],
            capture_output=True, text=True, timeout=30,
        )
        data = json.loads(proc.stdout)
        best, best_d = None, None
        for el in data.get("elements", []):
            if el.get("type") == "node":
                elat, elon = el.get("lat"), el.get("lon")
            else:
                c = el.get("center", {})
                elat, elon = c.get("lat"), c.get("lon")
            if elat is None or elon is None:
                continue
            d = haversine(lat, lon, elat, elon)
            if d <= RADIUS_M and (best_d is None or d < best_d):
                t = el.get("tags", {})
                name = t.get("brand") or t.get("name") or t.get("operator") or "Gasolinera"
                best, best_d = name, d
        return best, best_d
    except Exception as e:
        sys.stderr.write(f"overpass fail: {e}\n")
        return None, None


def main():
    conn = sqlite3.connect(OBD_DB, timeout=10.0)
    conn.execute("PRAGMA busy_timeout=10000")
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.OperationalError:
        pass
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
            c.execute(f"ALTER TABLE refuels ADD COLUMN {col} TEXT" if col in ("station", "source") else f"ALTER TABLE refuels ADD COLUMN {col} REAL")
        except sqlite3.OperationalError:
            pass

    since = (datetime.now() - timedelta(hours=LOOKBACK_H)).isoformat()
    c.execute("""
        SELECT s.id, s.end_time, s.distance_km,
               p.lat, p.lon
        FROM sessions s
        LEFT JOIN positions p ON p.id = (
            SELECT p2.id FROM positions p2 WHERE p2.session_id = s.id
            ORDER BY p2.timestamp DESC LIMIT 1
        )
        WHERE s.status='completed' AND s.end_time >= ?
          AND s.distance_km >= 0.5
          AND NOT EXISTS (SELECT 1 FROM refuels r WHERE r.session_id = s.id)
        ORDER BY s.end_time DESC LIMIT 5
    """, (since,))
    trips = [dict(r) for r in c.fetchall()]

    reports = []
    for t in trips:
        if t["lat"] is None or t["lon"] is None:
            continue
        name, dist = find_fuel_station(t["lat"], t["lon"])
        if not name:
            continue
        price, st = get_price_at(t["lat"], t["lon"])
        ts = t["end_time"] or datetime.now().isoformat()
        try:
            c.execute(
                """INSERT OR IGNORE INTO refuels
                   (ts, prev_ts, fuel_before, fuel_after, jump_pct, liters,
                    full_tank, session_id, price_per_l, cost, station, source)
                   VALUES (?,?,NULL,NULL,NULL,NULL,0,?,?,NULL,?,'gps')""",
                (ts, ts, t["id"], price, name),
            )
        except sqlite3.Error as e:
            sys.stderr.write(f"gps refuel insert fail: {e}\n")
        if c.rowcount:
            reports.append((ts, name, dist, price, t["id"], t["distance_km"]))

    conn.commit()
    conn.close()

    if reports:
        lines = []
        for ts, name, dist, price, sid, km in reports:
            d = datetime.fromisoformat(ts)
            precio = f" · 💶 {price:.3f} €/L" if price else ""
            lines.append(
                f"⛽ Posible repostaje — {d.strftime('%d/%m %H:%M')}\n"
                f"📍 {name} (a {dist:.0f} m){precio}\n"
                f"📏 viaje {km:.1f} km · confirma litros en la webapp (/refuels)"
            )
        print("\n\n".join(lines))


if __name__ == "__main__":
    main()
