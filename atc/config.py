"""Runtime configuration. Everything can be overridden with environment variables."""

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("VISOR_DATA_DIR", ROOT / "data"))            # recordings (writable)
NAVDATA_DIR = Path(os.environ.get("VISOR_NAVDATA_DIR", DATA_DIR))          # airports/navaids CSVs
PUBLIC_DIR = ROOT / "public"
DB_PATH = DATA_DIR / "traffic.db"

# Simulated/recorded airspace. The map, recorder and simulator are all clipped to this box.
EUROPE = {
    "lat_min": float(os.environ.get("VISOR_LAT_MIN", 34.0)),
    "lat_max": float(os.environ.get("VISOR_LAT_MAX", 72.0)),
    "lon_min": float(os.environ.get("VISOR_LON_MIN", -25.0)),
    "lon_max": float(os.environ.get("VISOR_LON_MAX", 45.0)),
}

# --- OpenSky -------------------------------------------------------------------------------
RECORD = os.environ.get("VISOR_RECORD", "1") != "0"   # 0 = don't poll OpenSky (tests, CI)
OPENSKY_CLIENT_ID = os.environ.get("OPENSKY_CLIENT_ID")
OPENSKY_CLIENT_SECRET = os.environ.get("OPENSKY_CLIENT_SECRET")
OPENSKY_AUTHENTICATED = bool(OPENSKY_CLIENT_ID and OPENSKY_CLIENT_SECRET)

# A Europe-sized bounding box costs 4 credits per /states/all call.
#   anonymous:      400 credits/day  -> 100 calls/day  -> one every ~15 min
#   authenticated: 4000 credits/day  -> 1000 calls/day -> one every ~90 s
DEFAULT_POLL = 90 if OPENSKY_AUTHENTICATED else 900
POLL_INTERVAL_S = float(os.environ.get("OPENSKY_POLL_INTERVAL", DEFAULT_POLL))
RETENTION_S = float(os.environ.get("VISOR_RETENTION_HOURS", 24)) * 3600
# Authenticated users may request states up to 1 h in the past: backfill on first start.
BACKFILL_STEP_S = float(os.environ.get("OPENSKY_BACKFILL_STEP", 300))

# --- Simulation ----------------------------------------------------------------------------
TICK_REAL_S = 0.5            # wall-clock period of the simulation loop
MAX_SUBSTEP_S = 4.0          # largest integration step (sim seconds)
CONFLICT_PERIOD_S = 4.0      # sim seconds between separation scans
DECISION_PERIOD_S = 5.0      # sim seconds between decision-point refreshes
LOOKAHEAD_S = 120.0          # conflict prediction horizon (STCA)
SEP_H_NM = 5.0               # horizontal separation minimum (en-route)
SEP_H_TMA_NM = 3.0           # below FL100 (terminal areas)
TMA_CEILING_FT = 10000.0
SEP_V_FT = 1000.0            # vertical separation minimum
LOS_V_FT = 900.0             # vertical loss threshold (tolerates altimetry noise)
PILOT_DELAY_S = (3.0, 8.0)   # pilot reaction time range
