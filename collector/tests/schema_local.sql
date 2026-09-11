CREATE TABLE readings (
        timestamp TEXT PRIMARY KEY,
        rpm REAL, speed REAL, coolant REAL, throttle REAL,
        intake REAL, fuel REAL, maf REAL, voltage REAL, map REAL, ambient REAL, fuel_pressure REAL, fuel_rate REAL, engine_load REAL);
CREATE TABLE positions (
        timestamp TEXT PRIMARY KEY,
        lat REAL, lon REAL, alt REAL, speed REAL,
        bearing REAL, accuracy REAL, provider TEXT);
CREATE TABLE dtc (
        timestamp TEXT,
        code TEXT,
        description TEXT,
        kind TEXT,
        PRIMARY KEY (code, kind));
CREATE TABLE fap_events (
        start_ts TEXT PRIMARY KEY,
        end_ts TEXT,
        duration_min REAL,
        rpm_avg REAL,
        maf_avg REAL);
CREATE TABLE calibration (
        key TEXT PRIMARY KEY,
        value REAL,
        n INTEGER,
        updated TEXT);
CREATE TABLE can_readings (
        ts TEXT PRIMARY KEY,
        consumption_l100 REAL,
        range_km REAL,
        odometer_km REAL);
