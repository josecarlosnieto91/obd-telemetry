#!/usr/bin/env python3
"""obd_local_collector.py — recolector OBD2 LOCAL en Polar Star (autónomo, sin Internet).

Lee el bridge TCP 127.0.0.1:22000 (VgateBridge) + GPS local (termux-location),
guarda en SQLite local ~/obd_data/obd_local.db. No depende de red: si no hay
Internet, la captura sigue. Sync oportunista a Cassiopeia vía SCP cada ~10 min
cuando hay red (MagicDNS: cassiopeia, no IP).

v2 (2026-08-03): añade lectura de DTCs (Mode 03 almacenados + 07 pendientes)
cada ciclo de sync, con descripciones del fichero de configuración del
vehículo (~/obd_vehicle_config.json). Todo local.

Mantenido vivo por polar_boot_extra.sh (crond cada minuto, idempotente).
Copia maestra: ~/.hermes/scripts/obd_local_collector.py en Cassiopeia.
"""
import fcntl
import json
import os
import re
import socket
import sqlite3
import subprocess
import sys
import time

# ── Config ──
HOME = os.environ.get("HOME", "/data/data/com.termux/files/home")
DATA_DIR = os.path.join(HOME, "obd_data")
DB_PATH = os.path.join(DATA_DIR, "obd_local.db")
LOG_PATH = os.path.join(DATA_DIR, "obd_local.log")
CONFIG_PATH = os.path.join(HOME, "obd_vehicle_config.json")
BRIDGE_HOST = "127.0.0.1"
BRIDGE_PORT = 22000
CASSIOPEIA = "josecnr91@cassiopeia"          # MagicDNS — no IP hardcodeada
INCOMING_PATH = "/home/josecnr91/.hermes/data/incoming/polar_obd_local.db"
# v4.8: CSV del CanSnifferService (consumo CAN del decodificador Witson).
# Ruta getExternalFilesDir del APK (Android 10: /sdcard/Download exige permiso
# runtime; esta ruta no). Termux puede leerla con permiso de storage.
CAN_CSV_PATH = os.environ.get("CAN_CSV_PATH",
    "/sdcard/Android/data/com.cassiopeia.vgatebridge/files/Download/can_readings.csv")
SSH_KEY = os.path.join(HOME, ".ssh", "id_ed25519")

INTERVAL = 30            # segundos entre ciclos
SYNC_EVERY = 20           # sync cada N ciclos (~10 min con INTERVAL=30)
GPS_TIMEOUT = 8
BRIDGE_TIMEOUT = 6

PIDS = [
    ("rpm", "010C"),
    ("speed", "010D"),
    ("coolant", "0105"),
    ("engine_load", "0104"),   # FIX 2026-08-24: faltaba de la lista — el C4
                               # lo expone (los recolectores antiguos lo leían)
    ("throttle", "0111"),
    ("intake", "010F"),
    ("fuel", "012F"),
    ("maf", "0110"),
    ("fuel_rate", "015E"),   # Engine Fuel Rate (L/h) — consumo real de la ECU
]

# PIDs diésel adicionales (solo se leen si el escaneo dice que el motor los soporta)
PIDS_DIESEL = [
    ("map", "010B"),            # Intake manifold pressure (boost turbo) — kPa
    ("ambient", "0146"),        # Ambient air temperature — °C
    ("fuel_pressure", "0123"),  # Fuel rail pressure — kPa (si lo expone)
]

# Comandos de escaneo de soporte (Mode 01: 0100 → PIDs 01-20, 0120 → 21-40, ...)
SCAN_CMDS = ["0100", "0120", "0140", "0160", "0180", "01A0", "01C0"]

os.makedirs(DATA_DIR, exist_ok=True)

# Config del vehículo: umbrales + descripciones DTC (si el fichero existe)
VEHICLE = {}
try:
    with open(CONFIG_PATH) as fh:
        VEHICLE = json.load(fh)
except Exception:
    pass

DTC_DESCRIPTIONS = VEHICLE.get("dtc_descriptions", {})

# Portabilidad 2026-08-05: heurística FAP solo en diésel con DPF.
# Un coche sin filtro de partículas (gasolina, diésel sin DPF) daría falsos
# positivos — el flag vehicle.has_dpf lo desactiva.
HAS_DPF = bool((VEHICLE.get("vehicle", {}) or {}).get("has_dpf", True))


def log(msg):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_PATH, "a") as fh:
            fh.write(line + "\n")
    except Exception:
        pass


def db_connect():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""CREATE TABLE IF NOT EXISTS readings (
        timestamp TEXT PRIMARY KEY,
        rpm REAL, speed REAL, coolant REAL, throttle REAL,
        intake REAL, fuel REAL, maf REAL, voltage REAL)""")
    # Columnas diésel (PIDs extendidos, opcionales según soporte del motor)
    for col in ("map", "ambient", "fuel_pressure", "fuel_rate", "engine_load"):
        try:
            conn.execute(f"ALTER TABLE readings ADD COLUMN {col} REAL")
        except sqlite3.OperationalError:
            pass  # columna ya existe
    conn.execute("""CREATE TABLE IF NOT EXISTS positions (
        timestamp TEXT PRIMARY KEY,
        lat REAL, lon REAL, alt REAL, speed REAL,
        bearing REAL, accuracy REAL, provider TEXT)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS dtc (
        timestamp TEXT,
        code TEXT,
        description TEXT,
        kind TEXT,
        PRIMARY KEY (code, kind))""")
    # v4.8: lecturas CAN del decodificador (CanSnifferService → CSV → aquí).
    # ts es PK → INSERT OR IGNORE evita duplicados al re-importar.
    conn.execute("""CREATE TABLE IF NOT EXISTS can_readings (
        ts TEXT PRIMARY KEY,
        consumption_l100 REAL,
        range_km REAL,
        odometer_km REAL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS fap_events (
        start_ts TEXT PRIMARY KEY,
        end_ts TEXT,
        duration_min REAL,
        rpm_avg REAL,
        maf_avg REAL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS calibration (
        key TEXT PRIMARY KEY,
        value REAL,
        n INTEGER,
        updated TEXT)""")
    conn.commit()
    return conn


def recv_until_prompt(sock, timeout=6):
    """Lee hasta recibir el prompt '>' del ELM327 o timeout."""
    buf = b""
    sock.settimeout(timeout)
    try:
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            buf += chunk
            if b">" in chunk:
                break
    except socket.timeout:
        pass
    except Exception:
        pass
    return buf


def read_pid(sock, cmd, timeout=6):
    try:
        sock.sendall(cmd.encode() + b"\r\n")
        return recv_until_prompt(sock, timeout)
    except Exception:
        return b""


def parse_hex_response(resp):
    """'41 0C 1A F4' → 6900 (rpm) o None. Maneja respuestas multilínea."""
    if not resp:
        return None
    m = re.search(rb"41 ([0-9A-Fa-f]{2})[ \r\n]+([0-9A-Fa-f ]+)", resp)
    if not m:
        return None
    pid = m.group(1).decode().upper()
    payload = [int(x, 16) for x in m.group(2).split()]
    if pid == "0C" and len(payload) >= 2:
        return (payload[0] * 256 + payload[1]) / 4.0
    if pid == "04" and len(payload) >= 1:   # Engine load (%) — FIX 2026-08-24
        return round(payload[0] * 100 / 255.0, 1)   # 1 decimal (FIX 2026-08-31)
    if pid == "0D" and len(payload) >= 1:
        return float(payload[0])
    if pid == "05" and len(payload) >= 1:
        return payload[0] - 40
    if pid == "11" and len(payload) >= 1:
        return round(payload[0] * 100 / 255.0, 1)   # 1 decimal (FIX 2026-08-31)
    if pid == "0F" and len(payload) >= 1:
        return payload[0] - 40
    if pid == "2F" and len(payload) >= 1:
        return round(payload[0] * 100 / 255.0, 1)   # 1 decimal (FIX 2026-08-31)
    if pid == "10" and len(payload) >= 2:
        return (payload[0] * 256 + payload[1]) / 100.0
    if pid == "0B" and len(payload) >= 2:   # MAP: presión absoluta admisión (boost)
        return round(payload[0] * 256 + payload[1], 1)  # kPa
    if pid == "46" and len(payload) >= 1:   # Ambient air temp
        return payload[0] - 40
    if pid == "23" and len(payload) >= 2:   # Fuel rail pressure
        return round((payload[0] * 256 + payload[1]) * 10.0, 1)  # kPa
    if pid == "5E" and len(payload) >= 2:   # Engine Fuel Rate (L/h)
        return round((payload[0] * 256 + payload[1]) * 0.01, 3)
    return None


def parse_voltage(resp):
    m = re.search(rb"([\d.]+)\s*[Vv]", resp)
    return round(float(m.group(1)), 1) if m else None


def parse_dtcs(resp):
    """Parsea la respuesta del ELM327 a Mode 03/07.

    Codificación SAE J2012: cada DTC son 2 bytes.
      byte1: bits 7-6 = letra (00=P, 01=C, 10=B, 11=U)
             bits 5-4 = 1er dígito del código (0-3)
             bits 3-0 = 2º dígito (hex)
      byte2: bits 7-4 = 3er dígito, bits 3-0 = 4º dígito
    Ej: 01 00 → P0100, 04 01 → P0401, 11 AA → P11AA (PSA).
    Devuelve lista de códigos tipo 'P0100'. Vacío si no hay DTCs.
    """
    codes = []
    if not resp:
        return codes
    # Buscar la línea de respuesta (empieza por 43 o 47)
    m = re.search(rb"\b4[37][ \r\n]+([0-9A-Fa-f ]+)", resp)
    if not m:
        return codes
    payload = m.group(1).decode(errors="replace").split()
    # '00 00' o '00' = sin DTCs
    if len(payload) < 2 or payload[0] in ("00", "0000"):
        return codes
    # Pares de bytes: cada DTC son 2 bytes
    for i in range(0, len(payload) - 1, 2):
        try:
            b1 = int(payload[i], 16)
            b2 = int(payload[i + 1], 16)
        except ValueError:
            continue
        letter = "PCBU"[(b1 >> 6) & 0x03]
        d1 = (b1 >> 4) & 0x03
        d2 = b1 & 0x0F
        d3 = (b2 >> 4) & 0x0F
        d4 = b2 & 0x0F
        if d1 == 0 and d2 == 0 and d3 == 0 and d4 == 0:
            continue
        codes.append(f"{letter}{d1:X}{d2:X}{d3:X}{d4:X}")
    return codes


def read_dtcs(sock):
    """Lee DTCs almacenados (Mode 03) y pendientes (Mode 07).
    Devuelve (stored, pending) como listas de códigos."""
    stored = []
    pending = []
    try:
        resp = read_pid(sock, "03")
        stored = parse_dtcs(resp)
        resp = read_pid(sock, "07")
        pending = parse_dtcs(resp)
    except Exception:
        pass
    return stored, pending


def scan_supported_pids(sock):
    """Escanea qué PIDs Mode 01 soporta el motor (bitmaps de 0100/0120/...).
    Devuelve set de códigos tipo '01', '0B', '23', ... (sin el prefijo 01).

    ⚠️ El ELM327 durante el arranque devuelve DOS respuestas a 0100: una
    preliminar incompleta (ej. '41 00 80 00 00 11') y la definitiva
    ('41 00 98 3B 40 11'). re.search agarraba la primera → el scan creía que
    el motor solo soportaba 3-4 PIDs. Fix: parsear TODAS las respuestas del
    buffer y quedarse con la que produzca más PIDs (la más completa)."""
    supported = set()
    for base in SCAN_CMDS:
        resp = read_pid(sock, base)
        best = set()
        for m in re.finditer(rb"41 ([0-9A-Fa-f]{2})[ \r\n]+([0-9A-Fa-f ]+)", resp):
            payload = [int(x, 16) for x in m.group(2).split()]
            if len(payload) < 4:
                continue
            base_val = int(m.group(1), 16)  # 0x00, 0x20, 0x40...
            cand = set()
            for bit in range(32):
                byte_idx = bit // 8
                bit_idx = 7 - (bit % 8)  # MSB primero según SAE J1979
                if payload[byte_idx] & (1 << bit_idx):
                    pid = base_val + bit + 1
                    if 1 <= pid <= 0xFF:
                        cand.add(f"{pid:02X}")
            if len(cand) > len(best):
                best = cand
        supported |= best
    return supported


def save_supported_pids(supported):
    """Persiste el set de PIDs soportados en un fichero auxiliar."""
    try:
        with open(os.path.join(DATA_DIR, "supported_pids.json"), "w") as fh:
            json.dump(sorted(supported), fh)
    except Exception:
        pass


def load_supported_pids():
    try:
        with open(os.path.join(DATA_DIR, "supported_pids.json")) as fh:
            return set(json.load(fh))
    except Exception:
        return set()


def get_gps():
    """Última posición conocida (instantáneo, sin fijar satélites)."""
    try:
        result = subprocess.run(
            ["termux-location", "-p", "passive", "-r", "last"],
            capture_output=True, text=True, timeout=GPS_TIMEOUT)
        if result.returncode == 0 and result.stdout.strip():
            return json.loads(result.stdout)
    except Exception:
        pass
    return None


# ── Estado de conexión del bridge (OPT 2026-08-12) ──
# Antes, cada read_bridge() abría socket nuevo + init 6s + warm-up 12s → el
# ciclo real tardaba ~40s de trabajo (gap ~71s con INTERVAL=30). Ahora el
# socket se REUTILIZA entre ciclos; init+warm-up solo en la primera conexión
# o tras un fallo. El protocolo ELM327 exige que tras el init el primer
# comando dispare SEARCHING (warm-up) — eso sigue pasando en cada reconexión.
_BRIDGE_SOCK = None
_BRIDGE_INIT_DONE = False


def _bridge_connect():
    """(Re)conecta al bridge con init+warm-up completos. Devuelve socket o None."""
    global _BRIDGE_SOCK, _BRIDGE_INIT_DONE
    try:
        if _BRIDGE_SOCK is not None:
            try:
                _BRIDGE_SOCK.close()
            except Exception:
                pass
        sock = socket.create_connection((BRIDGE_HOST, BRIDGE_PORT), timeout=BRIDGE_TIMEOUT)
        # Ventana init del bridge: el ELM327 ejecuta ATE0/ATZ al conectar el
        # primer cliente y tarda ~5s. Si enviamos comandos antes, los rechaza
        # (STOPPED) y desincroniza el buffer. Esperar la ventana completa.
        time.sleep(6)
        # Flush residual del init (ATE0/OK/ATZ...)
        try:
            sock.settimeout(2)
            while sock.recv(4096):
                pass
        except Exception:
            pass
        # Warm-up: el primer comando de datos tras el init dispara SEARCHING
        # (re-negocia protocolo). Se descarta su respuesta.
        read_pid(sock, "0100", timeout=12)
        # Flush de seguridad: si el warm-up dejó resto, no desincroniza los PIDs
        try:
            sock.settimeout(1)
            while sock.recv(4096):
                pass
        except Exception:
            pass
        _BRIDGE_SOCK = sock
        _BRIDGE_INIT_DONE = True
        return sock
    except Exception:
        _BRIDGE_SOCK = None
        _BRIDGE_INIT_DONE = False
        return None


def read_bridge(with_dtcs=False, supported=None):
    """Lee PIDs del bridge. Reutiliza el socket si está vivo; reconecta
    (init+warm-up) solo en la primera llamada o tras un fallo.

    Devuelve dict con 'reading' y opcionalmente 'dtcs', o None si falla."""
    global _BRIDGE_SOCK, _BRIDGE_INIT_DONE
    reading = {}

    # 1. Obtener socket: reutilizado, o reconectar si no hay/nunca init
    sock = _BRIDGE_SOCK if _BRIDGE_INIT_DONE else None
    if sock is None:
        sock = _bridge_connect()
        if sock is None:
            return None

    try:
        # 2. Verificar que el socket sigue vivo con un comando barato.
        #    ATRV (voltaje) responde rápido y no depende de PIDs.
        probe = read_pid(sock, "ATRV", timeout=5)
        voltage = parse_voltage(probe)
        if voltage is not None:
            reading["voltage"] = voltage
        elif probe == b"":
            # Socket muerto (bridge cerrado / BT caído): reconectar y reintentar
            _BRIDGE_SOCK = None
            _BRIDGE_INIT_DONE = False
            sock = _bridge_connect()
            if sock is None:
                return None
            probe = read_pid(sock, "ATRV", timeout=5)
            voltage = parse_voltage(probe)
            if voltage is not None:
                reading["voltage"] = voltage

        # 3. PIDs base (sin warm-up: el socket ya está sincronizado)
        for name, cmd in PIDS:
            resp = read_pid(sock, cmd, timeout=5)
            value = parse_hex_response(resp)
            if value is not None:
                reading[name] = value

        # 4. PIDs diésel: solo si el escaneo confirmó que el motor los expone
        if supported:
            for name, cmd in PIDS_DIESEL:
                pid_code = cmd[2:]
                if pid_code in supported:
                    resp = read_pid(sock, cmd, timeout=5)
                    value = parse_hex_response(resp)
                    if value is not None:
                        reading[name] = value

        # 5. DTCs: solo en el ciclo de sync (Mode 03 + 07) — cuestan ~2-4s
        dtcs = None
        if with_dtcs:
            stored, pending = read_dtcs(sock)
            dtcs = {"stored": stored, "pending": pending}

        result = {"reading": reading}
        if dtcs is not None:
            result["dtcs"] = dtcs
        return result if reading else None
    except Exception:
        # Cualquier error: marcar el socket como inválido para reconectar luego
        try:
            if _BRIDGE_SOCK is not None:
                _BRIDGE_SOCK.close()
        except Exception:
            pass
        _BRIDGE_SOCK = None
        _BRIDGE_INIT_DONE = False
        return None


def revive_bridge():
    """Si el bridge no responde, lo revive sin abrir UI (guard isAlive en el
    servicio evita duplicados). Una vez por ciclo como mucho."""
    try:
        subprocess.run(
            ["am", "startservice", "-n",
             "com.cassiopeia.vgatebridge/.BridgeService",
             "-a", "com.cassiopeia.vgatebridge.START"],
            capture_output=True, timeout=10)
    except Exception:
        pass


def import_can_csv(conn):
    """Importa el CSV del CanSnifferService (consumo CAN real del decodificador)
    a la tabla can_readings local. Devuelve nº de filas nuevas.

    CSV en /sdcard/Download/can_readings.csv: ts,consumption_l100,range_km,odometer_km
    ts es PK → INSERT OR IGNORE. Tras importar, se vacía el CSV (el dato ya
    está en la BD local, que es lo que se sincroniza a Cassiopeia).
    """
    csv_path = CAN_CSV_PATH
    if not os.path.exists(csv_path):
        return 0
    imported = 0
    try:
        with open(csv_path, "r") as f:
            lines = f.readlines()
        for line in lines:
            line = line.strip()
            if not line or line.startswith("ts,"):
                continue
            parts = line.split(",")
            # CSV normal: ts,cons,range,odom (4 campos)
            # CSV corrupto por locale: ts,cons,range,odom,extra si la coma
            # decimal española partió un valor (p.ej. "4,4" → "4","4")
            # FIX 2026-08-23: el CanSniffer v4.8.2 usaba %.1f con locale
            # español → "4,4" en vez de "4.4". El APK 4.8.3 ya fuerza
            # Locale.US; aquí toleramos ambas formas.
            if len(parts) < 4:
                continue
            try:
                ts = parts[0]
                # Si el primer valor numérico está partido ("4","4"), reconstruir
                if len(parts) == 5:
                    # Formato corrupto: ts,cons_ent,cons_dec,range,odom
                    cons = float(f"{parts[1]}.{parts[2]}")
                    rng, odom = float(parts[3]), float(parts[4])
                else:
                    cons, rng, odom = float(parts[1]), float(parts[2]), float(parts[3])
            except ValueError:
                continue
            cur = conn.execute(
                "INSERT OR IGNORE INTO can_readings (ts, consumption_l100, range_km, odometer_km) "
                "VALUES (?,?,?,?)", (ts, cons, rng, odom))
            imported += cur.rowcount
        if imported:
            conn.commit()
            # Vaciar el CSV: ya está en la BD local (y por tanto en el próximo sync)
            open(csv_path, "w").close()
            log(f"CAN: {imported} lecturas importadas del CSV")
    except Exception as e:
        log(f"CAN: error importando CSV: {e}")
    return imported


def sync_to_cassiopeia():
    """Sube una copia consistente si hay red. No bloquea la recolección.

    ⚠️ RACE FIX 2026-08-05: antes hacía `scp DB_PATH` mientras el proceso
    seguía escribiendo en el fichero → copia inconsistente/corrupta en
    Cassiopeia (el importador podía recibir un SQLite a medias). Ahora se
    genera una copia consistente con la API de backup de SQLite (snapshot
    atómico del estado actual) y se sube esa copia. La recolección en el
    fichero principal no se interrumpe.
    """
    tmp_db = DB_PATH + ".sync"
    try:
        src = sqlite3.connect(DB_PATH)
        try:
            dst = sqlite3.connect(tmp_db)
            try:
                src.backup(dst)  # snapshot consistente aunque src siga escribiendo
            finally:
                dst.close()
        finally:
            src.close()
        result = subprocess.run(
            ["scp", "-o", "ConnectTimeout=6", "-o", "BatchMode=yes",
             "-o", "StrictHostKeyChecking=no", "-i", SSH_KEY,
             tmp_db, f"{CASSIOPEIA}:{INCOMING_PATH}"],
            capture_output=True, text=True, timeout=20)
        return result.returncode == 0
    except Exception:
        return False
    finally:
        try:
            os.remove(tmp_db)
        except OSError:
            pass


# ── Calibración automática ─────────────────────────────────
# Aprende valores normales del vehículo con datos reales y los guarda en
# la tabla calibration. Cada clave es una media acumulada (media móvil simple
# con conteo). Los consejos de trip_summary luego usan estos umbrales.

CAL_KEYS = {
    "rpm_idle":       ("rpm",    lambda r: r.get("speed", 0) is not None and r.get("speed", 0) < 2 and (r.get("coolant") or 0) > 70),
    "rpm_cruise":     ("rpm",    lambda r: (r.get("speed") or 0) > 80),
    "maf_idle":       ("maf",    lambda r: (r.get("speed") or 0) < 2 and (r.get("coolant") or 0) > 70),
    "coolant_cruise": ("coolant", lambda r: (r.get("speed") or 0) > 80),
    "voltage_run":    ("voltage", lambda r: (r.get("rpm") or 0) > 500),
    "voltage_stop":   ("voltage", lambda r: (r.get("rpm") or 0) < 100),
}


def update_calibration(conn, reading):
    """Actualiza medias acumuladas con la lectura actual (solo si el valor
    es plausible). Se llama una vez por ciclo."""
    for key, (field, cond) in CAL_KEYS.items():
        if not cond(reading):
            continue
        val = reading.get(field)
        if val is None or val <= 0:
            continue
        row = conn.execute(
            "SELECT value, n FROM calibration WHERE key=?", (key,)).fetchone()
        if row:
            value, n = row[0], row[1]
            new_n = n + 1
            new_value = (value * n + val) / new_n
            conn.execute(
                "UPDATE calibration SET value=?, n=?, updated=? WHERE key=?",
                (new_value, new_n, time.strftime("%Y-%m-%dT%H:%M:%S"), key))
        else:
            conn.execute(
                "INSERT INTO calibration (key, value, n, updated) VALUES (?,?,?,?)",
                (key, val, 1, time.strftime("%Y-%m-%dT%H:%M:%S")))


def get_calibration(conn, key, default=None):
    row = conn.execute(
        "SELECT value FROM calibration WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


# ── Detección de regeneración FAP ─────────────────────────
# Heurística sin PIDs de fabricante (el ELM327 estándar no expone temperatura
# de escape): el FAP en regeneración activa sube el ralentí del motor
# (c. +150-300 rpm) y aumenta el MAF en vacío de forma sostenida.
# Requiere N lecturas consecutivas con esas condiciones para no dar falsos
# positivos por calefacción eléctrica auxiliar o carga de alternador.

FAP_RPM_DELTA = 120          # rpm por encima del ralentí calibrado
FAP_MAF_MIN = 1.4            # multiplicador sobre MAF ralentí calibrado
FAP_MIN_CYCLES = 3           # lecturas consecutivas (~3 min)
FAP_OPEN_EXTRA = 2           # ciclos extra tras el último match para cerrar

_fap_state = {"count": 0, "start": None, "matches": 0, "grace": 0,
              "rpm_sum": 0.0, "maf_sum": 0.0}


def do_pid_scan():
    """Conexión dedicada para escanear PIDs soportados. Devuelve el set."""
    try:
        sock = socket.create_connection((BRIDGE_HOST, BRIDGE_PORT), timeout=BRIDGE_TIMEOUT)
        time.sleep(6)
        try:
            sock.settimeout(2)
            while sock.recv(4096):
                pass
        except Exception:
            pass
        read_pid(sock, "0100", timeout=12)
        try:
            sock.settimeout(1)
            while sock.recv(4096):
                pass
        except Exception:
            pass
        supported = scan_supported_pids(sock)
        sock.close()
        save_supported_pids(supported)
        return supported
    except Exception:
        return set()


def detect_fap_regen(conn, reading, ts):
    """Devuelve True si se abrió un evento nuevo de regeneración este ciclo.
    No-op si el vehículo no tiene DPF (vehicle.has_dpf=false, portabilidad)."""
    if not HAS_DPF:
        return False
    rpm = reading.get("rpm")
    maf = reading.get("maf")
    speed = reading.get("speed") or 0
    if rpm is None:
        return False

    idle_rpm = get_calibration(conn, "rpm_idle") or 800
    idle_maf = get_calibration(conn, "maf_idle") or 5.0
    threshold_rpm = idle_rpm + FAP_RPM_DELTA
    threshold_maf = idle_maf * FAP_MAF_MIN

    in_regen = (speed < 2 and rpm > threshold_rpm and
                (maf is None or maf > threshold_maf))

    st = _fap_state
    if in_regen:
        st["matches"] += 1
        st["rpm_sum"] += rpm
        if maf is not None:
            st["maf_sum"] += maf
        if st["matches"] >= FAP_MIN_CYCLES and st["start"] is None:
            st["start"] = ts
            log(f"FAP: posible regeneración detectada (ralentí {rpm:.0f} rpm, "
                f"MAF {maf if maf is not None else '?'} g/s)")
        st["grace"] = FAP_OPEN_EXTRA
    else:
        if st["start"] is not None:
            st["grace"] -= 1
            if st["grace"] <= 0:
                # Cerrar evento: rpm_sum incluye TODAS las lecturas in_regen
                # (matches), así que el promedio divide por matches totales.
                n_samples = max(1, st["matches"])
                rpm_avg = st["rpm_sum"] / n_samples
                maf_avg = st["maf_sum"] / n_samples
                # Duración real entre timestamps (si se puede parsear)
                dur_min = None
                try:
                    from datetime import datetime as _dt
                    t0 = _dt.fromisoformat(st["start"])
                    t1 = _dt.fromisoformat(ts)
                    dur_min = round((t1 - t0).total_seconds() / 60.0, 1)
                except Exception:
                    dur_min = round(n_samples * INTERVAL / 60.0, 1)
                try:
                    conn.execute(
                        "INSERT OR IGNORE INTO fap_events "
                        "(start_ts, end_ts, duration_min, rpm_avg, maf_avg) "
                        "VALUES (?,?,?,?,?)",
                        (st["start"], ts, dur_min, rpm_avg, maf_avg))
                    conn.commit()
                    log(f"FAP: regeneración terminada ({dur_min} min, "
                        f"rpm medio {rpm_avg:.0f})")
                except Exception:
                    pass
                st["start"] = None
                st["matches"] = 0
                st["rpm_sum"] = 0.0
                st["maf_sum"] = 0.0
                st["grace"] = 0

    if st["start"] is not None:
        st["count"] += 1
    return False


def main():
    # ⚠️ RACE FIX 2026-08-05: guard de instancia única. polar_boot_extra.sh
    # puede lanzar este script dos veces casi simultáneas (crond + RUN_COMMAND);
    # sin lock, dos procesos escriben la misma BD local → lecturas perdidas o
    # locks. flock se libera solo si el proceso muere (no hay locks huérfanos).
    lock_fd = open(os.path.join(DATA_DIR, "obd_local_collector.lock"), "w")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log("Ya hay otra instancia en ejecución — saliendo")
        sys.exit(0)

    subprocess.run(["termux-wake-lock"], capture_output=True)
    conn = db_connect()
    counter = 0
    # Cargar PIDs soportados conocidos (el escaneo se hace en el primer ciclo
    # que el bridge responde; luego se persiste para no repetirlo cada vez)
    supported = load_supported_pids()
    log("Recolector OBD local iniciado (autónomo, sin Internet)")

    while True:
        # ⚠️ FIX 2026-08-12: timestamp monótono. Si la tablet estuvo apagada
        # varios días, el RTC puede resetearse (p.ej. fecha de marzo 2026) y
        # time.strftime devolvería timestamps ANTERIORES al último guardado →
        # lecturas con fecha falsa que el importador ignora (viaje perdido).
        # Nunca retroceder: usar max(now, último timestamp local + 1s).
        now = time.strftime("%Y-%m-%dT%H:%M:%S")
        ts = now
        try:
            last_row = conn.execute(
                "SELECT MAX(timestamp) FROM readings").fetchone()
            if last_row and last_row[0]:
                last_ts = last_row[0]
                if last_ts > now:
                    # Reloj reseteado: avanzar 1s desde el último guardado
                    try:
                        import datetime as _dtmod
                        nxt = _dtmod.datetime.fromisoformat(last_ts)
                        ts = (nxt + _dtmod.timedelta(seconds=1)).isoformat()
                    except Exception:
                        ts = now
        except sqlite3.OperationalError:
            pass
        # DTCs solo en el ciclo de sync (~5 min): cuestan tiempo extra en el bus
        with_dtcs = (counter % SYNC_EVERY == 0)
        # Escaneo de PIDs soportados: primer ciclo (si no hay datos previos)
        # y cada 6h (360 ciclos) para refrescar. Conexión dedicada.
        if (not supported and counter % 2 == 0) or (counter > 0 and counter % 360 == 0):
            supported = do_pid_scan()
            if supported:
                log(f"PID scan: {len(supported)} soportados, "
                    f"diésel={'map' if '0B' in supported else '-'}/"
                    f"{'ambient' if '46' in supported else '-'}/"
                    f"{'fuel' if '23' in supported else '-'}")
        bridge = read_bridge(with_dtcs=with_dtcs, supported=supported)
        reading = bridge["reading"] if bridge else None
        dtcs = bridge.get("dtcs") if bridge else None
        if reading is None:
            # Sin respuesta del bridge: puede estar ocupado (contienda) o
            # muerto (ROM/LMKD). El guard isAlive del servicio lo ignora si
            # ya está activo — seguro lanzarlo.
            revive_bridge()
        gps = get_gps()

        position = None
        if gps and gps.get("latitude") is not None:
            position = {
                "timestamp": ts,
                "lat": gps["latitude"],
                "lon": gps["longitude"],
                "alt": gps.get("altitude"),
                "speed": gps.get("speed"),
                "bearing": gps.get("bearing"),
                "accuracy": gps.get("accuracy"),
                "provider": gps.get("provider"),
            }

        with conn:
            if reading:
                conn.execute(
                    "INSERT OR IGNORE INTO readings VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (ts, reading.get("rpm"), reading.get("speed"),
                     reading.get("coolant"), reading.get("throttle"),
                     reading.get("intake"), reading.get("fuel"),
                     reading.get("maf"), reading.get("voltage"),
                     reading.get("map"), reading.get("ambient"),
                     reading.get("fuel_pressure"), reading.get("fuel_rate"),
                     reading.get("engine_load")))
                update_calibration(conn, reading)
                detect_fap_regen(conn, reading, ts)
            if position:
                conn.execute(
                    "INSERT OR IGNORE INTO positions VALUES (?,?,?,?,?,?,?,?)",
                    (position["timestamp"], position["lat"], position["lon"],
                     position["alt"], position["speed"], position["bearing"],
                     position["accuracy"], position["provider"]))
            # DTCs: upsert por (code, kind) — si un fallo persiste, se actualiza
            # el timestamp del último avistamiento en vez de llenar de duplicados.
            if dtcs:
                all_codes = [(c, "stored") for c in dtcs.get("stored", [])] + \
                            [(c, "pending") for c in dtcs.get("pending", [])]
                for code, kind in all_codes:
                    desc = DTC_DESCRIPTIONS.get(code, "")
                    conn.execute(
                        "INSERT INTO dtc (timestamp, code, description, kind) "
                        "VALUES (?,?,?,?) "
                        "ON CONFLICT(code, kind) DO UPDATE SET timestamp=excluded.timestamp",
                        (ts, code, desc, kind))
        conn.commit()

        if counter % 60 == 0:
            extra = ""
            if dtcs:
                all_codes = dtcs.get("stored", []) + dtcs.get("pending", [])
                extra = f" dtc={','.join(all_codes) if all_codes else 'none'}"
            log(f"ciclo {counter}: readings={reading is not None} gps={position is not None}{extra}")

        counter += 1
        # Importar consumo CAN del CSV (CanSnifferService) antes del sync
        import_can_csv(conn)
        if counter % SYNC_EVERY == 0:
            if sync_to_cassiopeia():
                log("Sync a Cassiopeia OK")
            # Si no hay red, silencio — se reintenta en el próximo ciclo de sync

        time.sleep(INTERVAL)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
    except Exception as error:
        log(f"Error fatal: {error}")
        raise
