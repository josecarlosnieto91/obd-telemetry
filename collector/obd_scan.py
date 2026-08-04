#!/usr/bin/env python3
"""Escaneo exhaustivo del OBD — lee TODO lo que el vehículo expone.

Modos probados:
  - 01: PIDs de datos en vivo (todos los bitmaps de soporte 0100-01C0)
  - 02: DTC congelados (freeze frame)
  - 03: DTC almacenados
  - 07: DTC pendientes
  - 09: VIN, calibración, ECU name (si el coche lo permite)
  - 22: PIDs de fabricante (barrido selectivo de rangos PSA comunes)

Salida: imprime un informe legible + guarda JSON en ~/.hermes/data/obd_scan.json

Uso: python3 obd_scan.py [host] [port]
"""
import socket, time, sys, json, os, re, datetime

HOST = sys.argv[1] if len(sys.argv) > 1 else "100.64.0.1"
PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 22000
OUT = os.path.expanduser("~/.hermes/data/obd_scan.json")

# PIDs Mode 01 estándar con nombre y decodificación
PID_NAMES = {
    "00": "Bitmap soporte 01-20", "20": "Bitmap soporte 21-40",
    "40": "Bitmap soporte 41-60", "60": "Bitmap soporte 61-80",
    "80": "Bitmap soporte 81-A0", "A0": "Bitmap soporte A1-C0",
    "C0": "Bitmap soporte C1-E0", "E0": "Bitmap soporte E1-FF",
    "01": "Monitor status DTCs", "02": "DTC que provocó freeze frame",
    "03": "Sistema de combustible", "04": "Carga motor (%)",
    "05": "Temp refrigerante (°C)", "06": "Ajuste combustible banco 1 (%)",
    "07": "Ajuste combustible banco 1 a largo (%)", "08": "Ajuste combustible banco 2 (%)",
    "09": "Ajuste combustible banco 2 a largo (%)", "0A": "Presión fuel (kPa)",
    "0B": "Presión manifold (kPa)", "0C": "RPM", "0D": "Velocidad (km/h)",
    "0E": "Avance encendido (°)", "0F": "Temp admisión (°C)",
    "10": "MAF (g/s)", "11": "Posición mariposa (%)", "12": "Estado aire secundario",
    "13": "Sensores O2", "14": "Sensor O2 B1S1 (V)", "15": "Sensor O2 B1S2 (V)",
    "16": "Sensor O2 B1S3 (V)", "17": "Sensor O2 B1S4 (V)",
    "18": "Sensor O2 B2S1 (V)", "19": "Sensor O2 B2S2 (V)",
    "1A": "Sensor O2 B2S3 (V)", "1B": "Sensor O2 B2S4 (V)",
    "1C": "Normativa OBD", "1D": "Sensores O2 aux", "1E": "Estado sensor aux",
    "1F": "Tiempo motor desde arranque (s)", "21": "Distancia con MIL (km)",
    "22": "Presión raíl (kPa)", "23": "Presión raíl Diesel (kPa)",
    "2C": "Registro EGR", "2D": "Desgaste EGR", "2E": "Desgaste EVAP",
    "2F": "Nivel combustible (%)", "30": "Nº calentamientos",
    "31": "Distancia desde DTCs (km)", "32": "Presión EVAP (Pa)",
    "33": "Presión absoluta manifold (kPa)", "34": "Sensores O2 (4 sensores)",
    "3C": "Temp catalizador B1S1 (°C)", "3D": "Temp catalizador B1S2 (°C)",
    "3E": "Temp catalizador B2S1 (°C)", "3F": "Temp catalizador B2S2 (°C)",
    "41": "Monitor status (continuo)", "42": "Control voltaje ECU (V)",
    "43": "Carga alternador (%)", "44": "Temp fuel (°C)",
    "45": "Temp admisión relativa (°C)", "46": "Temp ambiente (°C)",
    "47": "Presión absoluta EGR (kPa)", "49": "Acelerador D (%)",
    "4A": "Acelerador E (%)", "4C": "Acelerador F (%)",
    "4D": "Tiempo con MIL (min)", "4E": "Tiempo desde DTCs (min)",
    "51": "Tipo fuel", "52": "Etanol (%)",
    "5C": "Temp aceite (°C)", "5E": "Presión inyección (MPa)",
    "5F": "Torque real (%)", "61": "Torque del conductor (%)",
    "62": "Torque motor referencia (%)",
}


def send_cmd(sock, cmd, wait=1.2):
    try:
        sock.sendall(cmd + b"\r\n")
    except Exception:
        return b""
    time.sleep(wait)
    d = b""
    try:
        while True:
            c = sock.recv(4096)
            if not c:
                break
            d += c
            if b">" in c:
                break
    except Exception:
        pass
    return d


def decode_hex_resp(resp):
    """Extrae la respuesta hex limpia: '41 0C 1A F8' → bytes."""
    m = re.search(rb"4[0179] [0-9A-Fa-f]{2}[ \r\n]+([0-9A-Fa-f ]+)", resp)
    if not m:
        return None
    return [int(x, 16) for x in m.group(1).split()]


def main():
    print(f"Conectando a {HOST}:{PORT}...")
    s = socket.create_connection((HOST, PORT), timeout=8)
    s.settimeout(3)
    # Esperar init del ELM + bridge
    time.sleep(7)
    try:
        while s.recv(4096):
            pass
    except Exception:
        pass
    # Warm-up
    r = send_cmd(s, b"ATI", 1.5)
    print("ELM:", r.decode(errors="replace").strip()[:60])

    result = {
        "scan_time": datetime.datetime.now().isoformat(),
        "elms": r.decode(errors="replace").strip(),
        "live_pids": {},
        "freeze_frame": {},
        "dtc_stored": [], "dtc_pending": [],
        "vehicle_info": {},
        "manufacturer_pids": {},
    }

    # ── Mode 01: escanear soporte → leer todos los PIDs soportados ──
    print("\n=== MODE 01 — PIDs de datos en vivo ===")
    # Primero descubrir los bitmaps de soporte
    supported = set()
    bitmap_chain = ["00"]
    for base in ["00", "20", "40", "60", "80", "A0", "C0"]:
        resp = send_cmd(s, ("01" + base).encode())
        print(f"  010{base[-1]} -> {resp.decode(errors='replace').strip()[:70]}" if base == "00" else f"  01{base} -> {resp.decode(errors='replace').strip()[:70]}")
        # parsear todos los 41 xx presentes (el ELM a veces duplica)
        best = set()
        for m in re.finditer(rb"41 ([0-9A-Fa-f]{2})[ \r\n]+([0-9A-Fa-f ]+)", resp):
            payload = [int(x, 16) for x in m.group(2).split()]
            if len(payload) < 4:
                continue
            bv = int(m.group(1), 16)
            cand = set()
            for bit in range(32):
                bi = bit // 8
                if payload[bi] & (1 << (7 - (bit % 8))):
                    pid = bv + bit + 1
                    if 1 <= pid <= 0xFF:
                        cand.add(pid)
            if len(cand) > len(best):
                best = cand
        # El bitmap en sí (0x00) indica soporte de 0x01-0x20; añadir el pid base
        for m in re.finditer(rb"41 ([0-9A-Fa-f]{2})[ \r\n]+([0-9A-Fa-f ]+)", resp):
            bv = int(m.group(1), 16)
            if bv in (0x00, 0x20, 0x40, 0x60, 0x80, 0xA0, 0xC0):
                supported.add(bv)  # el bitmap mismo = soportado
        supported |= best

    pids_sorted = sorted(supported)
    print(f"\nPIDs soportados ({len(pids_sorted)}):", " ".join(f"{p:02X}" for p in pids_sorted))

    # Leer cada PID soportado (vivo)
    for pid in pids_sorted:
        if pid in (0x00, 0x20, 0x40, 0x60, 0x80, 0xA0, 0xC0):
            continue  # son bitmaps, ya leídos
        resp = send_cmd(s, ("01" + f"{pid:02X}").encode(), 0.9)
        hexb = decode_hex_resp(resp)
        name = PID_NAMES.get(f"{pid:02X}", f"PID {pid:02X}")
        if hexb:
            # Decodificaciones básicas de los PIDs más útiles
            decoded = None
            if pid == 0x0C:
                decoded = f"{(hexb[0]*256 + hexb[1])/4:.0f} rpm"
            elif pid == 0x0D:
                decoded = f"{hexb[0]} km/h"
            elif pid == 0x05:
                decoded = f"{hexb[0]-40} °C"
            elif pid == 0x0F:
                decoded = f"{hexb[0]-40} °C"
            elif pid == 0x10:
                decoded = f"{(hexb[0]*256 + hexb[1])/100:.1f} g/s"
            elif pid == 0x0B:
                decoded = f"{hexb[0]} kPa abs"
            elif pid == 0x0A:
                decoded = f"{hexb[0]*3} kPa"
            elif pid == 0x04:
                decoded = f"{hexb[0]*100/255:.0f} %"
            elif pid == 0x11:
                decoded = f"{hexb[0]*100/255:.0f} %"
            elif pid == 0x2F:
                decoded = f"{hexb[0]*100/255:.0f} %"
            elif pid == 0x46:
                decoded = f"{hexb[0]-40} °C"
            elif pid == 0x42:
                decoded = f"{hexb[0]*0.001 + hexb[1]*0.001:.2f} V"
            elif pid == 0x5C:
                decoded = f"{hexb[0]-40} °C"
            result["live_pids"][f"{pid:02X}"] = {
                "name": name, "raw": " ".join(f"{x:02X}" for x in hexb), "decoded": decoded,
            }
            print(f"  {pid:02X} {name}: raw={' '.join(f'{x:02X}' for x in hexb)} {'→ ' + decoded if decoded else ''}")
        else:
            print(f"  {pid:02X} {name}: (sin respuesta)")
        # Breve pausa para no saturar el bus
        time.sleep(0.3)

    # ── Mode 02: freeze frame ──
    print("\n=== MODE 02 — DTC congelados (freeze frame) ===")
    resp = send_cmd(s, b"0202", 1.5)
    print("  ", resp.decode(errors="replace").strip()[:120])
    result["freeze_frame"]["raw"] = resp.decode(errors="replace").strip()

    # ── Mode 03: DTC almacenados ──
    print("\n=== MODE 03 — DTC almacenados ===")
    resp = send_cmd(s, b"03", 1.5)
    print("  ", resp.decode(errors="replace").strip()[:200])
    result["dtc_stored"] = resp.decode(errors="replace").strip()

    # ── Mode 07: DTC pendientes ──
    print("\n=== MODE 07 — DTC pendientes ===")
    resp = send_cmd(s, b"07", 1.5)
    print("  ", resp.decode(errors="replace").strip()[:200])
    result["dtc_pending"] = resp.decode(errors="replace").strip()

    # ── Mode 09: información del vehículo ──
    print("\n=== MODE 09 — Información del vehículo ===")
    for pid in ["00", "01", "02", "04", "0A"]:
        resp = send_cmd(s, ("09" + pid).encode(), 1.5)
        print(f"  09{pid} -> {resp.decode(errors='replace').strip()[:120]}")
        result["vehicle_info"][f"09{pid}"] = resp.decode(errors="replace").strip()
        if b"NO DATA" not in resp and pid in ("02", "04", "0A"):
            # intentar decodificar VIN/cálculo ID desde la respuesta
            pass

    # ── Mode 22: PIDs de fabricante (barrido PSA común) ──
    print("\n=== MODE 22 — PIDs de fabricante (barrido) ===")
    # PSA/Bosch EDC16: rangos probados en foros (nivel, presión, EGR, etc.)
    probe_ranges = [
        list(range(0x00, 0x10)),    # 00-0F
        list(range(0x20, 0x30)),    # 20-2F
        list(range(0x50, 0x60)),    # 50-5F
        list(range(0xA0, 0xB0)),    # A0-AF
        list(range(0xF0, 0x100)),   # F0-FF
        [0x1A40, 0x1A41, 0x1A42, 0x1A43, 0x1A44, 0x1A45, 0x1A46, 0x1A47, 0x1A48, 0x1A49],
        [0x2A00, 0x2A01, 0x2A02, 0x2A03],
    ]
    found_man = 0
    for rng in probe_ranges:
        for pid in rng:
            cmd = ("22" + f"{pid:04X}").encode()
            resp = send_cmd(s, cmd, 0.8)
            if resp and b"NO DATA" not in resp and b"SEARCHING" in resp or (resp and b"NO DATA" not in resp and b"7F" not in resp):
                # Respuesta con datos (no error)
                clean = resp.decode(errors="replace").strip()
                if "62" in clean and "NO DATA" not in clean and "7F" not in clean:
                    found_man += 1
                    result["manufacturer_pids"][f"22{pid:04X}"] = clean
                    print(f"  ✅ 22{pid:04X} -> {clean[:80]}")
            time.sleep(0.15)
        print(f"  ... rango {rng[0]:04X}-{rng[-1]:04X} escaneado")
    if not found_man:
        print("  (sin PIDs de fabricante con datos en los rangos probados)")

    s.close()

    # ── Guardar export ──
    with open(OUT, "w") as fh:
        json.dump(result, fh, indent=2, ensure_ascii=False)
    print(f"\n📁 Export completo: {OUT}")
    print(f"Resumen: {len(result['live_pids'])} PIDs vivos, {len(result['manufacturer_pids'])} PIDs fabricante")
    return result


if __name__ == "__main__":
    main()
