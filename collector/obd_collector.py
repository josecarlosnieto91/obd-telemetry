#!/usr/bin/env python3
"""
OBD2 Data Collector — connects to vehicle tablet's VgateBridge TCP tunnel
and logs vehicle data to SQLite. Designed to run as cronjob.
"""
import socket
import sqlite3
import json
import time
import os
import sys
from datetime import datetime

# ── Configuration ──────────────────────────────────────────────
POLAR_HOST = "100.64.0.1"   # Tailscale IP
POLAR_PORT = 22000
DB_PATH = os.path.expanduser("~/.hermes/data/obd_telemetry.db")
DATA_DIR = os.path.expanduser("~/.hermes/data")
PID_INTERVAL = 2  # seconds between PID reads (don't flood the ELM327)
TRIP_IDLE_SECS = 120  # seconds without speed before ending a trip

# ── Database setup ─────────────────────────────────────────────
os.makedirs(DATA_DIR, exist_ok=True)

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
        CREATE TABLE IF NOT EXISTS dtc (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER,
            timestamp TEXT NOT NULL,
            code TEXT NOT NULL,
            description TEXT,
            cleared INTEGER DEFAULT 0,
            FOREIGN KEY (session_id) REFERENCES sessions(id)
        );
        CREATE TABLE IF NOT EXISTS alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER,
            timestamp TEXT NOT NULL,
            category TEXT NOT NULL,
            severity TEXT DEFAULT 'info',
            message TEXT NOT NULL,
            acknowledged INTEGER DEFAULT 0,
            FOREIGN KEY (session_id) REFERENCES sessions(id)
        );
        CREATE INDEX IF NOT EXISTS idx_readings_session ON readings(session_id);
        CREATE INDEX IF NOT EXISTS idx_readings_ts ON readings(timestamp);
    """)
    conn.commit()
    return conn

# ── OBD2 Protocol ──────────────────────────────────────────────

class OBD2Client:
    def __init__(self, host, port, timeout=10):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.sock = None
        self.connected = False

    def connect(self):
        try:
            self.sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
            self.sock.settimeout(5)
            self.connected = True
            self._init_elm()
            return True
        except Exception as e:
            self.connected = False
            return False

    def _init_elm(self):
        """Initialize ELM327 with standard commands"""
        for cmd in ["ATZ", "ATE0", "ATL0", "ATS0", "ATH0", "ATSP0"]:
            self._cmd(cmd)
            time.sleep(0.3)

    def _cmd(self, cmd, timeout=3):
        """Send command and read response"""
        try:
            self.sock.settimeout(timeout)
            self.sock.sendall((cmd + "\r\n").encode())
            time.sleep(0.1)
            data = b""
            while True:
                try:
                    chunk = self.sock.recv(1024)
                    if not chunk:
                        break
                    data += chunk
                    if b">" in data:
                        break
                except socket.timeout:
                    break
            return data.decode(errors="replace").strip()
        except Exception:
            return ""

    def read_pid(self, pid):
        """Read a single PID, returns raw response"""
        resp = self._cmd(pid)
        # Parse response: typically "41 XX YY ZZ ..."
        lines = resp.strip().split("\r\n")
        for line in lines:
            line = line.replace(" ", "").replace("\r", "")
            if line.startswith("4" + pid[1:3]):
                data = line[4:]  # Remove "41XX" header
                return data
        return None

    def read_voltage(self):
        """Read battery voltage via ATRV"""
        resp = self._cmd("ATRV")
        for line in resp.split("\r\n"):
            line = line.strip()
            if "V" in line or "v" in line:
                try:
                    return float(line.replace("V", "").replace("v", "").strip())
                except:
                    pass
        return None

    def read_dtc(self):
        """Read Diagnostic Trouble Codes"""
        resp = self._cmd("03")
        codes = []
        for line in resp.split("\r\n"):
            line = line.strip().replace(" ", "")
            if line.startswith("43") and len(line) >= 6:
                # Parse DTC bytes: 43 XX YY ZZ WW
                data = line[2:]
                for i in range(0, len(data), 4):
                    if i + 4 <= len(data):
                        byte1 = int(data[i:i+2], 16)
                        byte2 = int(data[i+2:i+4], 16)
                        if byte1 == 0 and byte2 == 0:
                            continue
                        code = _dtc_code(byte1, byte2)
                        codes.append(code)
        return codes

    def close(self):
        if self.sock:
            try:
                self.sock.close()
            except:
                pass
        self.connected = False


def _dtc_code(byte1, byte2):
    """Convert DTC bytes to standard code (e.g., P0101)"""
    prefixes = {0: "P0", 1: "P1", 2: "P2", 3: "P3"}
    first = (byte1 >> 6) & 0x03
    prefix = prefixes.get(first, "U0")
    code_num = ((byte1 & 0x3F) << 8) | byte2
    return f"{prefix}{code_num:04d}"


DTC_DESCRIPTIONS = {
    "P0100": "Circuito MAF — revisar sensor de flujo de aire",
    "P0101": "MAF fuera de rango — limpiar o reemplazar sensor",
    "P0115": "Circuito temp. refrigerante — revisar sensor ECT",
    "P0120": "Circuito posición mariposa — revisar TPS",
    "P0130": "Circuito sensor O2 — revisar sonda lambda",
    "P0171": "Mezcla pobre — posible fuga de admisión",
    "P0172": "Mezcla rica — posible inyectores sucios",
    "P0300": "Fallos de encendido múltiples — revisar bujías/bobinas",
    "P0301": "Fallo cilindro 1", "P0302": "Fallo cilindro 2",
    "P0303": "Fallo cilindro 3", "P0304": "Fallo cilindro 4",
    "P0335": "Sensor posición cigüeñal — revisar CKP",
    "P0340": "Sensor posición árbol de levas — revisar CMP",
    "P0400": "Circuito EGR — posible válvula obstruida",
    "P0420": "Catalizador eficiencia baja — posible catalizador dañado",
    "P0440": "Sistema EVAP — revisar tapón combustible",
    "P0480": "Circuito ventilador refrigeración",
    "P0500": "Sensor velocidad vehículo — revisar VSS",
    "P0505": "Circuito IAC — revisar válvula ralentí",
    "P0562": "Tensión batería baja — revisar alternador/batería",
    "P0563": "Tensión batería alta — posible regulador alternador",
    "P0600": "Fallo comunicación ECM",
    "P0606": "Fallo interno ECM/PCM",
    "P0700": "Fallo transmisión — revisar TCM",
    "P1130": "Sensor O2 banco 1 — mezcla fuera de límites",
}

# ── Alert Engine ───────────────────────────────────────────────

def check_alerts(reading, session_id, conn):
    """Analyze reading and generate alerts if needed"""
    alerts = []
    ts = reading.get("timestamp", datetime.now().isoformat())
    c = conn.cursor()

    # Temperature alerts
    ct = reading.get("coolant_temp")
    if ct is not None:
        if ct > 105:
            alerts.append(("temp", "critical", f"Temperatura refrigerante crítica: {ct:.0f}°C — ¡detener vehículo!"))
        elif ct > 95:
            alerts.append(("temp", "warning", f"Temperatura refrigerante alta: {ct:.0f}°C — revisar nivel"))

    # Voltage alerts
    v = reading.get("voltage")
    if v is not None:
        if v < 11.5:
            alerts.append(("battery", "critical", f"Tensión batería muy baja: {v:.1f}V — posible fallo alternador"))
        elif v < 12.0:
            alerts.append(("battery", "warning", f"Tensión batería baja: {v:.1f}V"))
        if v > 15.0:
            alerts.append(("battery", "warning", f"Tensión alternador alta: {v:.1f}V — revisar regulador"))

    # RPM alerts
    rpm = reading.get("rpm")
    if rpm is not None:
        if rpm > 6000:
            alerts.append(("rpm", "warning", f"RPM excesivos: {rpm:.0f} rpm — reducir marcha"))
        elif rpm < 500 and reading.get("speed", 0) > 10:
            alerts.append(("rpm", "info", f"RPM bajos para la velocidad: {rpm:.0f} rpm — reducir marcha"))

    # Fuel level
    fl = reading.get("fuel_level")
    if fl is not None and fl < 15:
        alerts.append(("fuel", "warning", f"Combustible bajo: {fl:.0f}% — repostar pronto"))

    # DTC alerts
    dtc_count = reading.get("dtc_count", 0)
    if dtc_count > 0:
        alerts.append(("dtc", "critical", f"{dtc_count} código(s) de error activo(s)"))

    # Store alerts
    for category, severity, message in alerts:
        c.execute(
            "INSERT INTO alerts (session_id, timestamp, category, severity, message) VALUES (?, ?, ?, ?, ?)",
            (session_id, ts, category, severity, message)
        )

    if alerts:
        conn.commit()
    return alerts


# ── Main Collection Loop ───────────────────────────────────────

def collect():
    conn = init_db()
    c = conn.cursor()

    print(f"[{datetime.now().isoformat()}] OBD2 Collector iniciado")

    # Check/create active session
    c.execute("SELECT id FROM sessions WHERE status = 'active' LIMIT 1")
    row = c.fetchone()
    if row:
        session_id = row[0]
        print(f"  Continuando sesión activa #{session_id}")
    else:
        c.execute(
            "INSERT INTO sessions (start_time, status) VALUES (?, 'active')",
            (datetime.now().isoformat(),)
        )
        session_id = c.lastrowid
        conn.commit()
        print(f"  Nueva sesión #{session_id}")

    client = OBD2Client(POLAR_HOST, POLAR_PORT)
    if not client.connect():
        print(f"  ❌ No se pudo conectar a {POLAR_HOST}:{POLAR_PORT}")
        return

    print(f"  ✅ Conectado a VgateBridge")

    idle_counter = 0
    readings_count = 0

    try:
        while True:
            ts = datetime.now()
            reading = {"timestamp": ts.isoformat(), "session_id": session_id}

            # Read PIDs
            try:
                # RPM (010C -> 41 0C XX YY -> rpm = (XX*256+YY)/4)
                data = client.read_pid("010C")
                if data and len(data) >= 4:
                    rpm = (int(data[:2], 16) * 256 + int(data[2:4], 16)) / 4
                    reading["rpm"] = round(rpm, 1)

                # Speed (010D -> 41 0D XX -> km/h)
                data = client.read_pid("010D")
                if data and len(data) >= 2:
                    speed = float(int(data[:2], 16))
                    reading["speed"] = speed

                # Coolant temp (0105 -> 41 05 XX -> °C = XX - 40)
                data = client.read_pid("0105")
                if data and len(data) >= 2:
                    ct = int(data[:2], 16) - 40
                    reading["coolant_temp"] = ct

                # Engine load (0111 -> 41 11 XX -> % = XX*100/255)
                data = client.read_pid("0111")
                if data and len(data) >= 2:
                    load = int(data[:2], 16) * 100 / 255
                    reading["engine_load"] = round(load, 1)

                # Intake temp (010F -> 41 0F XX -> °C = XX - 40)
                data = client.read_pid("010F")
                if data and len(data) >= 2:
                    it = int(data[:2], 16) - 40
                    reading["intake_temp"] = it

                # Throttle position (0111 -> same as engine load? No, PID is different)
                # Actually throttle is PID 0111 too? No, PID 0111 is engine load.
                # Throttle is PID 0113 (relative) or 0114 (absolute)
                # Let me use PID 0114 (absolute throttle position)
                data = client.read_pid("0114")
                if data and len(data) >= 2:
                    tp = int(data[:2], 16) * 100 / 255
                    reading["throttle_pos"] = round(tp, 1)

                # Fuel level (012F -> 41 2F XX -> % = XX*100/255)
                data = client.read_pid("012F")
                if data and len(data) >= 2:
                    fl = int(data[:2], 16) * 100 / 255
                    reading["fuel_level"] = round(fl, 1)

                # MAF (0110 -> 41 10 XX YY -> g/s = (XX*256+YY)/100)
                data = client.read_pid("0110")
                if data and len(data) >= 4:
                    maf = (int(data[:2], 16) * 256 + int(data[2:4], 16)) / 100
                    reading["maf"] = round(maf, 2)

                # Voltage
                voltage = client.read_voltage()
                if voltage:
                    reading["voltage"] = round(voltage, 1)

                # DTC check (every 10th reading)
                if readings_count % 10 == 0:
                    dtc_codes = client.read_dtc()
                    reading["dtc_count"] = len(dtc_codes)
                    reading["dtc_codes"] = ",".join(dtc_codes) if dtc_codes else ""

                    # Store new DTCs
                    for code in dtc_codes:
                        c.execute(
                            "SELECT id FROM dtc WHERE code = ? AND session_id = ? AND cleared = 0",
                            (code, session_id)
                        )
                        if not c.fetchone():
                            desc = DTC_DESCRIPTIONS.get(code, "Código no reconocido - consultar taller")
                            c.execute(
                                "INSERT INTO dtc (session_id, timestamp, code, description) VALUES (?, ?, ?, ?)",
                                (session_id, ts.isoformat(), code, desc)
                            )
                else:
                    reading["dtc_count"] = 0
                    reading["dtc_codes"] = ""

            except Exception as e:
                print(f"  ⚠ Error leyendo PIDs: {e}")
                # Reconnect if needed
                if not client.connected:
                    print("  Reconectando...")
                    if not client.connect():
                        print("  ❌ No se pudo reconectar")
                        break

            # Store reading
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
                reading.get("maf"), reading.get("dtc_count", 0),
                reading.get("dtc_codes", "")
            ))
            conn.commit()
            readings_count += 1

            # Check alerts
            alerts = check_alerts(reading, session_id, conn)
            for cat, sev, msg in alerts:
                print(f"  🔔 [{sev}] {msg}")

            # Idle detection
            speed = reading.get("speed", 0)
            if speed == 0 or speed is None:
                idle_counter += 1
            else:
                idle_counter = 0

            # Print summary
            vals = []
            if reading.get("rpm"): vals.append(f"{reading['rpm']:.0f} rpm")
            if reading.get("speed"): vals.append(f"{reading['speed']:.0f} km/h")
            if reading.get("coolant_temp"): vals.append(f"{reading['coolant_temp']:.0f}°C")
            if reading.get("voltage"): vals.append(f"{reading['voltage']:.1f}V")
            print(f"  [{ts.strftime('%H:%M:%S')}] {' · '.join(vals)}")

            # End trip if idle too long
            if idle_counter * PID_INTERVAL >= TRIP_IDLE_SECS:
                print(f"  🅿 Vehículo aparcado — finalizando sesión #{session_id}")
                end_session(session_id, conn)
                print(f"  Esperando nuevo viaje...")
                # Wait and then start new session
                time.sleep(30)
                c.execute(
                    "INSERT INTO sessions (start_time, status) VALUES (?, 'active')",
                    (datetime.now().isoformat(),)
                )
                session_id = c.lastrowid
                conn.commit()
                print(f"  Nueva sesión #{session_id}")
                idle_counter = 0
                continue

            time.sleep(PID_INTERVAL)

    except KeyboardInterrupt:
        print(f"\n  ⏹ Interrupción — guardando...")
    finally:
        # Don't close session on script stop, keep it active for next run
        client.close()
        print(f"  📊 Total lecturas: {readings_count}")
        print(f"  Sesión #{session_id} activa (continuará)")


def end_session(session_id, conn):
    """Calculate session stats and mark as completed"""
    c = conn.cursor()
    c.execute("""
        SELECT COUNT(*), MAX(rpm), MAX(speed), AVG(speed),
               MIN(timestamp), MAX(timestamp)
        FROM readings WHERE session_id = ?
    """, (session_id,))
    row = c.fetchone()
    if row and row[0] > 0:
        count = row[0]
        max_rpm = row[1] or 0
        max_speed = row[2] or 0
        avg_speed = row[3] or 0
        start_ts = row[4]
        end_ts = row[5]

        # Estimate driving minutes
        if start_ts and end_ts:
            duration = (datetime.fromisoformat(end_ts) - datetime.fromisoformat(start_ts)).total_seconds()
            driving_min = int(duration / 60)
        else:
            driving_min = 0

        # Estimate distance (rough: speed * time, sampled every PID_INTERVAL secs)
        c.execute("SELECT speed, timestamp FROM readings WHERE session_id = ? ORDER BY timestamp", (session_id,))
        distance = 0
        prev_speed = 0
        for r in c.fetchall():
            speed = r[0] or 0
            distance += (speed + prev_speed) / 2 * (PID_INTERVAL / 3600) / 2  # km
            prev_speed = speed

        c.execute("""
            UPDATE sessions SET
                end_time = ?, max_speed = ?, max_rpm = ?,
                avg_speed = ?, driving_minutes = ?, distance_km = ?,
                status = 'completed'
            WHERE id = ?
        """, (end_ts or datetime.now().isoformat(),
              round(max_speed, 1), round(max_rpm, 1),
              round(avg_speed, 1), driving_min, round(distance, 2),
              session_id))
        conn.commit()

        # Generate maintenance tips based on trip data
        generate_tips(session_id, c, count, conn)


def generate_tips(session_id, c, reading_count, conn):
    """Generate driving/maintenance tips based on trip analysis"""
    # Get trip stats
    c.execute("""
        SELECT AVG(rpm), MAX(rpm), AVG(speed), MAX(speed),
               AVG(coolant_temp), MAX(coolant_temp),
               AVG(engine_load), AVG(throttle_pos),
               AVG(voltage), MIN(voltage)
        FROM readings WHERE session_id = ?
    """, (session_id,))
    row = c.fetchone()
    if not row: return

    avg_rpm, max_rpm, avg_speed, max_speed = row[0], row[1], row[2], row[3]
    avg_temp, max_temp = row[4], row[5]
    avg_load, avg_throttle = row[6], row[7]
    avg_voltage, min_voltage = row[8], row[9]

    tips = []

    # Driving efficiency
    if avg_rpm and avg_rpm > 3000:
        tips.append(("driving", "info",
            "RPM medios altos (%.0f rpm). Para ahorrar combustible, cambia a una marcha superior "
            "cuando el motor supere las 2500 rpm." % avg_rpm))
    if avg_rpm and avg_rpm < 1500 and avg_speed and avg_speed > 60:
        tips.append(("driving", "tip",
            "Conducción eficiente: RPM bajos a velocidad de crucero. Buen estilo."))
    if avg_load and avg_load > 60:
        tips.append(("driving", "info",
            "Carga media del motor alta (%.0f%%). Revisar presión neumáticos y "
            "exceso de peso en el vehículo." % avg_load))
    if max_speed and max_speed > 120:
        tips.append(("driving", "warning",
            "Velocidad máxima de %.0f km/h registrada. Circular a altas velocidades "
            "incrementa el consumo y el desgaste." % max_speed))

    # Maintenance
    if max_temp and max_temp > 100:
        tips.append(("maintenance", "warning",
            "Temperatura refrigerante alta (%.0f°C). Revisar nivel de "
            "refrigerante y funcionamiento del termostato/ventilador." % max_temp))
    if min_voltage and min_voltage < 12.0:
        tips.append(("maintenance", "warning",
            "Tensión mínima de batería baja (%.1fV). Revisar estado de la "
            "batería y alternador." % min_voltage))
    if avg_voltage and avg_voltage > 14.8:
        tips.append(("maintenance", "info",
            "Tensión media del alternador alta (%.1fV). Vigilar regulador." % avg_voltage))
    if max_rpm and max_rpm > 5000:
        tips.append(("maintenance", "info",
            "RPM máximo de %.0f rpm. Si es frecuente, revisar estado del "
            "aceite y niveles." % max_rpm))
    if reading_count > 10:
        duration_mins = reading_count * PID_INTERVAL / 60
        if duration_mins < 10:
            tips.append(("usage", "tip",
                "Trayecto corto (%.0f min). Los motores diésel necesitan "
                "trayectos más largos para alcanzar temperatura óptima." % duration_mins))

    # Store tips
    cat_map = {"driving": "conduccion", "maintenance": "mantenimiento", "usage": "uso"}
    for category, severity, message in tips:
        cat_name = cat_map.get(category, category)
        c.execute(
            "INSERT INTO alerts (session_id, timestamp, category, severity, message) "
            "VALUES (?, ?, ?, ?, ?)",
            (session_id, datetime.now().isoformat(), cat_name, severity, message)
        )
    conn.commit()


if __name__ == "__main__":
    collect()
