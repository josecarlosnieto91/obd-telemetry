CREATE TABLE sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            start_time TEXT NOT NULL,
            end_time TEXT,
            distance_km REAL DEFAULT 0,
            max_speed REAL DEFAULT 0,
            avg_speed REAL DEFAULT 0,
            max_rpm REAL DEFAULT 0,
            driving_minutes INTEGER DEFAULT 0,
            status TEXT DEFAULT 'active'
        , fuel_liters REAL, consumption_l100 REAL);
CREATE TABLE readings (
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
            dtc_codes TEXT, map REAL, ambient REAL, fuel_pressure REAL, fuel_rate REAL,
            FOREIGN KEY (session_id) REFERENCES sessions(id)
        );
CREATE TABLE dtc (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER,
            timestamp TEXT NOT NULL,
            code TEXT NOT NULL,
            description TEXT,
            cleared INTEGER DEFAULT 0,
            FOREIGN KEY (session_id) REFERENCES sessions(id)
        );
CREATE TABLE alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER,
            timestamp TEXT NOT NULL,
            category TEXT NOT NULL,
            severity TEXT DEFAULT 'info',
            message TEXT NOT NULL,
            acknowledged INTEGER DEFAULT 0,
            FOREIGN KEY (session_id) REFERENCES sessions(id)
        );
CREATE INDEX idx_readings_session ON readings(session_id);
CREATE INDEX idx_readings_ts ON readings(timestamp);
CREATE TABLE positions (
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
CREATE INDEX idx_positions_session ON positions(session_id);
CREATE INDEX idx_positions_ts ON positions(timestamp);
CREATE TABLE fap_events (id INTEGER PRIMARY KEY AUTOINCREMENT, start_ts TEXT, end_ts TEXT, duration_min REAL, rpm_avg REAL, maf_avg REAL);
CREATE TABLE calibration (key TEXT PRIMARY KEY, value REAL, n INTEGER, updated TEXT);
CREATE TABLE refuels (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT NOT NULL,
        prev_ts TEXT,
        fuel_before REAL,
        fuel_after REAL,
        jump_pct REAL,
        liters REAL,
        full_tank INTEGER DEFAULT 0,
        session_id INTEGER, price_per_l REAL, cost REAL, station TEXT, source TEXT DEFAULT 'level',
        UNIQUE(prev_ts, ts)
    );
CREATE TABLE services (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        maintenance_id TEXT NOT NULL,
        ts TEXT NOT NULL,
        odometer_km REAL
    );
CREATE TABLE can_readings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER,
            ts TEXT NOT NULL,
            consumption_l100 REAL,
            range_km REAL,
            odometer_km REAL);
