"""SQLite storage of recorded OpenSky snapshots (rolling window, airborne aircraft only)."""

import sqlite3
import threading

from . import config
from .geo import FT_PER_M, KT_PER_MS

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    t INTEGER PRIMARY KEY,
    n INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS states (
    t INTEGER NOT NULL,
    icao24 TEXT NOT NULL,
    callsign TEXT,
    country TEXT,
    lat REAL NOT NULL,
    lon REAL NOT NULL,
    alt_ft REAL NOT NULL,
    gs_kt REAL,
    trk REAL,
    vs_fpm REAL,
    category INTEGER,
    squawk TEXT,
    PRIMARY KEY (t, icao24)
);
CREATE INDEX IF NOT EXISTS states_icao_t ON states (icao24, t);
"""

# Column order of a row returned by the query helpers below.
ROW_FIELDS = ("t", "icao24", "callsign", "country", "lat", "lon", "alt_ft",
              "gs_kt", "trk", "vs_fpm", "category", "squawk")


class Store:
    def __init__(self, path=None):
        path = path or config.DB_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.executescript(SCHEMA)
        self._lock = threading.Lock()

    def insert_snapshot(self, payload):
        """Store an OpenSky /states/all response. Returns number of airborne rows stored."""
        t = int(payload.get("time") or 0)
        rows = []
        for s in payload.get("states") or []:
            lat, lon, on_ground = s[6], s[5], s[8]
            alt_m = s[7] if s[7] is not None else s[13]
            if lat is None or lon is None or on_ground or alt_m is None:
                continue
            t_pos = s[3] or s[4] or t
            if t - t_pos > 60:          # stale position, skip
                continue
            rows.append((
                t, s[0], (s[1] or "").strip() or None, s[2], lat, lon,
                alt_m * FT_PER_M,
                s[9] * KT_PER_MS if s[9] is not None else None,
                s[10],
                s[11] * FT_PER_M * 60 if s[11] is not None else None,
                s[17] if len(s) > 17 else None,
                s[14],
            ))
        with self._lock:
            self._db.execute("BEGIN")
            self._db.execute("INSERT OR REPLACE INTO snapshots VALUES (?, ?)", (t, len(rows)))
            self._db.executemany(
                "INSERT OR REPLACE INTO states VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)
            self._db.execute("COMMIT")
        return len(rows)

    def purge_before(self, t):
        with self._lock:
            self._db.execute("DELETE FROM states WHERE t < ?", (t,))
            self._db.execute("DELETE FROM snapshots WHERE t < ?", (t,))

    def coverage(self):
        """(first_t, last_t, snapshot_count) or (None, None, 0)."""
        with self._lock:
            first, last, n = self._db.execute(
                "SELECT MIN(t), MAX(t), COUNT(*) FROM snapshots").fetchone()
        return first, last, n

    def snapshot_times(self, t_from, t_to):
        with self._lock:
            return [r[0] for r in self._db.execute(
                "SELECT t FROM snapshots WHERE t > ? AND t <= ? ORDER BY t", (t_from, t_to))]

    def states_between(self, t_from, t_to):
        """Rows with t_from < t <= t_to, ordered by time."""
        with self._lock:
            return self._db.execute(
                "SELECT * FROM states WHERE t > ? AND t <= ? ORDER BY t", (t_from, t_to)).fetchall()
