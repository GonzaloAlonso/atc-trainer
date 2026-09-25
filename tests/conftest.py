"""Shared fixtures. Environment is set before any `atc` import: config is read at import time."""

import math
import os
import tempfile
import time

_TMP = tempfile.mkdtemp(prefix="visor-tests-")
os.environ["VISOR_DATA_DIR"] = _TMP
os.environ.setdefault("VISOR_NAVDATA_DIR", os.path.join(_TMP, "navdata"))  # missing -> no fixes
os.environ["VISOR_RECORD"] = "0"
os.environ["VISOR_ADMIN_USER"] = "admin"
os.environ["VISOR_ADMIN_PASSWORD"] = "admin-password-123"
os.environ.pop("OPENSKY_CLIENT_ID", None)
os.environ.pop("OPENSKY_CLIENT_SECRET", None)

import pytest  # noqa: E402

import atc.auth  # noqa: E402
from atc import config  # noqa: E402
from atc.store import Store  # noqa: E402

atc.auth.PBKDF2_ITERATIONS = 1000   # fast hashing in tests (hashes store their iteration count)

ADMIN = ("admin", "admin-password-123")

FT = 0.3048
KT = 0.514444


def state(icao, callsign, lat, lon, alt_ft, gs_kt, trk, vs_fpm=0.0, t=0, category=4):
    """One OpenSky state vector (extended format, 18 fields)."""
    return [icao, callsign, "Testland", t, t, lon, lat, alt_ft * FT, False, gs_kt * KT, trk,
            vs_fpm * FT / 60.0, None, alt_ft * FT, "1000", False, 0, category]


def fly(lat, lon, trk, gs_kt, dt_s):
    d = gs_kt * dt_s / 3600.0
    h = math.radians(trk)
    lat2 = lat + d * math.cos(h) / 60.0
    lon2 = lon + d * math.sin(h) / (60.0 * math.cos(math.radians(lat)))
    return lat2, lon2


# Two airliners head-on at FL350 inside the ALPS-UPPER sector, plus one well clear at FL390.
TRAFFIC = [
    # icao, callsign, lat, lon, alt, gs, track
    ("aaa001", "TST001", 46.9, 7.2, 35000, 450, 90),
    ("bbb002", "TST002", 46.9, 10.8, 35000, 450, 270),
    ("ccc003", "TST003", 47.6, 7.0, 39000, 460, 90),
]


def write_scenario(store, t0, snapshots=25, every=60):
    for k in range(snapshots):
        t = t0 + k * every
        states = []
        for icao, cs, lat, lon, alt, gs, trk in TRAFFIC:
            la, lo = fly(lat, lon, trk, gs, k * every)
            states.append(state(icao, cs, la, lo, alt, gs, trk, t=t))
        store.insert_snapshot({"time": t, "states": states})


class FakeNav:
    """Minimal navdata with a couple of fixes near the scenario."""

    fixes = [
        {"ident": "ALPHA", "name": "Alpha VOR", "kind": "VOR", "lat": 47.5, "lon": 9.0},
        {"ident": "BRAVO", "name": "Bravo VOR", "kind": "VOR", "lat": 46.0, "lon": 8.0},
    ]

    def find(self, ident, near_lat=None, near_lon=None):
        return next((f for f in self.fixes if f["ident"] == ident.upper()), None)

    def nearby(self, lat, lon, radius_nm, kinds=None, limit=40):
        return [dict(f, dist_nm=0.0) for f in self.fixes]


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "traffic.db")


@pytest.fixture
def scenario_store(store):
    t0 = int(time.time()) - 7200
    write_scenario(store, t0)
    return store, t0


@pytest.fixture
def engine(scenario_store):
    from atc.engine import Engine
    store, t0 = scenario_store
    e = Engine(store, FakeNav())
    e.reset("replay", t0)
    return e


@pytest.fixture
def fresh_paths(tmp_path, monkeypatch):
    """Point the app at an empty data directory (traffic + users)."""
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "traffic.db")
    monkeypatch.setattr(config, "USERS_DB_PATH", tmp_path / "users.db")
    return tmp_path


def login(client, username, password):
    r = client.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return r.json()


@pytest.fixture
def client(fresh_paths):
    """TestClient signed in as the bootstrap admin."""
    from fastapi.testclient import TestClient
    from atc.api import create_app
    with TestClient(create_app()) as c:
        login(c, *ADMIN)
        yield c
