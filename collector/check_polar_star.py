#!/home/josecnr91/.hermes/hermes-agent/venv/bin/python3
"""
Polar Star — detecta arranque, llegada a casa y guarda tracks GPS.

Cronjob no_agent=True. Silencioso en estado estable.
Notifica cuando:
  - 🚗 El coche ARRANCA
  - 🏠 El coche LLEGA A CASA
  - 📍 Cada trayecto guardado como track GPX

Cada arranque → nuevo track GPS en ~/.hermes/data/tracks/
"""
import os, json, time, subprocess, math
from datetime import datetime

STATE_FILE = os.path.expanduser("~/.hermes/data/polar_star_state.json")
TRACK_DIR = os.path.expanduser("~/.hermes/data/tracks")

# Configuración propia — ~/.hermes/config/polar_star.json
def load_cfg():
    with open(os.path.expanduser("~/.hermes/config/polar_star.json")) as f:
        return json.load(f)

CFG = load_cfg()
SSH_CMD = ["ssh", "-o", "ConnectTimeout=8", "-o", "StrictHostKeyChecking=no",
           "-i", os.path.expanduser(CFG["ssh"]["key"]),
           "-p", str(CFG["ssh"]["port"]),
           f"{CFG['ssh']['user']}@{CFG['ssh']['host']}"]

HOME_LAT = CFG["home"]["lat"]
HOME_LON = CFG["home"]["lon"]
HOME_RADIUS = CFG["home"]["radius_m"]

# Si el GPS no se mueve más de 30m en 3 ticks (~15 min), cerramos el track
MIN_MOVE_METERS = 30
MAX_IDLE_CHECKS = 3

def now_ts():
    return int(time.time())

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except:
            pass
    return {"last_seen": None, "last_unreachable": None, "last_gps": None,
            "notified_start": False, "notified_home": False,
            "track_active": False, "track_points": [], "track_start": None,
            "idle_checks": 0}

def save_state(s):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(s, f)

def run_ssh(cmd_list):
    full_cmd = SSH_CMD + cmd_list
    try:
        r = subprocess.run(full_cmd, capture_output=True, text=True, timeout=12)
        if r.returncode == 0:
            return True, r.stdout.strip()
        return False, r.stderr.strip()[:200]
    except:
        return False, "timeout/error"

def get_gps(prev_gps):
    ok, out = run_ssh(["termux-location"])
    if ok and out:
        try:
            gps = json.loads(out)
            if isinstance(gps, dict) and "latitude" in gps:
                return gps
        except:
            pass
    return prev_gps

def haversine(lat1, lon1, lat2, lon2):
    R = 6371000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi/2)**2 + math.cos(phi1)*math.cos(phi2)*math.sin(dlam/2)**2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))

def is_at_home(lat, lon):
    return haversine(lat, lon, HOME_LAT, HOME_LON) < HOME_RADIUS

def format_duration(seconds):
    if seconds < 60:
        return f"{seconds}s"
    mins = seconds // 60
    if mins < 60:
        return f"{mins} min"
    hours = mins // 60
    mins_rem = mins % 60
    if hours < 24:
        return f"{hours}h {mins_rem}min"
    days = hours // 24
    return f"{days}d {hours % 24}h"

def gpx_header():
    return '<?xml version="1.0" encoding="UTF-8"?>\n<gpx version="1.1" creator="cassiopeia-polar-star" xmlns="http://www.topografix.com/GPX/1/1">\n  <trk>\n    <name>Track</name>\n    <trkseg>'

def gpx_footer():
    return '  </trkseg>\n  </trk>\n</gpx>'

def gpx_point(pt):
    ts = datetime.utcfromtimestamp(pt["ts"]).strftime("%Y-%m-%dT%H:%M:%SZ")
    line = f'    <trkpt lat="{pt["lat"]}" lon="{pt["lon"]}">\n'
    line += f'      <ele>{pt.get("alt", 0)}</ele>\n'
    line += f'      <time>{ts}</time>\n'
    if pt.get("speed", -1) >= 0:
        line += f'      <speed>{pt["speed"]}</speed>\n'
    return line + '    </trkpt>'

def save_track_gpx(points, start_time):
    """Save track points as GPX file and return the filename."""
    os.makedirs(TRACK_DIR, exist_ok=True)
    dt = datetime.fromtimestamp(start_time)
    fname = f"track_{dt.strftime('%Y%m%d_%H%M%S')}.gpx"
    fpath = os.path.join(TRACK_DIR, fname)
    
    with open(fpath, "w") as f:
        f.write(gpx_header())
        for pt in points:
            f.write("\n")
            f.write(gpx_point(pt))
        f.write("\n")
        f.write(gpx_footer())
        f.write("\n")
    
    # Also save a compact JSON summary
    total_dist = 0
    for i in range(1, len(points)):
        total_dist += haversine(points[i-1]["lat"], points[i-1]["lon"],
                                points[i]["lat"], points[i]["lon"])
    
    end_time = points[-1]["ts"] if points else start_time
    duration = end_time - start_time
    n_points = len(points)
    
    summary = {
        "file": fname,
        "date": dt.strftime("%Y-%m-%d"),
        "start": dt.strftime("%H:%M:%S"),
        "duration_sec": duration,
        "distance_m": total_dist,
        "points": n_points
    }
    
    summary_path = fpath.replace(".gpx", ".json")
    with open(summary_path, "w") as f:
        json.dump(summary, f, indent=2)
    
    return fname, summary

def format_track_summary(summary):
    dist_km = summary["distance_m"] / 1000
    dur = format_duration(summary["duration_sec"])
    return f"📁 **Track guardado:** `{summary['file']}`\n📏 {dist_km:.1f} km | ⏱ {dur} | 📍 {summary['points']} puntos"

def main():
    state = load_state()
    now = now_ts()
    os.makedirs(TRACK_DIR, exist_ok=True)

    # Try to reach polar-star
    ok, _ = run_ssh(["echo", "ping"])

    was_unreachable = state.get("last_unreachable") is not None
    track_active = state.get("track_active", False)
    track_start = state.get("track_start")
    track_points = state.get("track_points", [])

    lines = []

    if ok:
        # --- REACHABLE ---
        last_seen = state.get("last_seen")
        was_unreachable_since = state.get("last_unreachable")
        was_notified_start = state.get("notified_start", False)
        was_notified_home = state.get("notified_home", False)

        state["last_seen"] = now
        state["last_unreachable"] = None

        # Get fresh GPS
        gps = get_gps(state.get("last_gps"))
        state["last_gps"] = gps

        lat = gps.get("latitude", HOME_LAT) if gps else HOME_LAT
        lon = gps.get("longitude", HOME_LON) if gps else HOME_LON
        at_home = is_at_home(lat, lon)

        # Uptime
        uptime_secs = 0
        uptime_ok, uptime_raw = run_ssh(["cat", "/proc/uptime"])
        if uptime_ok and uptime_raw:
            try:
                uptime_secs = int(float(uptime_raw.split()[0]))
            except:
                pass

        # --- TRACK LOGIC ---
        # 1. START NEW TRACK when car turns on
        if was_unreachable and was_unreachable_since is not None:
            state["track_active"] = True
            state["track_start"] = now
            state["track_points"] = []
            state["idle_checks"] = 0
            track_active = True
            track_start = now
            track_points = []

            if gps:
                pt = {
                    "lat": lat, "lon": lon,
                    "alt": gps.get("altitude", 0),
                    "speed": gps.get("speed", -1),
                    "ts": now
                }
                track_points.append(pt)
                state["track_points"] = [pt]

        # 2. ADD POINT to active track (if moved enough)
        elif track_active and gps:
            last_pt = track_points[-1] if track_points else None
            moved = True
            if last_pt:
                dist = haversine(last_pt["lat"], last_pt["lon"], lat, lon)
                moved = dist > MIN_MOVE_METERS
            
            if moved:
                pt = {
                    "lat": lat, "lon": lon,
                    "alt": gps.get("altitude", 0),
                    "speed": gps.get("speed", -1),
                    "ts": now
                }
                track_points.append(pt)
                state["track_points"] = track_points
                state["idle_checks"] = 0
            else:
                state["idle_checks"] = state.get("idle_checks", 0) + 1

        # 3. CLOSE TRACK if idle too long or arrived home
        if track_active and (at_home or state.get("idle_checks", 0) >= MAX_IDLE_CHECKS):
            if len(track_points) >= 2:
                fname, summary = save_track_gpx(track_points, track_start)
                lines.append("📍 **Trayecto completado**")
                lines.append(format_track_summary(summary))
            state["track_active"] = False
            state["track_points"] = []
            state["track_start"] = None
            state["idle_checks"] = 0

        # --- NOTIFICATIONS (same as before) ---
        # Car started
        if was_unreachable and was_unreachable_since is not None:
            state["notified_start"] = True
            state["notified_home"] = False
            offline_h = format_duration(now - was_unreachable_since)
            notif = [f"🚗 **Coche arrancado**", "",
                     f"⏱ **Apagado durante:** {offline_h}"]
            if uptime_secs > 0 and uptime_secs < 600:
                notif.append(f"⚡ **Recién encendido** ({format_duration(uptime_secs)})")
            lines = notif + lines  # prepend

        # Arrived home
        elif at_home and not was_notified_home:
            state["notified_home"] = True
            state["notified_start"] = False

        # Left home
        elif not at_home and was_notified_home and not was_notified_start:
            state["notified_home"] = False

        if was_notified_start:
            state["notified_start"] = False

        state.setdefault("notified_start", False)
        state.setdefault("notified_home", False)

        if lines:
            # Add track count info
            tracks_today = [f for f in os.listdir(TRACK_DIR) if f.startswith("track_") and datetime.today().strftime("%Y%m%d") in f]
            if len(lines) > 0:
                print("\n".join(lines))

        save_state(state)

    else:
        # --- UNREACHABLE ---
        # Close track if car went offline while tracking
        if track_active and len(track_points) >= 2:
            fname, summary = save_track_gpx(track_points, track_start)
            print(f"📍 **Trayecto completado** (desconexión)")
            print(format_track_summary(summary))
        
        if not was_unreachable:
            state["last_unreachable"] = now
        state["track_active"] = False
        state["track_points"] = []
        state["track_start"] = None
        state["idle_checks"] = 0
        save_state(state)

if __name__ == "__main__":
    main()
