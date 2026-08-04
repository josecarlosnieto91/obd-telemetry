#!/data/data/com.termux/files/usr/bin/python3
"""vehicle tablet GPS Logger — continuous GPS tracking for car trips.

Captures GPS position every 15 seconds, writes GPX tracks,
detects trip start/stop, syncs to server when possible.
Designed to survive power cuts (appends, not buffers).
"""
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

# ── Config ──
HOME = os.environ.get("HOME", "/data/data/com.termux/files/home")
TRACK_DIR = os.path.join(HOME, "tracks")
STATE_DIR = os.path.join(HOME, ".track_state")
LOG_FILE = os.path.join(STATE_DIR, "logger.log")
STATE_FILE = os.path.join(STATE_DIR, "current.json")

GPS_INTERVAL = 15          # seconds between GPS reads
STOP_TIMEOUT = 180         # seconds stopped before ending track (3 min)
MOVE_THRESHOLD = 20        # meters to consider "moving"
CASSIOPEIA = "user@server"   # MagicDNS — no IP hardcodeada
CASSIOPEIA_PATH = "~/.hermes/data/tracks"
SSH_KEY = os.path.join(HOME, ".ssh", "id_ed25519")

os.makedirs(TRACK_DIR, exist_ok=True)
os.makedirs(STATE_DIR, exist_ok=True)


def log(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


def get_gps():
    """Get GPS position via termux-location"""
    try:
        # Try passive (instant) first
        r = subprocess.run(
            ["termux-location", "-p", "passive"],
            capture_output=True, text=True, timeout=5
        )
        if r.returncode == 0:
            return json.loads(r.stdout)

        # Fallback to gps
        r = subprocess.run(
            ["termux-location", "-p", "gps"],
            capture_output=True, text=True, timeout=15
        )
        if r.returncode == 0:
            return json.loads(r.stdout)
    except Exception:
        pass
    return None


def haversine(lat1, lon1, lat2, lon2):
    """Distance in meters between two GPS coordinates"""
    import math
    R = 6371000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi/2)**2 + math.cos(phi1)*math.cos(phi2)*math.sin(dlam/2)**2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))


def gpx_header():
    return '''<?xml version="1.0" encoding="UTF-8"?>
<gpx version="1.1" creator="vehicle-gps-logger" xmlns="http://www.topografix.com/GPX/1/1">
  <trk>
    <name>Track</name>
    <trkseg>
'''


def gpx_point(lat, lon, alt, speed, timestamp):
    return f'''    <trkpt lat="{lat}" lon="{lon}">
      <ele>{alt}</ele>
      <time>{timestamp}</time>
      <speed>{speed}</speed>
    </trkpt>
'''


def gpx_footer():
    return '''  </trkseg>
  </trk>
</gpx>
'''


def sync_to_server(gpx_file):
    """Upload GPX file to server via SCP"""
    try:
        r = subprocess.run(
            ["scp", "-o", "ConnectTimeout=10", "-o", "StrictHostKeyChecking=no",
             "-i", SSH_KEY, gpx_file, f"{CASSIOPEIA}:{CASSIOPEIA_PATH}/"],
            capture_output=True, text=True, timeout=15
        )
        if r.returncode == 0:
            log(f"✅ Subido {os.path.basename(gpx_file)}")
            return True
        else:
            log(f"⚠️ Error subida: {r.stderr[:100]}")
    except Exception as e:
        log(f"⚠️ No se pudo subir: {e}")
    return False


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {
            "tracking": False,
            "points": 0,
            "start_time": None,
            "last_lat": None,
            "last_lon": None,
            "idle_seconds": 0,
            "current_file": None
        }


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


def main():
    # Wake lock
    subprocess.run(["termux-wake-lock"], capture_output=True)

    state = load_state()
    log(f"Iniciando logger GPS (tracking={state['tracking']})")

    # Resume or start fresh
    if state["tracking"] and state["current_file"] and os.path.exists(state["current_file"]):
        # Previous session was interrupted (car turned off mid-track)
        # Finalize the GPX and upload it, then start fresh
        gpx_file = state["current_file"]
        log(f"Track anterior detectado: {os.path.basename(gpx_file)} ({state['points']} puntos)")
        with open(gpx_file, "a") as f:
            f.write(gpx_footer())
        log(f"🏁 Track finalizado (recuperado tras corte)")
        sync_to_server(gpx_file)
        # Reset completely
        state = {
            "tracking": False,
            "points": 0,
            "start_time": None,
            "last_lat": None,
            "last_lon": None,
            "idle_seconds": 0,
            "current_file": None
        }
        gpx_file = None

    while True:
        gps = get_gps()
        if not gps or gps.get("latitude") is None:
            time.sleep(5)
            continue

        lat = gps["latitude"]
        lon = gps["longitude"]
        alt = gps.get("altitude", 0) or 0
        speed = gps.get("speed", -1) or -1
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        # Determine if moving
        moving = False
        if speed >= 1.0:  # > 3.6 km/h
            moving = True
        elif state["last_lat"] is not None and state["last_lon"] is not None:
            dist = haversine(state["last_lat"], state["last_lon"], lat, lon)
            if dist > MOVE_THRESHOLD:
                moving = True

        # Start new track if moving and not tracking
        if not state["tracking"] and moving:
            state["tracking"] = True
            state["points"] = 0
            state["idle_seconds"] = 0
            fname = f"track_{datetime.now().strftime('%Y%m%d_%H%M%S')}.gpx"
            gpx_file = os.path.join(TRACK_DIR, fname)
            state["current_file"] = gpx_file
            with open(gpx_file, "w") as f:
                f.write(gpx_header())
            log(f"🚗 Nuevo track: {fname}")

        # Add point if tracking
        if state["tracking"] and gpx_file:
            with open(gpx_file, "a") as f:
                f.write(gpx_point(lat, lon, alt, speed, ts))
            state["points"] += 1
            state["last_lat"] = lat
            state["last_lon"] = lon
            state["idle_seconds"] = 0
        else:
            state["idle_seconds"] += GPS_INTERVAL
            if state["last_lat"] is None:
                state["last_lat"] = lat
                state["last_lon"] = lon

        # Check idle timeout
        if state["tracking"] and not moving:
            state["idle_seconds"] += GPS_INTERVAL

        if state["tracking"] and state["idle_seconds"] >= STOP_TIMEOUT:
            # End track
            if gpx_file and os.path.exists(gpx_file):
                with open(gpx_file, "a") as f:
                    f.write(gpx_footer())
                log(f"🏁 Track finalizado: {os.path.basename(gpx_file)} ({state['points']} puntos)")
                sync_to_server(gpx_file)

            state["tracking"] = False
            state["points"] = 0
            state["current_file"] = None
            gpx_file = None

        # Save state
        save_state(state)
        time.sleep(GPS_INTERVAL)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("Logger detenido por usuario")
    except Exception as e:
        log(f"Error fatal: {e}")
        raise
