#!/usr/bin/env python3
"""
OBD2 Collector (cron mode) — connects to vehicle tablet's VgateBridge,
collects one batch of readings + GPS position, stores in SQLite.
"""
import socket
import sqlite3
import json
import time
import os
import sys
import re
import subprocess as sp
from datetime import datetime

# ── Configuration ──
POLAR_HOST = "100.64.0.1"
POLAR_PORT = 22000
DB_PATH = os.path.expanduser("~/.hermes/data/obd_telemetry.db")
DATA_DIR = os.path.expanduser("~/.hermes/data")
PID_INTERVAL = 2
SAMPLES = 3
# Hard cap for the whole run: stays well under the 120s cron budget even if
# every ELM command burns its full timeout.
DEADLINE_SECONDS = 85

os.makedirs(DATA_DIR, exist_ok=True)


def _cmd(sock, cmd, timeout=4):
    try:
        sock.settimeout(timeout)
        sock.sendall((cmd + "\r\n").encode())
        time.sleep(0.2)
        data = b""
        while True:
            try:
                c = sock.recv(1024)
                if not c: break
                data += c
                if b">" in c: break
            except socket.timeout:
                break
        return data.decode(errors="replace").strip()
    except:
        return ""


def read_pid(sock, pid):
    resp = _cmd(sock, pid)
    for line in resp.split("\r\n"):
        line = line.replace(" ", "").replace("\r", "")
        if line.startswith("4" + pid[1:3]):
            return line[4:]
    return None


def get_gps():
    """Get GPS position from vehicle tablet via SSH termux-location"""
    SSH_HOST = "vehicle-tablet"  # Use alias from ~/.ssh/config
    try:
        # Try passive first (instant, returns last known)
        result = sp.run(
            ["ssh", SSH_HOST, "termux-location", "-p", "passive"],
            capture_output=True, text=True, timeout=5
        )
        data = json.loads(result.stdout)
        if data.get("latitude") is not None:
            return _parse_gps(data)

        # Fallback: network location (fast, ~2s)
        result = sp.run(
            ["ssh", SSH_HOST, "termux-location", "-p", "network"],
            capture_output=True, text=True, timeout=8
        )
        data = json.loads(result.stdout)
        if data.get("latitude") is not None:
            return _parse_gps(data)

        # Last resort: GPS (slow, ~10s but accurate)
        result = sp.run(
            ["ssh", SSH_HOST, "termux-location", "-p", "gps"],
            capture_output=True, text=True, timeout=15
        )
        data = json.loads(result.stdout)
        return _parse_gps(data)

    except json.JSONDecodeError:
        return {"error": "invalid JSON from termux-location"}
    except sp.TimeoutExpired:
        return {"error": "GPS timeout"}
    except Exception as e:
        return {"error": str(e)}


def _parse_gps(data):
    return {
        "lat": data.get("latitude"),
        "lon": data.get("longitude"),
        "gps_speed": data.get("speed"),
        "bearing": data.get("bearing"),
        "accuracy": data.get("accuracy"),
        "altitude": data.get("altitude"),
        "provider": data.get("provider", "unknown"),
    }


def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.executescript("""
        CREATE TABLE IF NOT EXISTS sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            start_time TEXT NOT NULL,
            end_time TEXT,
            distance_km REAL DEFAULT 0,
            max_speed REAL DEFAULT 0,
            avg_speed REAL DEFAULT 0,
            max_rpm REAL DEFAULT 0,
            driving_minutes INTEGER DEFAULT 0,
            status TEXT DEFAULT 'active'
        );
        CREATE TABLE IF NOT EXISTS readings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER NOT NULL,
            timestamp TEXT NOT NULL,
            rpm REAL,
            speed REAL,
            coolant_temp REAL,
            engine_load REAL,
            intake_temp REAL,
            throttle_pos REAL,
            fuel_level REAL,
            voltage REAL,
            maf REAL,
            dtc_count INTEGER,
            dtc_codes TEXT,
            FOREIGN KEY (session_id) REFERENCES sessions(id)
        );
        CREATE TABLE IF NOT EXISTS positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER NOT NULL,
            timestamp TEXT NOT NULL,
            lat REAL,
            lon REAL,
            gps_speed REAL,
            bearing REAL,
            accuracy REAL,
            altitude REAL,
            provider TEXT,
            FOREIGN KEY (session_id) REFERENCES sessions(id)
        );
        CREATE TABLE IF NOT EXISTS dtc (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER,
            timestamp TEXT NOT NULL,
            code TEXT NOT NULL,
            description TEXT,
            cleared INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER,
            timestamp TEXT NOT NULL,
            category TEXT NOT NULL,
            severity TEXT DEFAULT 'info',
            message TEXT NOT NULL,
            acknowledged INTEGER DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_readings_session ON readings(session_id);
        CREATE INDEX IF NOT EXISTS idx_readings_ts ON readings(timestamp);
        CREATE INDEX IF NOT EXISTS idx_positions_session ON positions(session_id);
        CREATE INDEX IF NOT EXISTS idx_positions_ts ON positions(timestamp);
    """)
    conn.commit()
    return conn


def main():
    # 0. Importar datos locales de vehicle tablet (recopilación autónoma sin
    #    Internet): si hay fichero entrante, merge en la BD antes del TCP live.
    try:
        sp.run([sys.executable,
                os.path.expanduser("~/.hermes/scripts/obd_local_import.py")],
               timeout=30)
    except Exception:
        pass  # el import falla silenciosamente si no hay datos o está ocupado

    conn = init_db()
    c = conn.cursor()

    # Check vehicle tablet reachable via TCP
    try:
        sock = socket.create_connection((POLAR_HOST, POLAR_PORT), timeout=6)
    except Exception as e:
        print(f"OFFLINE: {e}", file=sys.stderr)
        return

    print(f"ONLINE — connected to {POLAR_HOST}:{POLAR_PORT}", flush=True)

    # Get GPS position FIRST (can be slow), before bridge init window closes
    pos = get_gps()

    # Wait for bridge ELM327 init to finish, flush stale data
    time.sleep(5)
    sock.settimeout(1.0)
    try:
        while sock.recv(4096): pass
    except:
        pass

    # Probe: a live ELM bridge answers ATI. If it stays silent after the init
    # window, there is nothing to collect — exit fast instead of burning the
    # full PID timeout budget.
    probe = _cmd(sock, "ATI", timeout=3)
    if not probe:
        print("NO_DATA — bridge sin respuesta (TCP abierto, ELM mudo)", flush=True)
        sock.close()
        return

    t_start = time.monotonic()

    # Session
    c.execute("SELECT id FROM sessions WHERE status = 'active' LIMIT 1")
    row = c.fetchone()
    if row:
        session_id = row[0]
    else:
        c.execute(
            "INSERT INTO sessions (start_time, status) VALUES (?, 'active')",
            (datetime.now().isoformat(),)
        )
        session_id = c.lastrowid
        conn.commit()

    readings_saved = 0

    for sample in range(SAMPLES):
        if time.monotonic() - t_start >= DEADLINE_SECONDS:
            print("DEADLINE — tiempo global agotado, guardando lo leído", flush=True)
            break
        ts = datetime.now()
        reading = {"timestamp": ts.isoformat(), "session_id": session_id}

        # Read PIDs
        data = read_pid(sock, "010C")
        if data and len(data) >= 4:
            rpm = (int(data[:2], 16) * 256 + int(data[2:4], 16)) / 4
            reading["rpm"] = round(rpm, 1)

        data = read_pid(sock, "010D")
        if data and len(data) >= 2:
            reading["speed"] = float(int(data[:2], 16))
            # OBD2 speed overrides GPS speed when available
            if pos and "gps_speed" in pos and pos["gps_speed"] is not None:
                pos["gps_speed"] = float(pos["gps_speed"])

        data = read_pid(sock, "0105")
        if data and len(data) >= 2:
            reading["coolant_temp"] = int(data[:2], 16) - 40

        data = read_pid(sock, "0111")
        if data and len(data) >= 2:
            reading["engine_load"] = round(int(data[:2], 16) * 100 / 255, 1)

        data = read_pid(sock, "010F")
        if data and len(data) >= 2:
            reading["intake_temp"] = int(data[:2], 16) - 40

        data = read_pid(sock, "0114")
        if data and len(data) >= 2:
            reading["throttle_pos"] = round(int(data[:2], 16) * 100 / 255, 1)

        data = read_pid(sock, "012F")
        if data and len(data) >= 2:
            reading["fuel_level"] = round(int(data[:2], 16) * 100 / 255, 1)

        data = read_pid(sock, "0110")
        if data and len(data) >= 4:
            reading["maf"] = round((int(data[:2], 16) * 256 + int(data[2:4], 16)) / 100, 2)

        # Voltage
        try:
            sock.settimeout(3)
            sock.sendall(b"ATRV\r\n")
            time.sleep(0.5)
            vresp = b""
            while True:
                try:
                    chunk = sock.recv(4096)
                    if not chunk: break
                    vresp += chunk
                    if b">" in chunk: break
                except: break
            # Parseo robusto del voltaje: el ELM327 responde con CR (no CRLF),
            # p.ej. "12.5V\r\r>". El split("\r\n") anterior dejaba la respuesta
            # junta y float() fallaba -> voltaje None silencioso.
            m = re.search(r"([\d.]+)\s*[Vv]", vresp.decode(errors="replace"))
            if m:
                reading["voltage"] = round(float(m.group(1)), 1)
        except: pass

        # Store OBD2 reading
        c.execute("""
            INSERT INTO readings (session_id, timestamp, rpm, speed, coolant_temp,
                engine_load, intake_temp, throttle_pos, fuel_level, voltage, maf,
                dtc_count, dtc_codes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            session_id, reading["timestamp"],
            reading.get("rpm"), reading.get("speed"),
            reading.get("coolant_temp"), reading.get("engine_load"),
            reading.get("intake_temp"), reading.get("throttle_pos"),
            reading.get("fuel_level"), reading.get("voltage"),
            reading.get("maf"), 0, ""
        ))
        conn.commit()

        # Store GPS position once (first sample)
        if sample == 0 and pos and "lat" in pos and pos["lat"] is not None:
            c.execute("""
                INSERT INTO positions (session_id, timestamp, lat, lon,
                    gps_speed, bearing, accuracy, altitude, provider)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                session_id, reading["timestamp"],
                pos.get("lat"), pos.get("lon"),
                pos.get("gps_speed"), pos.get("bearing"),
                pos.get("accuracy"), pos.get("altitude"),
                pos.get("provider")
            ))
            conn.commit()

        readings_saved += 1

        # Print summary
        vals = []
        if reading.get("rpm"): vals.append(f"{reading['rpm']:.0f} rpm")
        if reading.get("speed"): vals.append(f"{reading['speed']:.0f} km/h")
        if reading.get("coolant_temp"): vals.append(f"{reading['coolant_temp']:.0f}°C")
        if reading.get("voltage"): vals.append(f"{reading['voltage']:.1f}V")
        if pos and "lat" in pos and pos["lat"] is not None:
            vals.append(f"📍 {pos['lat']:.4f},{pos['lon']:.4f}")
        print(f"  [{ts.strftime('%H:%M:%S')}] {' · '.join(vals)}", flush=True)

        if sample < SAMPLES - 1:
            time.sleep(PID_INTERVAL)

    sock.close()
    has_pos = pos and "lat" in pos and pos["lat"] is not None
    print(f"DONE — {readings_saved} readings + {'📍 GPS' if has_pos else '⚠️ sin GPS'} (session #{session_id})", flush=True)


if __name__ == "__main__":
    main()
