#!/usr/bin/env python3
"""
OBD2 Client for VgateBridge TCP tunnel.
Connects to vehicle tablet's VgateBridge TCP server and reads OBD2 data.
"""
import socket
import obd
import sys
import time

POLAR_STAR_HOST = "100.100.19.98"  # Tailscale IP
POLAR_STAR_PORT = 22000

class TcpSerial:
    """Wraps a TCP socket as a serial-like object for python-obd."""
    def __init__(self, host, port):
        self.sock = socket.create_connection((host, port), timeout=15)
    def read(self, size=1):
        return self.sock.recv(size)
    def write(self, data):
        return self.sock.sendall(data)
    def close(self):
        self.sock.close()
    def flush(self):
        pass
    @property
    def in_waiting(self):
        return 0

def main():
    print(f"🔌 Conectando a VgateBridge @ {POLAR_STAR_HOST}:{POLAR_STAR_PORT}...")
    sys.stdout.flush()
    
    serial = TcpSerial(POLAR_STAR_HOST, POLAR_STAR_PORT)
    connection = obd.OBD(serial, fast=False, timeout=30)
    
    if not connection.is_connected():
        print("❌ No se pudo conectar via TCP")
        return
    
    print(f"✅ Conectado! Protocolo: {connection.protocol_name()}")
    sys.stdout.flush()
    
    for cmd_name, label in [
        ("RPM", "RPM"),
        ("SPEED", "KM/H"),
        ("COOLANT_TEMP", "Temp. Refrig"),
        ("ENGINE_LOAD", "Carga Motor"),
        ("INTAKE_TEMP", "Temp. Admision"),
        ("THROTTLE_POS", "Mariposa"),
        ("FUEL_LEVEL", "Combustible"),
        ("AMBIANT_AIR_TEMP", "Temp. Exterior"),
        ("MAF", "MAF"),
    ]:
        try:
            cmd = getattr(obd.commands, cmd_name, None)
            if cmd:
                r = connection.query(cmd)
                if r and not r.is_null():
                    print(f"  {label}: {r.value}")
        except Exception as e:
            print(f"  {label}: error {e}")
        sys.stdout.flush()
    
    connection.close()
    print("🔌 Desconectado")

if __name__ == "__main__":
    main()
