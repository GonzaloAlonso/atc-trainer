"""Private training sandboxes for the guided tutorial.

Every trainee gets their own Engine running a small scripted scenario in the Alps Upper sector
(ALP-U, FL245–345),
so practising never disturbs the shared live simulation and the tutorial's situations (a
conflict, a pilot request) happen when the lesson needs them:

  * TRN303 cruises FL290 across the sector: the aircraft the trainee practises clearances on.
    Held at FL330 away from its planned level, it will ask for FL290 back (a pilot request).
  * Background traffic, mostly in the Lower and High layers above and below, never in conflict.
  * inject_conflict() puts TRN101 and TRN202 head-on at FL310 a few minutes apart.
"""

import math
import threading
import time

from .aircraft import FlightPlan
from .engine import Engine
from .store import Store

SECTOR = "ALP-U"
KT = 0.514444
FT = 0.3048

TRAINEE = ("7a0303", "TRN303", 45.0, 3.4, 29000, 440, 90)
BACKGROUND = [
    # icao, callsign, lat, lon, alt ft, gs kt, track
    ("7b0011", "BKG11", 45.6, 3.4, 37000, 460, 110),     # ALP-H
    ("7b0012", "BKG12", 43.4, 9.6, 20000, 380, 290),     # ALP-L
    ("7b0013", "BKG13", 45.8, 9.5, 39000, 450, 250),     # ALP-H
    ("7b0014", "BKG14", 43.3, 4.0, 15000, 330, 60),      # ALP-L
    ("7b0015", "BKG15", 45.4, 9.0, 41000, 480, 200),     # ALP-H
    ("7b0016", "BKG16", 43.6, 4.6, 26000, 420, 80),      # ALP-U, well below the lesson traffic
]
CONFLICT = [("7c0101", "TRN101", 90), ("7c0202", "TRN202", 270)]
CONFLICT_CENTRE = (44.3, 7.3)
CONFLICT_GAP_NM = 70.0          # head-on at 900 kt closure: STCA after ~2.5 min, meet after ~4.7 min
SCENARIO_S = 2 * 3600
EVERY_S = 60


def fly(lat, lon, trk, gs_kt, dt_s):
    """Position after flying a constant track (flat-earth; fine for scenario building)."""
    d = gs_kt * dt_s / 3600.0
    h = math.radians(trk)
    lat2 = lat + d * math.cos(h) / 60.0
    lon2 = lon + d * math.sin(h) / (60.0 * math.cos(math.radians(lat)))
    return lat2, lon2


def state(icao, callsign, lat, lon, alt_ft, gs_kt, trk, vs_fpm=0.0, t=0, category=4):
    """One OpenSky state vector (extended format, 18 fields)."""
    return [icao, callsign, "Training", t, t, lon, lat, alt_ft * FT, False, gs_kt * KT, trk,
            vs_fpm * FT / 60.0, None, alt_ft * FT, "1000", False, 0, category]


def write_snapshots(store, flights, t0, duration_s=SCENARIO_S, every_s=EVERY_S):
    """Record straight-line flights as OpenSky snapshots (what the recorder would have stored)."""
    for k in range(int(duration_s // every_s) + 1):
        t = t0 + k * every_s
        states = []
        for icao, cs, lat, lon, alt, gs, trk in flights:
            la, lo = fly(lat, lon, trk, gs, k * every_s)
            states.append(state(icao, cs, la, lo, alt, gs, trk, t=t))
        store.insert_snapshot({"time": t, "states": states})


def straight_plan(icao, callsign, lat, lon, alt, gs, trk, t_start, duration_s=1800, every_s=30):
    plan = FlightPlan(icao)
    for k in range(int(duration_s // every_s) + 1):
        la, lo = fly(lat, lon, trk, gs, k * every_s)
        plan.add((t_start + k * every_s, icao, callsign, "Training", la, lo, alt, gs, trk, 0.0, 4, "1000"))
    return plan


class Sandbox:
    def __init__(self, navdata):
        self.created = self.last_used = time.time()
        self.store = Store(":memory:")
        # Scenario time sits hours in the past so replay can run at up to 16x without
        # catching up with the wall clock.
        t0 = int(time.time()) - 3 * 3600
        write_snapshots(self.store, [TRAINEE] + BACKGROUND, t0)
        self.engine = Engine(self.store, navdata)
        self.engine.reset("replay", t0)
        self.engine.start()

    def inject_conflict(self):
        """Put TRN101/TRN202 head-on at FL310, CONFLICT_GAP_NM apart, from the current sim time."""
        e = self.engine
        with e.lock:
            lat0, lon0 = CONFLICT_CENTRE
            dlon = (CONFLICT_GAP_NM / 2) / (60.0 * math.cos(math.radians(lat0)))
            for (icao, cs, trk), lon in zip(CONFLICT, (lon0 - dlon, lon0 + dlon)):
                e.aircraft.pop(icao, None)
                e.loader.plans[icao] = straight_plan(icao, cs, lat0, lon, 31000, 450, trk, int(e.t))
            e._spawn()
            e._update_sectors()
            e._build_frame()
        return {"aircraft": [cs for _, cs, _ in CONFLICT]}

    def stop(self):
        self.engine.stop()


class TutorialManager:
    """One sandbox per user, created on demand and reaped when idle."""

    def __init__(self, navdata, max_sandboxes=20, idle_s=1800):
        self.navdata = navdata
        self.max = max_sandboxes
        self.idle_s = idle_s
        self._boxes = {}
        self._lock = threading.Lock()

    def start(self, user_id):
        with self._lock:
            old = self._boxes.pop(user_id, None)
            if old is not None:
                old.stop()
            if len(self._boxes) >= self.max:
                raise OverflowError("too many training sessions, try again later")
            box = self._boxes[user_id] = Sandbox(self.navdata)
            return box

    def get(self, user_id):
        with self._lock:
            box = self._boxes.get(user_id)
            if box is not None:
                box.last_used = time.time()
            return box

    def stop(self, user_id):
        with self._lock:
            box = self._boxes.pop(user_id, None)
        if box is not None:
            box.stop()
        return box is not None

    def reap(self, now=None):
        now = now or time.time()
        with self._lock:
            idle = [uid for uid, b in self._boxes.items() if now - b.last_used > self.idle_s]
            boxes = [self._boxes.pop(uid) for uid in idle]
        for b in boxes:
            b.stop()
        return len(boxes)

    def engines(self):
        with self._lock:
            return {uid: b.engine for uid, b in self._boxes.items()}

    def stop_all(self):
        with self._lock:
            boxes = list(self._boxes.values())
            self._boxes.clear()
        for b in boxes:
            b.stop()
