#!/usr/bin/env python3
"""Probe de PIDs OBD contra el bridge local (127.0.0.1:22000) — Polar Star.

Se ejecuta EN LA TABLET (Termux): `python3 probe_pids.py`. Parar antes el
recolector (`pkill -f obd_local_collector.py`): el bridge atiende a UN cliente
y con el local vivo el ELM se queda mudo.

Imprime la respuesta CRUDA de cada PID, que es lo que permite distinguir
"no soportado por el ECU" (`NO DATA`) de "soportado pero el parser falla"
(nº de bytes distinto del esperado). Así se encontró el bug de MAP (010B, un
solo byte) del 2026-09-13.
"""
import socket
import time

PIDS = [
    ("0100", "supported 01-20"), ("0120", "supported 21-40"),
    ("0140", "supported 41-60"),
    ("0104", "engine load"), ("0105", "coolant"), ("010B", "MAP/boost"),
    ("010C", "rpm"), ("010D", "speed"), ("010F", "intake temp"),
    ("0110", "MAF"), ("0111", "throttle"), ("011F", "runtime"),
    ("0121", "dist w/ MIL"), ("0123", "fuel rail press"),
    ("012F", "fuel level"), ("0133", "baro"), ("0142", "voltage"),
    ("0144", "lambda"), ("0146", "ambient"), ("014A", "accel pedal"),
    ("015E", "fuel rate"), ("0161", "torque demand"),
]

s = socket.create_connection(("127.0.0.1", 22000), timeout=10)
s.settimeout(6)
time.sleep(1)


def cmd(c, espera=1.2):
    s.sendall((c + "\r").encode())
    time.sleep(espera)
    buf = b""
    try:
        while True:
            chunk = s.recv(256)
            if not chunk:
                break
            buf += chunk
            if b">" in buf:
                break
    except socket.timeout:
        pass
    return buf.decode("ascii", "replace").replace("\r", " ").replace("\n", " ").strip()


print("init:", cmd("ATZ", 2.0)[:60])
cmd("ATE0")
print()
for pid, desc in PIDS:
    out = cmd(pid)
    print(f"{pid} {desc:20} → {out[:70]}")
s.close()
